"""冷归档存储命令行入口。

所有命令输出 JSON 回执；违反约束时输出 {"ok": false, "error": ...} 并以
状态码 1 退出，方便值班脚本与演示流水线解析。
"""

import argparse
import json
import sys
from pathlib import Path

from . import coding
from .engine import ArchiveEngine
from .errors import ArchiveError
from .scenario import run_scenario


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _parse_targets(raw: str | None) -> dict[int, str] | None:
    if raw is None:
        return None
    targets = {}
    for part in raw.split(","):
        index, disk = part.split("=", 1)
        targets[int(index)] = disk
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cold-archive", description="2+1 XOR 冷归档存储（本地盘仿真）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化归档目录与事件日志")
    p_init.add_argument("root")

    p_disk = sub.add_parser("disk", help="登记维修人员发布的盘（故障域+可用容量）")
    p_disk.add_argument("root")
    p_disk.add_argument("--name", required=True)
    p_disk.add_argument("--domain", required=True)
    p_disk.add_argument("--free", required=True, type=int)

    p_disks = sub.add_parser("disks", help="查看所有盘与容量账")
    p_disks.add_argument("root")

    p_fault = sub.add_parser("fault", help="模拟整盘损坏，移入故障区域")
    p_fault.add_argument("root")
    p_fault.add_argument("--disk", required=True)

    p_put = sub.add_parser("put", help="写入对象（自动编码落盘并原子承诺）")
    p_put.add_argument("root")
    p_put.add_argument("--key", required=True)
    p_put.add_argument("--rev", required=True, type=int)
    p_put.add_argument("--hex", required=True, dest="hexdata", help="短十六进制字符串")
    p_put.add_argument("--digest", help="客户端原内容摘要；缺省按实际字节计算")

    p_get = sub.add_parser("download", help="绑定已承诺布局下载，不指定则取最新承诺")
    p_get.add_argument("root")
    p_get.add_argument("--holder", required=True)
    p_get.add_argument("--key", required=True)
    p_get.add_argument("--rev", required=True, type=int)
    p_get.add_argument("--layout", type=int, default=None)

    p_release = sub.add_parser("release", help="结束读请求并释放介质租约")
    p_release.add_argument("root")
    p_release.add_argument("--holder", required=True)

    p_migrate = sub.add_parser("migrate", help="从旧布局重建并发布新布局（布局迁移/修复）")
    p_migrate.add_argument("root")
    p_migrate.add_argument("--key", required=True)
    p_migrate.add_argument("--rev", required=True, type=int)
    p_migrate.add_argument("--to", required=True, type=int)
    p_migrate.add_argument("--target", help="显式目标，形如 0=da,1=db,2=dc")

    p_retire = sub.add_parser("retire", help="回收旧布局介质（租约未结束会拒绝）")
    p_retire.add_argument("root")
    p_retire.add_argument("--key", required=True)
    p_retire.add_argument("--rev", required=True, type=int)
    p_retire.add_argument("--layout", required=True, type=int)

    p_inspect = sub.add_parser("inspect", help="查询布局、缺片与不合规原因")
    p_inspect.add_argument("root")
    p_inspect.add_argument("--key", required=True)

    p_replay = sub.add_parser("replay", help="按 JSONL 契约回放演练场景")
    p_replay.add_argument("root")
    p_replay.add_argument("scenarios", nargs="+")
    p_replay.add_argument("--stop-on-error", action="store_true")

    p_demo = sub.add_parser("demo", help="端到端演示（可恢复读取+无法恢复受控结果+迁移）")
    p_demo.add_argument("root")
    p_demo.add_argument("--hex", dest="hexdata", help="自定义演示对象的短十六进制字符串")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        Path(args.root).mkdir(parents=True, exist_ok=True)
        engine = ArchiveEngine(args.root)
        _print({"ok": True, "root": str(Path(args.root).resolve()), "wal": str(engine.catalog.wal_path)})
        return 0

    engine = ArchiveEngine(args.root)
    try:
        if args.command == "disk":
            result = engine.handle_disk(
                {"disk": args.name, "domain": args.domain, "free": args.free}
            )
        elif args.command == "disks":
            result = {
                "disks": [
                    {
                        "disk": s.name,
                        "domain": s.domain,
                        "free": s.free,
                        "faulted": s.faulted,
                    }
                    for s in sorted(engine.catalog.disks.values(), key=lambda x: x.name)
                ]
            }
        elif args.command == "fault":
            result = engine.handle_fault({"disk": args.disk})
        elif args.command == "put":
            source = bytes.fromhex(args.hexdata)
            digest = args.digest or coding.digest(source)
            result = engine.put(args.key, args.rev, source, digest)
        elif args.command == "download":
            result = engine.download(args.holder, args.key, args.rev, args.layout)
        elif args.command == "release":
            result = engine.handle_release({"holder": args.holder})
        elif args.command == "migrate":
            result = engine.migrate(
                args.key, args.rev, args.to, _parse_targets(args.target)
            )
        elif args.command == "retire":
            result = engine.handle_retire(
                {"key": args.key, "rev": args.rev, "layout": args.layout}
            )
        elif args.command == "inspect":
            result = engine.handle_inspect({"key": args.key})
        elif args.command == "replay":
            result = {"receipts": []}
            for scenario in args.scenarios:
                # 每个演练场景使用独立子归档，避免同名对象跨场景污染
                stem = Path(scenario).stem
                scenario_root = Path(args.root) / "replays" / stem
                scenario_engine = ArchiveEngine(scenario_root)
                result["receipts"].append(
                    {
                        "scenario": scenario,
                        "archive": str(scenario_root),
                        "events": run_scenario(
                            scenario_engine, scenario, stop_on_error=args.stop_on_error
                        ),
                    }
                )
        elif args.command == "demo":
            from .demo import run_demo

            result = run_demo(engine, args.hexdata)
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except ArchiveError as error:
        _print({"ok": False, "command": args.command, "error": str(error)})
        return 1
    _print({"ok": True, "command": args.command, "result": result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
