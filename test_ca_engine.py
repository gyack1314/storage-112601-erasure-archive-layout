"""引擎语义：承诺原子性、坏片、租约、迁移、重启恢复与容量/域约束。"""

import tempfile
import unittest
from pathlib import Path

from cold_archive import coding
from cold_archive.engine import ArchiveEngine
from cold_archive.errors import (
    BadPiece,
    DuplicateHolder,
    DuplicateReport,
    LayoutInUse,
    LayoutNotCompliant,
    NotRecoverable,
    StaleGeneration,
)

HEX = "10203040"
SOURCE = bytes.fromhex(HEX)
DIGEST = coding.digest(SOURCE)
PIECES = coding.expected_pieces(SOURCE)


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "archive"
        self.engine = ArchiveEngine(self.root)
        for name, domain, free in [
            ("da", "X", 4096),
            ("db", "Y", 4096),
            ("dc", "Z", 4096),
            ("dd", "W", 4096),
            ("de", "V", 4096),
        ]:
            self.engine.handle_disk({"disk": name, "domain": domain, "free": free})

    def tearDown(self):
        self.tmp.cleanup()

    def commit_first_layout(self, key="log", rev=1, layout=1, disks=("da", "db", "dc")):
        if (key, rev) not in self.engine.catalog.objects:
            self.engine.handle_object({"key": key, "rev": rev, "bytes": HEX})
        for index, disk in zip((0, 1, 2), disks, strict=True):
            self.engine.handle_piece(
                {"key": key, "rev": rev, "layout": layout, "index": index,
                 "disk": disk, "hex": PIECES[index].hex()}
            )
        return self.engine.handle_finish({"key": key, "rev": rev, "layout": layout})


class CommitAndReadTests(EngineTestBase):
    def test_put_is_invisible_until_committed(self):
        self.engine.handle_object({"key": "log", "rev": 1, "bytes": HEX})
        # 只落两片时布局仍是 building，读请求看不到它
        self.engine.handle_piece(
            {"key": "log", "rev": 1, "layout": 1, "index": 0, "disk": "da",
             "hex": PIECES[0].hex()}
        )
        with self.assertRaises(LayoutNotCompliant):
            self.engine.handle_read(
                {"holder": "h", "key": "log", "rev": 1, "layout": 1}
            )
        # 补齐剩余两片后承诺（索引0已存在，只补 1/2）
        for index, disk in ((1, "db"), (2, "dc")):
            self.engine.handle_piece(
                {"key": "log", "rev": 1, "layout": 1, "index": index,
                 "disk": disk, "hex": PIECES[index].hex()}
            )
        self.engine.handle_finish({"key": "log", "rev": 1, "layout": 1})
        receipt = self.engine.handle_read(
            {"holder": "h", "key": "log", "rev": 1, "layout": 1}
        )
        self.assertEqual(receipt["bytes"], HEX)

    def test_bad_digest_piece_is_rejected(self):
        self.engine.handle_object({"key": "log", "rev": 1, "bytes": HEX})
        with self.assertRaises(BadPiece):
            self.engine.handle_piece(
                {"key": "log", "rev": 1, "layout": 1, "index": 0, "disk": "da",
                 "hex": "ffff"}
            )
        self.assertEqual(self.engine.catalog.pieces, [])

    def test_same_index_replica_counts_once(self):
        self.commit_first_layout()
        # 在 dd 上给索引 0 再放一份副本
        self.engine.handle_move({"key": "log", "rev": 1, "from": 1, "to": 2})
        report = self.engine.handle_piece(
            {"key": "log", "rev": 1, "layout": 2, "index": 0, "disk": "da",
             "hex": PIECES[0].hex()}
        )
        self.assertTrue(report["index_counted"])
        report = self.engine.handle_piece(
            {"key": "log", "rev": 1, "layout": 2, "index": 0, "disk": "dd",
             "hex": PIECES[0].hex()}
        )
        self.assertFalse(report["index_counted"])
        self.assertEqual(report["replica_of"], 0)

    def test_duplicate_report_is_idempotent(self):
        self.commit_first_layout()
        self.engine.handle_move({"key": "log", "rev": 1, "from": 1, "to": 2})
        self.engine.handle_piece(
            {"key": "log", "rev": 1, "layout": 2, "index": 0, "disk": "da",
             "hex": PIECES[0].hex()}
        )
        with self.assertRaises(DuplicateReport):
            self.engine.handle_piece(
                {"key": "log", "rev": 1, "layout": 2, "index": 0, "disk": "da",
                 "hex": PIECES[0].hex()}
            )

    def test_domain_collision_blocks_commit_even_if_bytes_correct(self):
        # 布局1：da(X)/db(Y)/dc(Z) 三域隔离，承诺成功
        self.commit_first_layout()
        # 新布局让数据片与校验片同域（db 与 db2 都在 Y）
        self.engine.handle_disk({"disk": "db2", "domain": "Y", "free": 4096})
        self.engine.handle_move({"key": "log", "rev": 1, "from": 1, "to": 2})
        for index, disk in {0: "da", 1: "db", 2: "db2"}.items():
            self.engine.handle_piece(
                {"key": "log", "rev": 1, "layout": 2, "index": index,
                 "disk": disk, "hex": PIECES[index].hex()}
            )
        with self.assertRaises(LayoutNotCompliant) as ctx:
            self.engine.handle_finish({"key": "log", "rev": 1, "layout": 2})
        self.assertIn("故障域", str(ctx.exception))
        # 旧布局仍然承诺可用，新布局保持 building
        self.assertEqual(
            self.engine.catalog.require_object("log", 1).layouts,
            {1: "committed", 2: "building"},
        )

    def test_report_to_committed_generation_is_rejected(self):
        self.commit_first_layout()
        with self.assertRaises(StaleGeneration):
            self.engine.handle_piece(
                {"key": "log", "rev": 1, "layout": 1, "index": 0, "disk": "dd",
                 "hex": PIECES[0].hex()}
            )


