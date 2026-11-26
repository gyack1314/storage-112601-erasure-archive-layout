"""冷归档引擎的异常层次。

所有违反输入契约或安全约束的调用都抛出 :class:`ArchiveError` 的子类，
CLI 与场景回放器据此输出结构化回执，而不是让栈回溯泄漏给值班人员。
"""


class ArchiveError(Exception):
    """归档引擎可预期错误的基类。"""


class UnknownDisk(ArchiveError):
    """引用了未登记或已故障的盘。"""


class FullDisk(ArchiveError):
    """目标盘剩余容量不足以容纳该分片。"""


class UnknownObject(ArchiveError):
    """对象 key/rev 尚未写入或已不存在。"""


class UnknownLayout(ArchiveError):
    """引用的布局代次不存在。"""


class BadPiece(ArchiveError):
    """分片字节与该对象该索引在该布局下应有的内容不符。"""


class DuplicateHolder(ArchiveError):
    """同一个读持有者名称被重复领用。"""


class NotRecoverable(ArchiveError):
    """可用分片不足以重建对象，错误消息列出缺失维度。"""


class LayoutNotCompliant(ArchiveError):
    """尝试提交的布局未达到独立恢复与域隔离要求。"""


class LayoutInUse(ArchiveError):
    """仍有读持有者绑定该布局，不能回收旧介质。"""


class StaleGeneration(ArchiveError):
    """回报属于已经结束或不是当前构建中的布局代次。"""


class DuplicateReport(ArchiveError):
    """同一位置的重复回报，不构成新的可用分片。"""
