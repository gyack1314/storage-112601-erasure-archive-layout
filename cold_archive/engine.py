"""冷归档协调引擎。

对外提供两层接口：

1. 契约消息处理（``handle_*``），与 examples/ 下 JSONL 的 11 种 kind 一一对应，
   场景回放器和维修回报都走这条路径；
2. 高层工作流（``put``、``download``、``migrate``），供 CLI 与演示入口使用。

所有状态变化经 :class:`~cold_archive.catalog.Catalog` 落事件日志，
协调端重启（``handle_restart``）后重建内存，building 与 committed 不混淆。
"""

from pathlib import Path

from . import coding
from .catalog import Catalog, PieceRecord
from .errors import (
    BadPiece,
    DuplicateReport,
    LayoutNotCompliant,
    NotRecoverable,
    StaleGeneration,
)
from .shardfile import piece_digest

INITIAL_LAYOUT = 1


class ArchiveEngine:
    def __init__(self, root: str | Path):
        self.catalog = Catalog(Path(root))

    # ============================================================ 契约消息
    def handle_disk(self, record: dict) -> dict:
        return self.catalog.register_disk(
            record["disk"], record["domain"], int(record["free"])
        )

    def handle_fault(self, record: dict) -> dict:
        return self.catalog.fault_disk(record["disk"])

    def handle_object(self, record: dict) -> dict:
        key, rev = record["key"], int(record["rev"])
        source = bytes.fromhex(record["bytes"])
        source_digest = coding.digest(source)
        expected = coding.expected_pieces(source)
        digests = {index: piece_digest(piece) for index, piece in expected.items()}
        self.catalog.write_object(
            key, rev, source, source_digest, digests, INITIAL_LAYOUT
        )
        return {
            "key": key,
            "rev": rev,
            "source_digest": source_digest,
            "source_length": len(source),
            "layout": INITIAL_LAYOUT,
            "state": "building",
        }

    def handle_piece(self, record: dict) -> dict:
        key, rev = record["key"], int(record["rev"])
        layout, index, disk = (
            int(record["layout"]),
            int(record["index"]),
            record["disk"],
        )
        obj = self.catalog.require_layout(key, rev, layout)
        if obj.layouts[layout] != "building":
            # 已承诺/已回收的代次不再接受回报，避免备用片被计成新可用片
            raise StaleGeneration(
                f"布局 {layout} 状态为 {obj.layouts[layout]}，"
                "分片回报只在 building 期间接受"
            )
        if index not in coding.REQUIRED_INDICES:
            raise BadPiece(f"非法分片索引 {index}（2+1 仅接受 0/1/2）")
        store = self.catalog.require_disk(disk)
        payload = bytes.fromhex(record["hex"])
        if piece_digest(payload) != obj.pieces[index]:
            raise BadPiece(
                f"回报分片摘要与对象 {key} rev{rev} 索引 {index} 的权威内容不符，按坏片拒收"
            )
        existing = self.catalog.find_piece(key, rev, layout, index, disk)
        if existing is not None:
            # 同位置重复回报：幂等，绝不能计成新的可用分片
            raise DuplicateReport(
                f"布局 {layout} 索引 {index} 在盘 {disk} 上已回报过，重复回报不计新片"
            )
        same_index = [
            p for p in self.catalog.pieces_of(key, rev, layout) if p.index == index
        ]
        store.write_shard(key, rev, layout, index, payload)
        self.catalog.report_piece(
            key, rev, layout, index, disk, obj.pieces[index]
        )
        return {
            "key": key,
            "rev": rev,
            "layout": layout,
            "index": index,
            "disk": disk,
            "domain": store.domain,
            "index_counted": not same_index,  # 同索引副本只计一个维度
            "replica_of": index if same_index else None,
        }

    def handle_finish(self, record: dict) -> dict:
        key, rev, layout = record["key"], int(record["rev"]), int(record["layout"])
        obj = self.catalog.require_layout(key, rev, layout)
        if obj.layouts[layout] == "committed":
            raise StaleGeneration(f"布局 {layout} 已承诺，重复 finish 属于旧代次回报")
        chosen, reasons = self._evaluate_layout(key, rev, layout)
        if reasons:
            raise LayoutNotCompliant("；".join(reasons))
        # 承诺前再读一遍实际介质，字节级坏片不能进入承诺
        for piece in chosen:
            self._verify_stored(piece)
        self.catalog.commit_layout(key, rev, layout)
        return {
            "key": key,
            "rev": rev,
            "layout": layout,
            "state": "committed",
            "placement": [
                {"index": p.index, "disk": p.disk, "domain": self.catalog.disks[p.disk].domain}
                for p in chosen
            ],
        }

    def handle_read(self, record: dict) -> dict:
        holder = record["holder"]
        key, rev, layout = record["key"], int(record["rev"]), int(record["layout"])
        obj = self.catalog.require_layout(key, rev, layout)
        if obj.layouts[layout] != "committed":
            raise LayoutNotCompliant(
                f"布局 {layout} 状态为 {obj.layouts[layout]}，未承诺不可领用"
            )
        usable, media_paths = self._usable_pieces(key, rev, layout)
        distinct = sorted(usable)
        if len(distinct) < 2:
            missing = [i for i in coding.REQUIRED_INDICES if i not in distinct]
            if not distinct:
                detail = "三个索引的分片全部不可用"
            else:
                detail = (
                    f"仅剩索引 {distinct[0]} 的分片，缺失维度为索引 "
                    + "、".join(map(str, missing))
                )
            raise NotRecoverable(
                f"对象 {key} rev{rev} 布局 {layout} 无法恢复：{detail}"
                "（2+1 至少需要两个不同索引的正确分片）"
            )
        source = coding.rebuild(
            {index: payload for index, (payload, _) in usable.items()},
            obj.source_length,
        )
        if coding.digest(source) != obj.source_digest:
            raise NotRecoverable(
                f"对象 {key} rev{rev} 重建内容与原摘要不符，拒绝交付"
            )
        self.catalog.open_lease(holder, key, rev, layout, media_paths)
        return {
            "holder": holder,
            "key": key,
            "rev": rev,
            "layout": layout,
            "bytes": source.hex(),
            "source_digest": obj.source_digest,
            "used_indices": distinct,
            "media_paths": media_paths,
        }

    def handle_release(self, record: dict) -> dict:
        lease = self.catalog.release_lease(record["holder"])
        return {"holder": record["holder"], "released": True, "layout": lease.layout}

    def handle_move(self, record: dict) -> dict:
        key, rev = record["key"], int(record["rev"])
        source_layout, target_layout = int(record["from"]), int(record["to"])
        obj = self.catalog.require_layout(key, rev, source_layout)
        if obj.layouts[source_layout] != "committed":
            raise StaleGeneration(
                f"源布局 {source_layout} 状态为 {obj.layouts[source_layout]}，不能作为迁移基准"
            )
        self.catalog.open_layout(key, rev, target_layout)
        return {
            "key": key,
            "rev": rev,
            "from": source_layout,
            "to": target_layout,
            "state": "building",
        }

    def handle_retire(self, record: dict) -> dict:
        key, rev, layout = record["key"], int(record["rev"]), int(record["layout"])
        self.catalog.retire_layout(key, rev, layout)
        return {"key": key, "rev": rev, "layout": layout, "retired": True}

    def handle_restart(self, record: dict | None = None) -> dict:
        """协调端中断恢复：重放事件日志，building/committed 状态原样还原。"""
        before_objects = len(self.catalog.objects)
        before_leases = len(self.catalog.leases)
        self.catalog = Catalog(self.catalog.root)
        building = sum(
            1
            for obj in self.catalog.objects.values()
            for state in obj.layouts.values()
            if state == "building"
        )
        committed = sum(
            1
            for obj in self.catalog.objects.values()
            for state in obj.layouts.values()
            if state == "committed"
        )
        return {
            "restarted": True,
            "objects": before_objects,
            "leases_restored": before_leases,
            "building_layouts": building,
            "committed_layouts": committed,
            "wal": str(self.catalog.wal_path),
        }

    def handle_inspect(self, record: dict) -> dict:
        return self.inspect(record["key"])

    # ============================================================ 高层工作流
    def put(self, key: str, rev: int, source: bytes, client_digest: str) -> dict:
        """一次完整的对象写入：选盘、编码、落片、原子承诺。

        承诺前任何一步失败都不会产生 committed 布局，客户端看不到半成品。
        """
        if coding.digest(source) != client_digest:
            raise BadPiece("客户端给出的原内容摘要与实际字节不符，拒绝写入")
        expected = coding.expected_pieces(source)
        targets = self._select_target_disks(
            len(next(iter(expected.values()))) if source else 0
        )
        receipt = self.handle_object(
            {"key": key, "rev": rev, "bytes": source.hex()}
        )
        layout = receipt["layout"]
        for index, disk in targets.items():
            self.handle_piece(
                {
                    "key": key,
                    "rev": rev,
                    "layout": layout,
                    "index": index,
                    "disk": disk,
                    "hex": expected[index].hex(),
                }
            )
        committed = self.handle_finish(
            {"key": key, "rev": rev, "layout": layout}
        )
        committed["source_digest"] = receipt["source_digest"]
        return committed

    def download(self, holder: str, key: str, rev: int, layout: int | None = None) -> dict:
        """打开并下载；不指定布局时取该对象最新的已承诺布局。"""
        if layout is None:
            layout = self.latest_committed_layout(key, rev)
        return self.handle_read(
            {"holder": holder, "key": key, "rev": rev, "layout": layout}
        )

    def latest_committed_layout(self, key: str, rev: int) -> int:
        obj = self.catalog.require_object(key, rev)
        committed = [l for l, s in obj.layouts.items() if s == "committed"]
        if not committed:
            raise LayoutNotCompliant(f"对象 {key} rev{rev} 没有已承诺布局")
        return max(committed)

    def migrate(
        self,
        key: str,
        rev: int,
        target_layout: int,
        explicit_disks: dict[int, str] | None = None,
    ) -> dict:
        """从仍可恢复的旧布局重建权威分片，写入新布局并承诺。"""
        obj = self.catalog.require_object(key, rev)
        source_layout = self.latest_committed_layout(key, rev)
        usable, _ = self._usable_pieces(key, rev, source_layout)
        if len(usable) < 2:
            raise NotRecoverable("旧布局已无法恢复，不能启动有内容依据的迁移")
        source = coding.rebuild(
            {index: payload for index, (payload, _) in usable.items()},
            obj.source_length,
        )
        if coding.digest(source) != obj.source_digest:
            raise NotRecoverable("迁移前重建内容与原摘要不符")
        self.handle_move(
            {"key": key, "rev": rev, "from": source_layout, "to": target_layout}
        )
        expected = coding.expected_pieces(source)
        if explicit_disks is not None:
            self._validate_explicit_targets(key, rev, target_layout, explicit_disks)
            targets = explicit_disks
        else:
            shard_len = len(next(iter(expected.values())))
            targets = self._select_target_disks(shard_len)
        for index, disk in targets.items():
            self.handle_piece(
                {
                    "key": key,
                    "rev": rev,
                    "layout": target_layout,
                    "index": index,
                    "disk": disk,
                    "hex": expected[index].hex(),
                }
            )
        return self.handle_finish(
            {"key": key, "rev": rev, "layout": target_layout}
        )

    # ============================================================ 合规与巡检
    def _evaluate_layout(self, key: str, rev: int, layout: int):
        """返回 (每个索引选中的分片列表, 不合规原因列表)。

        选择规则：三个索引各计一次（副本不增加维度），且所在故障域两两不同；
        故障盘上的分片不参与选择。
        """
        obj = self.catalog.require_layout(key, rev, layout)
        records = self.catalog.pieces_of(key, rev, layout)
        by_index: dict[int, list[PieceRecord]] = {}
        for piece in records:
            by_index.setdefault(piece.index, []).append(piece)
        reasons = []
        missing = [i for i in coding.REQUIRED_INDICES if i not in by_index]
        for index in missing:
            reasons.append(f"缺少索引 {index} 的分片")
        healthy: dict[int, list[PieceRecord]] = {}
        for index, group in by_index.items():
            alive = [p for p in group if not self.catalog.disks[p.disk].faulted]
            if not alive:
                reasons.append(f"索引 {index} 的分片全部位于故障盘")
            else:
                healthy[index] = alive
        chosen: list[PieceRecord] = []
        occupied_domains: set[str] = set()
        # 候选域少的索引先选，降低冲突误判
        def _domains(index: int) -> set[str]:
            return {self.catalog.disks[p.disk].domain for p in healthy[index]}

        for index in sorted(healthy, key=lambda i: len(_domains(i))):
            pick = next(
                (
                    p
                    for p in healthy[index]
                    if self.catalog.disks[p.disk].domain not in occupied_domains
                ),
                None,
            )
            if pick is None:
                domains = sorted(_domains(index))
                reasons.append(
                    f"索引 {index} 只能落在故障域 {domains}，均已被其他分片占用，"
                    "数据片与校验片不得同域"
                )
                continue
            chosen.append(pick)
            occupied_domains.add(self.catalog.disks[pick.disk].domain)
        return sorted(chosen, key=lambda p: p.index), reasons

    def _verify_stored(self, piece: PieceRecord) -> None:
        store = self.catalog.disks[piece.disk]
        try:
            payload = store.read_shard(
                piece.key, piece.rev, piece.layout, piece.index
            )
        except BadPiece:
            self.catalog.mark_piece_bad(
                piece.key, piece.rev, piece.layout, piece.index, piece.disk
            )
            raise
        obj = self.catalog.require_object(piece.key, piece.rev)
        if piece_digest(payload) != obj.pieces[piece.index]:
            self.catalog.mark_piece_bad(
                piece.key, piece.rev, piece.layout, piece.index, piece.disk
            )
            raise BadPiece(
                f"盘 {piece.disk} 上索引 {piece.index} 实际字节与权威摘要不符"
            )

    def _usable_pieces(self, key: str, rev: int, layout: int):
        """读取路径：逐盘实读，坏片记账，按索引去重，返回介质路径供租约钉住。"""
        usable: dict[int, tuple[bytes, PieceRecord]] = {}
        media_paths: list[str] = []
        for piece in self.catalog.pieces_of(key, rev, layout):
            store = self.catalog.disks.get(piece.disk)
            if store is None or store.faulted:
                continue
            try:
                payload = store.read_shard(key, rev, layout, piece.index)
            except BadPiece:
                self.catalog.mark_piece_bad(key, rev, layout, piece.index, piece.disk)
                continue
            obj = self.catalog.require_object(key, rev)
            if piece_digest(payload) != obj.pieces[piece.index]:
                self.catalog.mark_piece_bad(key, rev, layout, piece.index, piece.disk)
                continue
            media_paths.append(str(store.path_for(key, rev, layout, piece.index)))
            # 相同片号的副本不充当第二个维度，只保留先读到的一份
            usable.setdefault(piece.index, (payload, piece))
        return usable, media_paths

    def _select_target_disks(self, shard_length: int) -> dict[int, str]:
        """为 0/1/2 各挑一块健康、容量足够、故障域两两不同的盘。

        容量账按片数据字节计，与 DiskStore 的口径一致。
        """
        need = shard_length
        candidates = [
            store
            for store in self.catalog.disks.values()
            if not store.faulted and store.free >= need
        ]
        chosen: dict[int, str] = {}
        used_domains: set[str] = set()
        for index in coding.REQUIRED_INDICES:
            pick = next(
                (s for s in candidates if s.domain not in used_domains), None
            )
            if pick is None:
                raise LayoutNotCompliant(
                    f"找不到三个相互隔离故障域中的健康盘来放置索引 {index}，"
                    "或可用容量不足"
                )
            chosen[index] = pick.name
            used_domains.add(pick.domain)
        return chosen

    def _validate_explicit_targets(
        self, key: str, rev: int, layout: int, targets: dict[int, str]
    ) -> None:
        if set(targets) != set(coding.REQUIRED_INDICES):
            raise LayoutNotCompliant("显式目标必须为索引 0/1/2 各指定一块盘")
        domains = []
        for index, disk in targets.items():
            store = self.catalog.require_disk(disk)
            domains.append(store.domain)
        if len(set(domains)) != 3:
            raise LayoutNotCompliant(
                f"目标盘故障域 {sorted(domains)} 未做到三域隔离，"
                "数据片与校验片同域不能承诺"
            )

    def inspect(self, key: str) -> dict:
        result = {"key": key, "revs": {}}
        for (obj_key, rev), obj in sorted(self.catalog.objects.items()):
            if obj_key != key:
                continue
            layouts_view = {}
            for layout, state in sorted(obj.layouts.items()):
                chosen, reasons = self._evaluate_layout(key, rev, layout)
                pieces_view = []
                for piece in self.catalog.pieces_of(key, rev, layout):
                    store = self.catalog.disks[piece.disk]
                    pieces_view.append(
                        {
                            "index": piece.index,
                            "disk": piece.disk,
                            "domain": store.domain,
                            "healthy_disk": not store.faulted,
                            "digest": piece.piece_digest,
                        }
                    )
                layouts_view[layout] = {
                    "state": state,
                    "pieces": pieces_view,
                    "distinct_indices": sorted({p["index"] for p in pieces_view}),
                    "missing_indices": [
                        i
                        for i in coding.REQUIRED_INDICES
                        if i not in {p["index"] for p in pieces_view}
                    ],
                    # 承诺后又坏盘同样要暴露不合规，不能因为 committed 就掩盖
                    "compliant": not reasons,
                    "reasons": reasons,
                    "placement": [
                        {"index": p.index, "disk": p.disk, "domain": self.catalog.disks[p.disk].domain}
                        for p in chosen
                    ],
                }
            holders = [
                {"holder": h, **{"layout": lease.layout}}
                for h, lease in self.catalog.leases.items()
                if lease.key == key and lease.rev == rev
            ]
            result["revs"][rev] = {
                "source_digest": obj.source_digest,
                "source_length": obj.source_length,
                "layouts": layouts_view,
                "leases": holders,
            }
        result["disks"] = [
            {
                "disk": store.name,
                "domain": store.domain,
                "free": store.free,
                "faulted": store.faulted,
            }
            for store in sorted(self.catalog.disks.values(), key=lambda s: s.name)
        ]
        return result