class FailureAndRecoveryTests(EngineTestBase):
    def test_recoverable_after_one_disk_fault(self):
        self.commit_first_layout()
        self.engine.handle_fault({"disk": "da"})
        receipt = self.engine.handle_read(
            {"holder": "ops", "key": "log", "rev": 1, "layout": 1}
        )
        self.assertEqual(receipt["bytes"], HEX)
        self.assertEqual(receipt["used_indices"], [1, 2])

    def test_unrecoverable_names_missing_dimensions(self):
        self.commit_first_layout()
        self.engine.handle_fault({"disk": "da"})
        self.engine.handle_fault({"disk": "db"})
        with self.assertRaisesRegex(NotRecoverable, "缺失维度为索引 0、1"):
            self.engine.handle_read(
                {"holder": "lost", "key": "log", "rev": 1, "layout": 1}
            )

    def test_bit_rot_treated_as_bad_piece(self):
        self.commit_first_layout()
        self.engine.handle_fault({"disk": "da"})
        target = self.engine.catalog.disks["db"].path_for("log", 1, 1, 1)
        target.write_bytes(b"rot")
        with self.assertRaises(NotRecoverable):
            self.engine.handle_read(
                {"holder": "h", "key": "log", "rev": 1, "layout": 1}
            )
        # 坏片已被记账剔除
        self.assertNotIn(
            "db",
            [p.disk for p in self.engine.catalog.pieces_of("log", 1, 1) if p.index == 1],
        )

    def test_rebuilt_content_must_match_original_digest(self):
        # 直接篡改权威元数据之外的物理文件且绕过编码自洽：构造两片互相自洽
        # 但与原摘要不同的字节，重建后必须被原摘要拦截。
        self.commit_first_layout()
        fake0, fake1 = b"7777", b"8888"
        fake2 = bytes(a ^ b for a, b in zip(fake0, fake1, strict=True))
        for index, disk, payload in [
            (0, "da", fake0), (1, "db", fake1), (2, "dc", fake2)
        ]:
            path = self.engine.catalog.disks[disk].path_for("log", 1, 1, index)
            from cold_archive.shardfile import MAGIC, ShardHeader, piece_digest
            header = ShardHeader("log", 1, 1, index, disk, piece_digest(payload))
            path.write_bytes(MAGIC + header.to_json() + b"\n" + payload)
        with self.assertRaises(NotRecoverable):
            self.engine.handle_read(
                {"holder": "h", "key": "log", "rev": 1, "layout": 1}
            )


