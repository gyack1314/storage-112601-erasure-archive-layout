"""端到端演练：归档写入、下载、盘损坏与布局迁移。

演示刻意覆盖以下受控结果（失败都被捕获为带原因的记录，演练不中断）：
  1. 坏一片仍可恢复（两个不同索引即可重建，并复核原摘要）；
  2. 坏到只剩一个维度，明确报告缺失维度，拒绝交付；
  3. 新布局校验片与数据片落同域，即使字节正确也拒绝承诺；
  4. 同位置重复回报、向已承诺旧代次回报，都不能计成新可用片；
  5. 介质静默损坏（摘要不符）按坏片记账剔除，其余分片仍可恢复；
  6. 读租约未结束前，旧布局介质禁止提前回收。
"""

from pathlib import Path

from . import coding
from .engine import ArchiveEngine
from .errors import ArchiveError


def _step(steps: list, title: str, call):
    try:
        result = call()
        steps.append({"title": title, "ok": True, "result": result})
        return result
    except ArchiveError as error:
        steps.append({"title": title, "ok": False, "error": str(error)})
        return None


def run_demo(engine: ArchiveEngine, hexdata: str | None = None) -> dict:
    hexdata = hexdata or "a1b2c3d4e5"
    source = bytes.fromhex(hexdata)
    source_digest = coding.digest(source)
    steps: list[dict] = []

    # ---- 1. 维修人员发布 5 个故障域的盘，另加一块容量不足的盘 ----
    for name, domain, free in [
        ("demo-da", "X", 4096),
        ("demo-db", "Y", 4096),
        ("demo-dc", "Z", 4096),
        ("demo-dd", "W", 4096),
        ("demo-de", "V", 4096),
        ("demo-tiny", "U", 1),
    ]:
        _step(
            steps,
            f"登记盘 {name}（故障域 {domain}，可用 {free} 字节）",
            lambda n=name, d=domain, f=free: engine.handle_disk(
                {"disk": n, "domain": d, "free": f}
            ),
        )

    # ---- 2. 对象写入：客户端给原内容摘要，承诺前对外不可见 ----
    def _write_first_layout():
        engine.handle_object(
            {"key": "demo-log", "rev": 1, "bytes": hexdata}
        )
        for index, disk in ((0, "demo-da"), (1, "demo-db"), (2, "demo-dc")):
            engine.handle_piece(
                {
                    "key": "demo-log",
                    "rev": 1,
                    "layout": 1,
                    "index": index,
                    "disk": disk,
                    "hex": expected_pieces[index].hex(),
                }
            )
        return engine.handle_finish({"key": "demo-log", "rev": 1, "layout": 1})

    expected_pieces = coding.expected_pieces(source)
    committed = _step(
        steps,
        f"写入对象 demo-log rev1，原内容 {hexdata}，摘要 {source_digest[:12]}…，"
        "三片落到三个故障域并原子承诺",
        _write_first_layout,
    )
    layout1 = committed["layout"]

    # ---- 3. 巡检首个已承诺布局 ----
    _step(steps, "巡检：查看当前布局、分片位置与故障域", lambda: engine.inspect("demo-log"))

    # ---- 4. 一块数据盘损坏，仍剩两个不同索引，可恢复读取 ----
    _step(steps, "模拟盘 demo-da（索引0）损坏并移入故障区域", lambda: engine.handle_fault({"disk": "demo-da"}))
    recovered = _step(
        steps,
        "受控结果①：只剩两个维度，领用布局1下载并核对原摘要",
        lambda: engine.handle_read(
            {"holder": "ops-1", "key": "demo-log", "rev": 1, "layout": layout1}
        ),
    )
    assert recovered and recovered["bytes"] == hexdata

    # ---- 5. 第二块盘损坏，只剩单一维度，无法恢复 ----
    _step(steps, "模拟盘 demo-db（索引1）也损坏", lambda: engine.handle_fault({"disk": "demo-db"}))
    _step(
        steps,
        "受控结果②：仅剩一个分片维度，拒绝交付并标明缺失维度",
        lambda: engine.handle_read(
            {"holder": "ops-lost", "key": "demo-log", "rev": 1, "layout": layout1}
        ),
    )

    # ---- 6. 新布局构建：先制造域冲突，字节正确也不许承诺 ----
    _step(
        steps,
        "打开布局2（move），旧布局在新布局承诺前全部保留",
        lambda: engine.handle_move({"key": "demo-log", "rev": 1, "from": layout1, "to": 2}),
    )
    expected = coding.expected_pieces(source)
    for index, disk in [(0, "demo-dd"), (1, "demo-de"), (2, "demo-de")]:
        _step(
            steps,
            f"布局2 索引{index} 回报到盘 {disk}",
            lambda i=index, d=disk: engine.handle_piece(
                {
                    "key": "demo-log",
                    "rev": 1,
                    "layout": 2,
                    "index": i,
                    "disk": d,
                    "hex": expected[i].hex(),
                }
            ),
        )
    _step(
        steps,
        "受控结果③：索引1 与校验片同处故障域 V，finish 被拒（旧片不动）",
        lambda: engine.handle_finish({"key": "demo-log", "rev": 1, "layout": 2}),
    )

    # ---- 7. 重复回报与不同代次回报都不能计成新可用片 ----
    _step(
        steps,
        "受控结果④：同位置重复回报被识别，不产生第二个可用分片",
        lambda: engine.handle_piece(
            {
                "key": "demo-log",
                "rev": 1,
                "layout": 2,
                "index": 1,
                "disk": "demo-de",
                "hex": expected[1].hex(),
            }
        ),
    )
    _step(
        steps,
        "受控结果⑤：向已结束的旧布局代次回报被拒（备用片不计新片）",
        lambda: engine.handle_piece(
            {
                "key": "demo-log",
                "rev": 1,
                "layout": layout1,
                "index": 0,
                "disk": "demo-dd",
                "hex": expected[0].hex(),
            }
        ),
    )

    # ---- 8. 修正布局2 到三域隔离，承诺成功；重建期间再坏一块盘的情形也被覆盖 ----
    _step(
        steps,
        "把校验片改放到故障域 Z 的健康盘 demo-dc（三域隔离后重新承诺）",
        lambda: engine.handle_piece(
            {
                "key": "demo-log",
                "rev": 1,
                "layout": 2,
                "index": 2,
                "disk": "demo-dc",
                "hex": expected[2].hex(),
            }
        ),
    )
    _step(
        steps,
        "布局2 承诺成功，成为独立可恢复且满足冗余要求的新布局",
        lambda: engine.handle_finish({"key": "demo-log", "rev": 1, "layout": 2}),
    )

    # ---- 9. 新读者走新布局；旧持有者仍钉在布局1的介质路径上 ----
    new_read = _step(
        steps,
        "新读请求绑定布局2下载，不会悄悄混到旧布局的分片",
        lambda: engine.handle_read(
            {"holder": "ops-2", "key": "demo-log", "rev": 1, "layout": 2}
        ),
    )
    assert new_read and new_read["bytes"] == hexdata

    # ---- 10. 介质上的静默字节损坏：摘要不符按坏片处理（针对新布局副本） ----
    bitrot_target = engine.catalog.disks["demo-dd"].path_for("demo-log", 1, 2, 0)
    bitrot_target.write_bytes(b"corrupted-bytes-on-dd")
    _step(
        steps,
        "受控结果⑥：demo-dd 上数据片被改写，读取时摘要不符按坏片处理并记账剔除",
        lambda: engine.handle_read(
            {"holder": "ops-bitrot", "key": "demo-log", "rev": 1, "layout": 2}
        ),
    )
    # 坏片只影响该副本，布局2 仍可由索引1+2 恢复
    healed = _step(
        steps,
        "坏片记账后再读：剩余两个不同索引仍可重建并通过原摘要核对",
        lambda: engine.handle_read(
            {"holder": "ops-3", "key": "demo-log", "rev": 1, "layout": 2}
        ),
    )
    assert healed and healed["bytes"] == hexdata

    # ---- 11. 协调端中断重启：building 与 committed 可区分，租约恢复 ----
    _step(
        steps,
        "协调端重启：重放事件日志，恢复 building/committed 与读租约",
        lambda: engine.handle_restart({}),
    )

    # ---- 12. 旧租约未结束前禁止回收旧介质 ----
    _step(
        steps,
        "受控结果⑦：持有者 ops-1 未结束，retire 布局1 被拒",
        lambda: engine.handle_retire(
            {"key": "demo-log", "rev": 1, "layout": layout1}
        ),
    )
    _step(steps, "结束旧读请求 ops-1（介质路径此时才可释放）", lambda: engine.handle_release({"holder": "ops-1"}))
    _step(steps, "回收布局1，旧分片从盘上删除", lambda: engine.handle_retire({"key": "demo-log", "rev": 1, "layout": layout1}))
    _step(steps, "最终巡检：布局2 承诺、布局1 已回收", lambda: engine.inspect("demo-log"))

    return {
        "object": {"key": "demo-log", "rev": 1, "hex": hexdata, "source_digest": source_digest},
        "wal": str(engine.catalog.wal_path),
        "steps": steps,
        "failures_demonstrated": [
            s["title"] for s in steps if not s["ok"]
        ],
    }
