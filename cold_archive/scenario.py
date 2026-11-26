"""按 examples/ 的 JSONL 契约顺序回放消息，输出可追溯回执。

回放器自带消息校验，不依赖仓库根目录的演练资料读取器，保证包可独立安装。
单条消息失败时记录错误回执并按策略决定是否继续：演练中“故意失败的承诺”
（例如域冲突）需要继续观察后续消息，因此默认收集错误而不中断。
"""

import json
from pathlib import Path

from .engine import ArchiveEngine
from .errors import ArchiveError

DISPATCH = {
    "disk": "handle_disk",
    "object": "handle_object",
    "piece": "handle_piece",
    "finish": "handle_finish",
    "fault": "handle_fault",
    "read": "handle_read",
    "release": "handle_release",
    "move": "handle_move",
    "retire": "handle_retire",
    "restart": "handle_restart",
    "inspect": "handle_inspect",
}


def iter_messages(path: Path):
    previous = -1
    with path.open(encoding="utf-8") as source:
        for line_number, raw in enumerate(source, 1):
            if not raw.strip():
                continue
            row = json.loads(raw)
            if set(row) != {"at", "kind", "record"}:
                raise ValueError(f"{path.name} 第 {line_number} 行：消息需要 at、kind 和 record 字段")
            timestamp = row["at"]
            if type(timestamp) is not int or timestamp < 0 or timestamp < previous:
                raise ValueError(f"{path.name} 第 {line_number} 行：到达秒点非法或倒退")
            previous = timestamp
            yield row


def run_scenario(engine: ArchiveEngine, path: str | Path, stop_on_error: bool = False):
    """回放一个场景文件，返回回执列表。"""
    path = Path(path)
    receipts = []
    for row in iter_messages(path):
        at, kind, record = row["at"], row["kind"], row["record"]
        handler = getattr(engine, DISPATCH[kind])
        receipt = {"at": at, "kind": kind}
        try:
            receipt["ok"] = True
            receipt["result"] = handler(record)
        except ArchiveError as error:
            receipt["ok"] = False
            receipt["error"] = str(error)
            if stop_on_error:
                raise
        receipts.append(receipt)
    return receipts