class LeaseAndMigrationTests(EngineTestBase):
    def test_lease_pins_old_layout_until_release(self):
        self.commit_first_layout()
        self.engine.handle_read(
            {"holder": "old", "key": "log", "rev": 1, "layout": 1}
        )
        self.engine.migrate("log", 1, 2)
        with self.assertRaises(LayoutInUse):
            self.engine.handle_retire({"key": "log", "rev": 1, "layout": 1})
        self.engine.handle_release({"holder": "old"})
        self.engine.handle_retire({"key": "log", "rev": 1, "layout": 1})
        self.assertEqual(
            self.engine.catalog.require_object("log", 1).layouts[1], "retired"
        )

    def test_duplicate_holder_rejected(self):
        self.commit_first_layout()
        self.engine.handle_read(
            {"holder": "h", "key": "log", "rev": 1, "layout": 1}
        )
        with self.assertRaises(DuplicateHolder):
            self.engine.handle_read(
                {"holder": "h", "key": "log", "rev": 1, "layout": 1}
            )

    def test_download_pins_requested_layout_when_newer_exists(self):
        self.commit_first_layout()
        self.engine.handle_read(
            {"holder": "pin", "key": "log", "rev": 1, "layout": 1}
        )
        self.engine.migrate("log", 1, 2)
        receipt = self.engine.download("fresh", "log", 1, layout=1)
        self.assertEqual(receipt["layout"], 1)
        latest = self.engine.download("auto", "log", 1)
        self.assertEqual(latest["layout"], 2)

    def test_migrate_rebuilds_from_surviving_pieces_and_survives_restart(self):
        self.commit_first_layout()
        self.engine.handle_fault({"disk": "da"})
        # 修复迁移：自动挑选三域隔离的健康盘
        self.engine.migrate("log", 1, 2)
        view = self.engine.inspect("log")
        self.assertTrue(view["revs"][1]["layouts"][2]["compliant"])
        # 协调端中断后重放：building/committed 与租约均还原
        self.engine.handle_read(
            {"holder": "after", "key": "log", "rev": 1, "layout": 2}
        )
        restored = self.engine.handle_restart({})
        self.assertEqual(restored["committed_layouts"], 2)
        self.assertEqual(restored["leases_restored"], 1)
        receipt = self.engine.handle_read(
            {"holder": "after2", "key": "log", "rev": 1, "layout": 2}
        )
        self.assertEqual(receipt["bytes"], HEX)

    def test_capacity_constraint_blocks_placement(self):
        # 独立归档：只有三块隔离域的小容量盘，片字节长度 2 > free 1
        from cold_archive.engine import ArchiveEngine as _Engine

        small_root = Path(self.tmp.name) / "small"
        small_engine = _Engine(small_root)
        for name, domain in [("s1", "P"), ("s2", "Q"), ("s3", "R")]:
            small_engine.handle_disk({"disk": name, "domain": domain, "free": 1})
        with self.assertRaises(LayoutNotCompliant):
            small_engine.put(
                "big", 9, bytes.fromhex("aabbccdd"),
                coding.digest(bytes.fromhex("aabbccdd")),
            )

    def test_explicit_targets_must_use_three_domains(self):
        self.commit_first_layout()
        # da(X) 与 dd(W) 不同域，但 db(Y) 与 db2(Y) 同域：显式要求 db2 放索引2
        self.engine.handle_disk({"disk": "db2", "domain": "Y", "free": 4096})
        with self.assertRaises(LayoutNotCompliant):
            self.engine.migrate(
                "log", 1, 2, explicit_disks={0: "da", 1: "db", 2: "db2"}
            )
        # 失败的迁移留下 building 布局而不是半成品承诺
        self.assertEqual(
            self.engine.catalog.require_object("log", 1).layouts[2], "building"
        )


if __name__ == "__main__":
    unittest.main()
