# Tick 存储效率设计

> 状态：Planreview re-review 为 `pass-with-risks`；两个 implementation slices 已实现，完整回归通过；Deepreview 未发现实质性问题。设计基线为 `a4488c00`。
> Commit、push、merge、release、deployment 和历史数据清理都是独立边界。

## 目标

在不改变扫描、推荐、通知和实验事实语义的前提下，减少 scheduled Tick 对
`output_runs/<run_id>` 的重复写入，并保持以下约束：

- 不增加 OpenD 或其他 provider 请求；
- 不增加 gzip、数据库或通用存储层；
- 不把额外工作放进候选决策热路径；
- Strategy Lab、Shadow Replay 和 Recommendation Point 继续读取完整、可验证的
  opening candidate 与 required-data 权威事实；
- 新写入优化与历史数据清理保持分离。

## 成功信号

- scheduled Tick 仍只执行一次跨账户 required-data prefetch；同一次完整结果继续先用于
  required-data manifest 封存。
- 内存中的 prefetch 结果保留 `audit[].payload`，但新落盘的每账户
  `required_data_prefetch_summary.json` 仅删除这个嵌套字段。
- 落盘摘要继续保留所有现有外层字段，包括状态、message、symbol、source、耗时、receipt、统计、
  rate-limit、manifest binding、顶层 `errors` 和 audit envelope 中已有的外层错误字段。
- `opening_candidate_snapshot.json` 的解析结果、该 snapshot 自身的 `content_sha256`、校验、
  write-once/readback 和正式实验消费者行为不变；依赖物理文件 hash 的下游 provenance hash
  按新字节自洽更新。
- 固定 fixture 的两类新文件都比当前编码明显更小；opening snapshot 的落盘字节必须精确等于
  约定的紧凑 JSON 编码；payload-dominant prefetch fixture 的实际精简文件必须小于同一完整
  summary 按现有 writer 编码所得字节的一半。
- focused tests、Ruff、完整 pytest 和 `git diff --check` 通过。

真实 run 大小、每日增长量和 Tick 总耗时只能在另行授权的发布升级后观察，不能由源码测试提前
宣称通过。

## 非目标

- 不改 OpenD 获取、缓存、rate-limit、并发或 required-data planning。
- 不改候选归一化、过滤、排序、容量、决策结构或通知文案。
- 不删除 opening snapshot 顶层与 `opening_decision` 之间的重复候选字段。
- 不把 per-account prefetch summary 移到 run-level 共享路径。
- 不给所有 JSON 增加 gzip，也不修改通用 JSON writer。
- 不改 required-data canonical blob、receipt、manifest 或 Formal Corpus schema。
- 不删除、迁移或重写任何历史 run、旧 context、Shadow Replay 或 canonical blob。
- 不在本工作单元中 commit、push、merge、release、deploy 或升级运行环境。

## 当前事实和约束

### 容量证据

2026-08-30 的只读实施输入记录了以下容量快照：运行目录约 19.1 GB，`output_runs` 约
12.7 GB，最近交易日 44 个 run 增长约 596 MB，最近完整 run 约 38.5 MB。旧 Shadow Replay
约 4.65 GB；Formal Corpus 和 canonical required-data blob 已经压缩并保持较小。

这些数字是选择优化目标的时点证据，不是运行时阈值、保留策略或源码验收常量。源码分支不重新
测量或清理生产数据。

### Required-data prefetch

`src/application/tick_account_execution.py` 对所有有效扫描账户只调用一次
`prefetch_required_data()`。返回对象先原样传给 `seal_required_data_snapshot()`；manifest
完成读回和 hash 绑定后，同一 summary 才写入各账户的
`state/required_data_prefetch_summary.json`。

`src/application/multi_tick/required_data_prefetch.py` 在返回前已经从完整
`audit[].payload` 汇总 `fetch_metrics` 和 `run_fetch_summary`。成功并通过校验的 provider facts 已经通过
canonical required-data blob、quote receipt 和 terminal manifest 保存；将其再次展开进每账户摘要
不增加事实权威性。

