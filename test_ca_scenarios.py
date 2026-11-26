"""用仓库自带的两个 JSONL 演练场景驱动引擎，验证契约级行为。"""

import tempfile
import unittest
from pathlib import Path

from cold_archive.engine import ArchiveEngine
from cold_archive.scenario import run_scenario

EXAMPLES = Path(__file__).with_name("examples")


class ScenarioReplayTests(unittest.TestCase):
    def _engine(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return ArchiveEngine(Path(tmp.name) / "archive")

    def _by_kind(self, receipts, kind):
        return [r for r in receipts if r["kind"] == kind]

    def test_revive_with_one_piece_scenario(self):
        engine = self._engine()
        receipts = run_scenario(engine, EXAMPLES / "revive-with-one-piece.jsonl")
        results = {(r["at"], r["kind"]): r for r in receipts}

        # 三片落齐并承诺
        self.assertTrue(results[(2, "finish")]["ok"])
        # a 盘故障后首次读取仍可恢复（b+c）
        first = results[(4, "read")]
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["bytes"], "10203040")
        self.assertEqual(first["result"]["used_indices"], [1, 2])
        # b 再故障后第二次读取受控失败，且标明缺失维度
        lost = results[(6, "read")]
        self.assertFalse(lost["ok"])
        self.assertIn("缺失维度", lost["error"])
        # 巡检能查到对象与缺片
        inspect = results[(7, "inspect")]
        self.assertTrue(inspect["ok"])
        view = inspect["result"]["revs"][1]["layouts"][1]
        self.assertFalse(view["compliant"])
        self.assertTrue(view["reasons"])

    def test_domain_collision_scenario_keeps_old_layout_and_honors_lease(self):
        engine = self._engine()
        receipts = run_scenario(
            engine, EXAMPLES / "new-layout-collides-in-domain.jsonl"
        )
        results = {(r["at"], r["kind"], i): r for i, r in enumerate(receipts)}

        # 第一次 finish（at=6）：wrong(Y) 与 b(Y) 同域，拒绝承诺
        first_finish = next(r for r in receipts if r["kind"] == "finish" and not r["ok"])
        self.assertIn("故障域", first_finish["error"])

        # 重启后状态还原：布局1 committed，布局2 仍 building
        restart = next(r for r in receipts if r["kind"] == "restart")
        self.assertTrue(restart["ok"])
        self.assertEqual(restart["result"]["committed_layouts"], 1)
        self.assertEqual(restart["result"]["building_layouts"], 1)

        # spare(W) 补齐索引0，三域隔离，第二次 finish 成功；
        # 全程共两次成功承诺：布局1 初次承诺 + 布局2 修复承诺
        ok_finishes = [r for r in receipts if r["kind"] == "finish" and r["ok"]]
        self.assertEqual([r["result"]["layout"] for r in ok_finishes], [1, 2])

        # 旧持有者未释放前 retire 被拒；释放后再次 retire 成功
        retires = [r for r in receipts if r["kind"] == "retire"]
        self.assertFalse(retires[0]["ok"])
        self.assertIn("占用", retires[0]["error"])
        self.assertTrue(retires[1]["ok"])

        # 旧读者在释放前始终绑定布局1
        read = next(r for r in receipts if r["kind"] == "read")
        self.assertEqual(read["result"]["layout"], 1)


if __name__ == "__main__":
    unittest.main()
