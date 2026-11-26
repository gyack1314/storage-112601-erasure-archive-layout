"""归档目录：对象版本、布局代次、分片位置、读租约的唯一事实来源。

状态变化一律先追加 JSONL 事件日志（WAL）再更新内存，协调端重启后重放
日志即可区分“正在构建（building）”与“已经承诺（committed）”的布局。
分片字节本身落在各盘目录，事件日志只记录绑定关系与容量账。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (
    DuplicateHolder,
    LayoutInUse,
    StaleGeneration,
    UnknownDisk,
    UnknownLayout,
    UnknownObject,
)
from .shardfile import DiskStore


@dataclass
class ObjectRecord:
    key: str
    rev: int
    source_length: int
    source_digest: str
    pieces: dict[int, str]  # 索引 -> 权威分片字节摘要
    layouts: dict[int, str] = field(default_factory=dict)  # 布局 -> 状态


@dataclass
class PieceRecord:
    """一次分片落地。同一 (布局, 索引) 在不同盘上的多份记录互为副本。"""

    key: str
    rev: int
    layout: int
    index: int
    disk: str
    piece_digest: str


@dataclass
class LeaseRecord:
    holder: str
    key: str
    rev: int
    layout: int
    media_paths: list[str] = field(default_factory=list)


@dataclass
class RetireRecord:
    key: str
    rev: int
    layout: int


class Catalog:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.wal_path = self.root / "events.jsonl"
        self.disks: dict[str, DiskStore] = {}
        self.objects: dict[tuple[str, int], ObjectRecord] = {}
        self.pieces: list[PieceRecord] = []
        self.leases: dict[str, LeaseRecord] = {}
        self.retired: list[RetireRecord] = []
        self._seq = 0
        self._replay()

    # ------------------------------------------------------------------ WAL
    def _append(self, event: dict) -> None:
        self._seq += 1
        event = {"seq": self._seq, **event}
        with self.wal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._apply(event)

    def _replay(self) -> None:
        if not self.wal_path.exists():
            return
        for line in self.wal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            self._seq = event["seq"]
            self._apply(event)

    def _apply(self, event: dict) -> None:
        kind = event["kind"]
        r = event["record"]
        if kind == "disk_registered":
            store = DiskStore(
                self.root / "disks" / r["disk"], r["disk"], r["domain"], r["free"]
            )
            store.faulted = r.get("faulted", False)
            self.disks[r["disk"]] = store
        elif kind == "disk_faulted":
            self.disks[r["disk"]].fault()
        elif kind == "object_written":
            self.objects[(r["key"], r["rev"])] = ObjectRecord(
                key=r["key"],
                rev=r["rev"],
                source_length=r["source_length"],
                source_digest=r["source_digest"],
                pieces={int(i): d for i, d in r["pieces"].items()},
                layouts={int(l): s for l, s in r["layouts"].items()},
            )
        elif kind == "layout_opened":
            self.objects[(r["key"], r["rev"])].layouts[r["layout"]] = "building"
        elif kind == "piece_reported":
            self.pieces.append(
                PieceRecord(
                    r["key"], r["rev"], r["layout"], r["index"], r["disk"], r["digest"]
                )
            )
        elif kind == "piece_marked_bad":
            self.pieces = [
                p
                for p in self.pieces
                if not (
                    p.key == r["key"]
                    and p.rev == r["rev"]
                    and p.layout == r["layout"]
                    and p.index == r["index"]
                    and p.disk == r["disk"]
                )
            ]
        elif kind == "layout_committed":
            self.objects[(r["key"], r["rev"])].layouts[r["layout"]] = "committed"
        elif kind == "lease_opened":
            self.leases[r["holder"]] = LeaseRecord(
                r["holder"], r["key"], r["rev"], r["layout"], list(r.get("media_paths", []))
            )
        elif kind == "lease_released":
            self.leases.pop(r["holder"], None)
        elif kind == "layout_retired":
            self.retired.append(RetireRecord(r["key"], r["rev"], r["layout"]))
            self.objects[(r["key"], r["rev"])].layouts[r["layout"]] = "retired"
        else:  # pragma: no cover - 防御性分支
            raise ValueError(f"未知事件类型 {kind}")

    # ------------------------------------------------------------- 磁盘管理
    def register_disk(self, disk: str, domain: str, free: int) -> dict:
        if disk in self.disks:
            store = self.disks[disk]
            if store.domain != domain:
                raise UnknownDisk(f"盘 {disk} 已登记在故障域 {store.domain}")
            store.free = free
        else:
            self._append(
                {
                    "kind": "disk_registered",
                    "record": {
                        "disk": disk,
                        "domain": domain,
                        "free": free,
                        "faulted": False,
                    },
                }
            )
        store = self.disks[disk]
        return {
            "disk": disk,
            "domain": store.domain,
            "free": store.free,
            "faulted": store.faulted,
        }

    def require_disk(self, disk: str) -> DiskStore:
        if disk not in self.disks:
            raise UnknownDisk(f"盘 {disk} 未登记")
        store = self.disks[disk]
        if store.faulted:
            raise UnknownDisk(f"盘 {disk} 已处于故障域隔离区，不可用")
        return store

    def fault_disk(self, disk: str) -> dict:
        if disk not in self.disks:
            raise UnknownDisk(f"盘 {disk} 未登记")
        if not self.disks[disk].faulted:
            self._append({"kind": "disk_faulted", "record": {"disk": disk}})
        return {"disk": disk, "faulted": True}

    # ------------------------------------------------------------- 对象/布局
    def write_object(
        self,
        key: str,
        rev: int,
        source: bytes,
        source_digest: str,
        piece_digests: dict[int, str],
        layout: int,
    ) -> None:
        if (key, rev) in self.objects:
            raise UnknownObject(f"对象 {key} 版本 {rev} 已存在")
        self._append(
            {
                "kind": "object_written",
                "record": {
                    "key": key,
                    "rev": rev,
                    "source_length": len(source),
                    "source_digest": source_digest,
                    "pieces": {str(i): d for i, d in piece_digests.items()},
                    "layouts": {str(layout): "building"},
                },
            }
        )

    def open_layout(self, key: str, rev: int, layout: int) -> None:
        obj = self.require_object(key, rev)
        if layout in obj.layouts:
            raise StaleGeneration(f"布局 {layout} 已存在，状态为 {obj.layouts[layout]}")
        self._append(
            {
                "kind": "layout_opened",
                "record": {"key": key, "rev": rev, "layout": layout},
            }
        )

    def require_object(self, key: str, rev: int) -> ObjectRecord:
        try:
            return self.objects[(key, rev)]
        except KeyError as exc:
            raise UnknownObject(f"对象 {key} 版本 {rev} 不存在") from exc

    def require_layout(self, key: str, rev: int, layout: int) -> ObjectRecord:
        obj = self.require_object(key, rev)
        if layout not in obj.layouts:
            raise UnknownLayout(f"布局代次 {layout} 从未打开")
        return obj

    # ------------------------------------------------------------- 分片账
    def find_piece(
        self, key: str, rev: int, layout: int, index: int, disk: str
    ) -> PieceRecord | None:
        for piece in self.pieces:
            if (
                piece.key == key
                and piece.rev == rev
                and piece.layout == layout
                and piece.index == index
                and piece.disk == disk
            ):
                return piece
        return None

    def report_piece(
        self,
        key: str,
        rev: int,
        layout: int,
        index: int,
        disk: str,
        digest: str,
    ) -> None:
        self._append(
            {
                "kind": "piece_reported",
                "record": {
                    "key": key,
                    "rev": rev,
                    "layout": layout,
                    "index": index,
                    "disk": disk,
                    "digest": digest,
                },
            }
        )

    def mark_piece_bad(
        self, key: str, rev: int, layout: int, index: int, disk: str
    ) -> None:
        if self.find_piece(key, rev, layout, index, disk) is not None:
            self._append(
                {
                    "kind": "piece_marked_bad",
                    "record": {
                        "key": key,
                        "rev": rev,
                        "layout": layout,
                        "index": index,
                        "disk": disk,
                    },
                }
            )

    def pieces_of(self, key: str, rev: int, layout: int) -> list[PieceRecord]:
        return [
            p
            for p in self.pieces
            if p.key == key and p.rev == rev and p.layout == layout
        ]

    # ------------------------------------------------------------- 承诺/租约
    def commit_layout(self, key: str, rev: int, layout: int) -> None:
        obj = self.require_layout(key, rev, layout)
        if obj.layouts[layout] == "committed":
            raise StaleGeneration(f"布局 {layout} 已经承诺")
        self._append(
            {
                "kind": "layout_committed",
                "record": {"key": key, "rev": rev, "layout": layout},
            }
        )

    def open_lease(
        self, holder: str, key: str, rev: int, layout: int, media_paths: list[str]
    ) -> None:
        if holder in self.leases:
            raise DuplicateHolder(f"持有者 {holder} 已绑定其他读请求")
        self._append(
            {
                "kind": "lease_opened",
                "record": {
                    "holder": holder,
                    "key": key,
                    "rev": rev,
                    "layout": layout,
                    "media_paths": media_paths,
                },
            }
        )

    def release_lease(self, holder: str) -> LeaseRecord:
        if holder not in self.leases:
            raise DuplicateHolder(f"持有者 {holder} 没有打开的读请求")
        record = self.leases[holder]
        self._append({"kind": "lease_released", "record": {"holder": holder}})
        return record

    def lease_uses_layout(self, key: str, rev: int, layout: int) -> bool:
        return any(
            lease.key == key
            and lease.rev == rev
            and lease.layout == layout
            for lease in self.leases.values()
        )

    def retire_layout(self, key: str, rev: int, layout: int) -> None:
        obj = self.require_layout(key, rev, layout)
        if obj.layouts[layout] == "retired":
            raise UnknownLayout(f"布局 {layout} 已回收")
        if self.lease_uses_layout(key, rev, layout):
            holders = [
                h
                for h, lease in self.leases.items()
                if lease.key == key and lease.rev == rev and lease.layout == layout
            ]
            raise LayoutInUse(f"布局 {layout} 仍被持有者 {holders} 占用，禁止释放介质")
        for piece in self.pieces_of(key, rev, layout):
            store = self.disks.get(piece.disk)
            if store is not None:
                store.remove_shard(key, rev, layout, piece.index)
        self._append(
            {
                "kind": "layout_retired",
                "record": {"key": key, "rev": rev, "layout": layout},
            }
        )