失败或 partial provider payload 不一定生成 canonical blob/receipt。其嵌套 `meta.error_code`、
`meta.errors` 可能只存在于 `audit[].payload`，而外层 envelope 通常只保留 status、message，只有
rate-limit 会额外提升为外层 `error_code`。这些细分 provider 诊断不是现有持久化摘要合同或实验事实
合同；本工作单元不新增字段提升协议。删除 payload 后仍必须保留所有既有外层错误状态和诊断字段，
但不承诺保留 payload 内部的 provider 细分错误。

当前仓库内消费者的有效读取范围是：

- Daily Brief 使用 `symbols`、`results`、`errors` 和摘要状态生成 data gap；
- runtime status 通用地展示落盘 JSON，不把 `audit[].payload` 当作运行判定；
- receipt、manifest、required-data reader、Recommendation Point、Shadow Replay 和 Formal Corpus
  都从各自 canonical owner 获取权威事实；
- 当前仓库没有读取落盘 prefetch summary 中 `audit[].payload` 的调用者。

仓库外的临时脚本无法由源码搜索证明。此设计明确把 `audit[].payload` 定义为 prefetch 内部计算
材料，而不是持久化摘要合同；历史摘要仍可读取，新 reader 也不得要求该字段存在。

### Opening candidate snapshot

`src/application/opening_candidate_snapshot.py` 先构造和校验完整语义 payload，再用
`canonical_sha256()` 计算 `content_sha256`。该 hash 使用排序、紧凑、数值规范化后的 canonical
JSON，不依赖 snapshot 文件是否带缩进。

当前 `_canonical_json_bytes()` 使用 `indent=2`。loader 先执行 `json.loads()`，再按语义字段和
`content_sha256` 校验，因此紧凑编码不会改变已解析 payload。candidate snapshot manifest 同时绑定
同一 run 的文件字节 hash 与 `content_sha256`：新文件的字节 hash 会按新编码生成并在同一 run 内
读回校验，语义 binding 不变。历史漂亮 JSON 不重写，现有 loader 继续接受。

opening snapshot 中候选 facts 的重复属于 `opening_candidate_snapshot.v1` 语义合同，已经被
Recommendation Point、Shadow Replay 和 Formal Corpus 消费。本工作单元只改变物理编码。

## 选择的设计

### 1. 只投影落盘 prefetch summary

在 `_publish_prefetch_summary_to_accounts()` 的持久化边界创建一个新字典：

- 保留 summary 的所有顶层键和值；
- 对 `audit` 中每个现有对象做浅拷贝，只排除键名 `payload`；
- 不 deep-copy、删改或替换传入的完整内存对象；
- 只生成一次投影，再把相同投影写入各账户的现有路径；
- 不新增 schema、文件、目录、配置项或共享协议。

只删除 `audit[].payload`；`source_snapshot`、`quote_source_receipt`、status、message、error code、
timestamps、duration、execution mode 和其他诊断字段继续落盘。若 summary 写入失败，保留现有
Tick barrier 的失败行为，不增加吞错或降级分支。这里的 error code 指 audit envelope 中已经存在的
外层字段；不从被删除的 provider payload 提升新字段。

顺序保持为：

```text
single cross-account prefetch
  -> full in-memory summary
  -> required-data manifest seal/readback
  -> persisted-summary projection
  -> existing per-account summary paths
  -> account pipelines
```

### 2. Opening snapshot 使用紧凑 JSON

只修改 `opening_candidate_snapshot.py:_canonical_json_bytes()`：

- 保留 `ensure_ascii=False`、`sort_keys=True`、`allow_nan=False` 和末尾换行；
- 删除 `indent=2`；
- 使用 `separators=(",", ":")`。

payload assembly、snapshot `content_sha256` 计算、validator、write-once、loader、readback adoption
和 manifest 逻辑均不修改。新 run 使用紧凑字节；已存在 run 保持原字节，不做就地重写或重新封存。

