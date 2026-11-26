# 冷归档存储：2+1 纠删分片、修复与冗余布局切换

字节以十六进制记录。布局 2+1 用同长两个数据片与其逐位 XOR 校验片，
原始字节长度随对象元数据保存以解释补零。对象承诺要求三个索引各在独立
故障域中，读取最低需要两个正确且不同索引的分片；相同片号的副本只计一个
维度，绝不充当两份不同分片。

## 架构

```
cold_archive/
  coding.py      2+1 XOR 编码/重建、原内容 SHA-256 摘要
  shardfile.py   分片文件格式（魔数+JSON头+片字节）与本地盘仿真、容量账
  catalog.py     事件日志（WAL）+ 内存目录：对象版本、布局代次、分片位置、租约
  engine.py      协调引擎：11 种契约消息、合规判定、读取恢复、迁移工作流
  scenario.py    JSONL 演练场景回放器
  demo.py        端到端受控演示
  cli.py         命令行入口（JSON 回执）
```

### 关键正确性规则

- **原子承诺**：对象写入先以 `building` 打开布局，三片全部落盘且通过
  三故障域隔离检查后才 `committed`；任何一步失败，客户端都看不到半成品。
- **版本绑定**：每个分片文件头绑定 key/rev/layout/index/disk 与片摘要；
  读取重新摘要核对，字节被改写即按坏片记账剔除，重建结果再核对原摘要。
- **读租约**：`read` 可显式绑定已打开的布局代次，新布局上线不会悄悄换源
  或混片；租约记录介质路径，`release` 前 `retire` 一律拒绝。
- **冗余合规**：数据片与校验片落同一故障域，即使字节正确也不能承诺；
  新布局未独立可恢复前，旧片全部保留。
- **代次纪律**：重复回报幂等不计新片；向已承诺/已回收代次的回报按旧代次
  拒绝，备用片不会被误计成新可用片。
- **中断恢复**：所有状态变化先追加 `events.jsonl` 再改内存，协调端
  `restart` 重放日志，`building` 与 `committed` 布局、读租约原样还原。

## 输入消息契约（examples/*.jsonl）

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind
与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。

- **disk**　disk、domain 和可用容量 free 字节。
- **object**　key/rev 的源 bytes 十六进制。元数据保存原长度与源摘要。
- **piece**　回报 key/rev、layout、index、disk 和观察到的 hex。同布局同
  索引可能存在多份同内容副本，读恢复和布局承诺均只计一个索引。
- **finish**　尝试提交布局 key/rev/layout。
- **fault**　报告明确 disk 此后不可用。
- **read**　holder 打开指定 layout；领用时需要实际可重建。
- **release**　关闭 holder。
- **move**　准备新布局 to，引用 from。
- **retire**　回收明确 layout。
- **restart**　协调端重启。
- **inspect**　查询对象。

## 使用

运行环境为 Python 3.11，只使用标准库。在仓库根目录执行：

```bash
# 运行全部测试（含原始契约读取器与 27 项引擎/场景/CLI 测试）
python3 -m unittest discover -v
python3 scenario_reader.py examples

# 初始化归档
python3 -m cold_archive.cli init ./archive

# 登记三块不同故障域的盘
python3 -m cold_archive.cli disk ./archive --name da --domain X --free 4096
python3 -m cold_archive.cli disk ./archive --name db --domain Y --free 4096
python3 -m cold_archive.cli disk ./archive --name dc --domain Z --free 4096

# 写入短十六进制对象（自动编码、落盘、原子承诺，回执含原摘要）
python3 -m cold_archive.cli put ./archive --key log --rev 1 --hex 10203040

# 巡检当前布局、缺片与不合规原因
python3 -m cold_archive.cli inspect ./archive --key log

# 模拟盘损坏
python3 -m cold_archive.cli fault ./archive --disk da

# 绑定布局下载（坏一片仍可恢复；坏到只剩一个维度会返回缺失维度错误码1）
python3 -m cold_archive.cli download ./archive --holder ops --key log --rev 1 --layout 1
python3 -m cold_archive.cli release ./archive --holder ops

# 布局迁移/修复：从仍可恢复的旧布局重建，自动选三域隔离健康盘
python3 -m cold_archive.cli migrate ./archive --key log --rev 1 --to 2
# 也可显式指定：--target 0=da,1=db,2=dc（同域会被拒绝）

# 回放契约演练场景（每个场景使用独立子归档）
python3 -m cold_archive.cli replay ./archive \
  examples/revive-with-one-piece.jsonl \
  examples/new-layout-collides-in-domain.jsonl

# 端到端演示：一次可恢复读取、一次无法恢复的受控结果、域冲突、
# 重复/旧代次回报、静默坏片、迁移、重启恢复与租约保护
python3 -m cold_archive.cli demo ./archive --hex a1b2c3d4e5
```

所有命令输出 JSON 回执；违反约束时输出 `{"ok": false, "error": ...}`
并以状态码 1 退出。事件日志位于 `<archive>/events.jsonl`，分片位于
`<archive>/disks/<disk>/<key>/rev<r>/layout<l>/piece<i>.shard`。

## 示例场景

- `revive-with-one-piece.jsonl`：两片损坏后明确报告缺失维度的受控失败。
- `new-layout-collides-in-domain.jsonl`：新布局数据片与校验片同域被拒、
  协调端重启区分 building/committed、租约未结束禁止回收旧介质。
