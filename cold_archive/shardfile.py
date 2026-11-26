"""分片文件格式与本地盘仿真。

每个分片是一个自描述文件：固定魔数 + JSON 头（对象、版本、布局、索引、
所在盘、分片字节摘要）+ 原始片字节。读取时重新摘要并核对头部绑定，
盘上字节被改写（坏片）会在此处被识别。
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import BadPiece, FullDisk, UnknownDisk

MAGIC = b"COLDARCH1\n"


@dataclass(frozen=True)
class ShardHeader:
    key: str
    rev: int
    layout: int
    index: int
    disk: str
    piece_digest: str

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "key": self.key,
                "rev": self.rev,
                "layout": self.layout,
                "index": self.index,
                "disk": self.disk,
                "piece_digest": self.piece_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "ShardHeader":
        data = json.loads(raw.decode("utf-8"))
        return cls(**data)


def piece_digest(piece: bytes) -> str:
    # 头部空间受限（演练盘容量按字节计），片内保存 64 位截短摘要；
    # 原内容完整 SHA-256 仍由对象元数据与事件日志保存。
    return hashlib.sha256(piece).hexdigest()[:16]


def shard_relative_path(key: str, rev: int, layout: int, index: int) -> Path:
    safe_key = key.replace("/", "_")
    return Path(safe_key) / f"rev{rev}" / f"layout{layout}" / f"piece{index}.shard"


class DiskStore:
    """以本地目录模拟一块归档盘，按登记容量执行占用与预留。"""

    def __init__(self, root: Path, name: str, domain: str, free: int):
        self.root = root
        self.name = name
        self.domain = domain
        self.free = free
        self.faulted = False
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str, rev: int, layout: int, index: int) -> Path:
        return self.root / shard_relative_path(key, rev, layout, index)

    def write_shard(
        self,
        key: str,
        rev: int,
        layout: int,
        index: int,
        piece: bytes,
    ) -> Path:
        if self.faulted:
            raise UnknownDisk(f"盘 {self.name} 已故障，无法写入")
        target = self.path_for(key, rev, layout, index)
        header = ShardHeader(
            key, rev, layout, index, self.name, piece_digest(piece)
        )
        head = header.to_json()
        if not target.exists():
            # 容量账只计分片数据字节：维修人员发布的 free 是片数据容量，
            # 魔数/JSON 头属于引擎自身元数据开销，不占该容量。
            if len(piece) > self.free:
                raise FullDisk(
                    f"盘 {self.name} 可用容量 {self.free} 不足片字节 {len(piece)}"
                )
            self.free -= len(piece)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with tmp.open("wb") as handle:
            handle.write(MAGIC)
            handle.write(head)
            handle.write(b"\n")
            handle.write(piece)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return target

    def read_shard(self, key: str, rev: int, layout: int, index: int) -> bytes:
        target = self.path_for(key, rev, layout, index)
        if not target.exists():
            raise BadPiece(f"盘 {self.name} 上缺少 {target.name}")
        raw = target.read_bytes()
        if not raw.startswith(MAGIC):
            raise BadPiece(f"盘 {self.name} 上的分片魔数损坏")
        body = raw[len(MAGIC) :]
        head_raw, _, payload = body.partition(b"\n")
        header = ShardHeader.from_json(head_raw)
        if (header.key, header.rev, header.layout, header.index, header.disk) != (
            key,
            rev,
            layout,
            index,
            self.name,
        ):
            raise BadPiece(f"盘 {self.name} 上的分片头部绑定与请求不符")
        if piece_digest(payload) != header.piece_digest:
            raise BadPiece(f"盘 {self.name} 上的分片摘要不符，按坏片处理")
        return payload

    def remove_shard(self, key: str, rev: int, layout: int, index: int) -> None:
        target = self.path_for(key, rev, layout, index)
        if target.exists():
            raw = target.read_bytes()
            # 只退还片数据部分的容量账
            _, _, payload = raw[len(MAGIC):].partition(b"\n")
            self.free += len(payload)
            target.unlink()

    def fault(self) -> None:
        """模拟整盘损坏：目录保留但标记不可用，读取一律失败。"""
        self.faulted = True
