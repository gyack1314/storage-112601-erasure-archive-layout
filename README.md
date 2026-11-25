# 纠删归档的分片修复与冗余布局切换

字节以十六进制记录。布局 2+1 用同长两个数据片与其逐位 XOR，示例存储原字节长度以解释补零。对象承诺要求三个索引各在独立故障域中，读取最低需要两个正确且不同索引。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **disk**　disk、domain 和可用容量 free 字节。
- **object**　key/rev 的源 bytes 十六进制。 元数据保存原长度与源摘要，样例源码字节仅用来合成可核对的输入。
- **piece**　回报 key/rev、layout、index、disk 和观察到的 hex。 同布局同索引可能存在多份同内容副本，读恢复和布局承诺均只计一个索引，域约束须实际选出对应位置。
- **finish**　尝试提交布局 key/rev/layout。
- **fault**　报告明确 disk 此后不可用。
- **read**　holder 打开指定 layout；领用时需要实际可重建。
- **release**　关闭 holder。
- **move**　准备新布局 to，引用 from。
- **retire**　回收明确 layout。
- **restart**　协调端重启。
- **inspect**　查询对象。

## 示例

- revive-with-one-piece.jsonl
- new-layout-collides-in-domain.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。