hash 层级必须分别验收：相同语义 payload 的 opening snapshot `content_sha256` 保持不变；snapshot
文件字节 `sha256` 必然变化，因此新 candidate manifest 的 owner file binding、manifest
`content_sha256` 和文件 hash 也会变化。Recommendation Point 绑定 terminal manifest 的物理 hash，
所以其 provenance 及 point `content_sha256` 也可能随之变化。这是预期的来源链更新，不是候选决策
事实变化；测试应证明每一层 binding 自洽，不断言所有下游 hash 数值不变。

状态顺序保持为：

```text
assemble semantic payload
  -> compute content_sha256
  -> validate
  -> compact encode
  -> write once or adopt identical bytes
  -> load and validate
  -> candidate manifest binds file hash + content_sha256
```

## Owner 和合同

| Owner | 本工作单元的责任 |
|---|---|
| `src/application/tick_account_execution.py` | 在 manifest 封存后投影并发布精简 prefetch summary |
| `src/application/opening_candidate_snapshot.py` | 保持语义 payload，改用紧凑文件编码 |
| `src/application/multi_tick/required_data_prefetch.py` | 不修改；继续在内存用完整 payload 计算指标 |
| `src/application/required_data_snapshot.py` | 不修改；继续消费完整 prefetch 结果并封存 canonical facts |
| `src/application/candidate_snapshot_manifest.py` | 不修改；继续绑定新文件字节 hash 与既有语义 hash |
| `src/application/daily_decision_brief_service.py` | 不修改；继续从精简摘要读取现有状态字段 |
| Recommendation Point / Shadow Replay / Formal Corpus | 不修改；继续校验更新后的 provenance hash，并从 canonical owner 消费事实 |

不改变 domain strategy、public CLI、runtime config、通知、ledger、trade 或 broker 合同。

## 失败和兼容行为

| 场景 | 必须保持的行为 |
|---|---|
| Prefetch 或 manifest seal 失败 | 不发布伪造摘要；沿用现有 barrier 失败路径 |
| 完整 audit payload 仍在内存 | manifest 和指标计算可继续使用；投影不得修改原对象 |
| 精简摘要写入失败 | 保留现有写入异常语义，不吞错、不补写共享 fallback |
| Daily Brief 读取新摘要 | 继续获得 symbol/status/reason/error/statistics；不要求 raw payload |
| runtime status 读取新摘要 | 展示精简后的持久化事实，不把缺失 payload 报为故障 |
| 新 opening snapshot | 紧凑编码、snapshot 语义 hash 不变、同一 run manifest 和下游 provenance binding 自洽 |
| 历史漂亮 JSON | 原样保留；带匹配旧 raw hash 的历史 manifest bundle 继续可加载、校验和归档 |
| 同一 run 已存在不同字节 | 保留 write-once conflict；不得为了跨版本重写而放宽 immutable contract |
| 文件包含 NaN 或非法语义 | 保留现有 fail-closed validator/encoder 行为 |

## 实施切片

### Slice 1：Prefetch 持久化投影

- 在现有 publish owner 内加入最小投影；
- 扩充 Tick barrier 回归，证明 seal 收到完整对象、原对象未变、两账户落盘一致且不含
  `audit[].payload`；
- 覆盖 success、外层 error status/message、duration、receipt、symbol 和 summary/manifest binding
  保留；typed/partial provider fixture 明确证明嵌套诊断随 payload 删除且没有新增字段提升；
- 使用 payload-dominant fixture，按当前 writer 的
  `json.dumps(full_summary, ensure_ascii=False, indent=2) + "\n"` 计算完整 summary 基准字节，读取
  实际精简文件并断言其长度小于基准的一半。该比例只保护 fixture 的显著缩减，不作为生产 run
  阈值。

完成 focused tests 后停止，等待下一 slice 的人工确认。

### Slice 2：Opening snapshot 紧凑编码

- 只修改现有 encoder；
- 扩充 opening snapshot 回归，证明 exact compact bytes、load/validate、snapshot semantic hash、
  write-once replay，以及 candidate manifest binding 自洽；最终完整回归再证明 Recommendation Point
  等下游 provenance hash 传播自洽；
