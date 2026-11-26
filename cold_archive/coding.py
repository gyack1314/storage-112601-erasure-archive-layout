"""2+1 纠删码：两个等长数据片加一份逐位 XOR 校验片。

原始字节按长度平分；奇数长度时前半段多一个字节，不足处补零后再做 XOR，
原始长度随对象元数据保存，重建时据此去掉补零。
"""

from .errors import BadPiece

K = 2
PARITY_INDEX = 2
REQUIRED_INDICES = (0, 1, 2)


def digest(data: bytes) -> str:
    """返回原始内容的 SHA-256 十六进制摘要。"""
    import hashlib

    return hashlib.sha256(data).hexdigest()


def encode(source: bytes) -> list[bytes]:
    """把原内容编码为 [d0, d1, parity] 三片，数据片等长。"""
    if not source:
        return [b"", b"", b""]
    half = (len(source) + 1) // 2
    d0 = source[:half]
    d1 = source[half:]
    if len(d1) < half:
        d1 = d1 + b"\x00" * (half - len(d1))
    parity = bytes(a ^ b for a, b in zip(d0, d1, strict=True))
    return [d0, d1, parity]


def rebuild(available: dict[int, bytes], original_length: int) -> bytes:
    """从 {索引: 片字节} 重建原内容。

    至少需要两个不同索引；校验片与任一数据片可反推出另一数据片。
    重建结果由调用方再与原摘要核对，本函数只负责数学还原与长度裁剪。
    """
    indices = set(available)
    if len(indices) < 2:
        raise BadPiece("可用分片不足两个不同索引，无法重建")
    if 0 in available and 1 in available:
        d0, d1 = available[0], available[1]
    elif 0 in available and PARITY_INDEX in available:
        d0 = available[0]
        d1 = bytes(a ^ b for a, b in zip(d0, available[PARITY_INDEX], strict=True))
    elif 1 in available and PARITY_INDEX in available:
        d1 = available[1]
        d0 = bytes(a ^ b for a, b in zip(d1, available[PARITY_INDEX], strict=True))
    else:
        raise BadPiece("可用分片不足两个不同索引，无法重建")
    return (d0 + d1)[:original_length]


def expected_pieces(source: bytes) -> dict[int, bytes]:
    """对象提交时计算的权威分片字节，作为所有回报的核对基准。"""
    pieces = encode(source)
    return {index: piece for index, piece in enumerate(pieces)}
