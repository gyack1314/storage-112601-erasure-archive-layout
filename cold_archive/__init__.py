"""2+1 XOR 冷归档存储：本地盘仿真、版本化分片、修复与布局迁移。"""

from .coding import K, PARITY_INDEX, digest, encode, rebuild
from .engine import ArchiveEngine
from .errors import ArchiveError

__all__ = [
    "ArchiveEngine",
    "ArchiveError",
    "K",
    "PARITY_INDEX",
    "digest",
    "encode",
    "rebuild",
]