- 添加历史 pretty snapshot + 匹配旧 raw hash manifest 的 fixture，证明
  `load_candidate_snapshot_bundle()` 接受完整历史 bundle；
- 用同一 fixture 同时计算旧漂亮编码长度，证明新落盘字节明显减少。

完成 focused tests 和最终完整 validation 后停止，等待 Deepreview 授权。

## 验证计划

Implementation 开始时，controller 先把 `OM_PYTHON` 设置为已验证且包含仓库依赖的 Python 3.12
可执行文件。以下 bootstrap 要求该值存在并显式 export，再通过 `scripts/python_runtime.sh` 解析和记录
实际路径；隔离 worktree 不依赖自己的 `.venv` 或 PATH fallback，后续 focused tests 均复用
`OM_REPO_PYTHON`：

```bash
: "${OM_PYTHON:?set OM_PYTHON to a compatible Python 3.12 executable}"
export OM_PYTHON
OM_REPO_PYTHON="$(bash -c 'source scripts/python_runtime.sh && om_select_repo_python "$PWD"')"
"$OM_REPO_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12)'
```

Slice 1 最小证据：

```bash
"$OM_REPO_PYTHON" -m pytest \
  tests/test_tick_account_execution_barrier.py \
  tests/test_daily_decision_brief_service.py
```

Slice 2 最小证据：

```bash
"$OM_REPO_PYTHON" -m pytest \
  tests/test_opening_candidate_snapshot.py \
  tests/test_candidate_snapshot_manifest.py
```

Recommendation Point、Shadow Replay、Strategy Lab 和 Formal Corpus 的广泛消费者覆盖留给最终完整
pytest，避免在 focused suite 重复运行同一证据。

最终验证：

- 运行 `OM_PYTHON="$OM_REPO_PYTHON" make lint`；
- 运行 `OM_PYTHON="$OM_REPO_PYTHON" make test`；
- 运行 `git diff --check`；
- 运行文档 wording 与 sensitive-artifact guardrails；
- imports 未变化时不重新生成 dependency graph；若实现引入 import 变化，则按仓库规则重新生成并验证。

不运行真实 Tick、OpenD probe、通知发送或生产 cleanup 作为源码验收。

## 拒绝和延期的方案

- **候选 facts 规范化为只保存一次**：潜在收益更大，但会改变
  `opening_candidate_snapshot.v1`、决策 hash 和多个正式消费者；另立合同后再做。
- **对 opening snapshot 使用 gzip**：节省更多磁盘，但会增加 Tick CPU、loader/manifest/archive
  协议和历史兼容复杂度；当前没有必要。
- **run-level 共享 prefetch summary**：可消除两账户的剩余重复，但需要新路径、reader migration 和
  account/run authority 规则；当前单文件投影已覆盖主要浪费。
- **修改通用 JSON writer 或批量压缩所有 JSON**：影响面远大于两个已证实热点，且难以证明热路径和
  兼容行为；不实施。
- **在源码分支中清理历史数据**：属于独立、可破坏的运维动作；只能先预览，再经单独授权执行。

## 风险和后续 owner

- 仓库外临时脚本若读取 `audit[].payload`，会看到字段缺失。Owner：调用方；权威 provider facts 应改读
  canonical required-data receipt/blob，运行诊断应使用保留的 summary 字段。
- 每账户 summary 路径仍会重复少量元数据。Owner：本设计；只有剩余占用再次成为可测热点时才设计
  run-level 共享协议。
- opening snapshot 仍重复候选语义 facts。Owner：opening candidate 合同；只有第一阶段收益不足且
  所有消费者迁移方案通过独立评审时再规范化。
- 38.5 MB 到 27–30 MB/run 是估算，不是源码验收事实。Owner：另行授权的发布升级后自然交易日观察。
- 旧 `output_runs` 和 Shadow Replay 仍占磁盘。Owner：operator cleanup/archive 流程；删除必须使用
  现有 preview、核对 Formal Corpus/归档证据并取得单独授权。

当前没有阻塞源码实现的 open question。生产容量、真实 Tick 时延和一个完整交易日的 Formal Corpus
生成情况必须留到独立部署观察。
