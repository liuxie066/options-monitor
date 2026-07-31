# Daily Brief 成功空期权链降级投递计划（严格边界版）

> 本计划只解决一个问题：OpenD/Futu 调用成功、响应与转换均有效、但期权链为零行时，
> 不再把它当成获取失败并阻断 Daily Brief。
>
> provider 调用失败的局部化判定明确延期；本轮所有 provider error 继续走现有失败路径。

## 1. 决策

本轮只交付 `success_empty`：

```text
provider 成功返回零行
  -> fetch owner 形成 success_empty
  -> 发布可验证的零行 quote snapshot/receipt
  -> opening strategy 完成，candidate_count=0
  -> candidate capture/source 继续走现有 complete v1
  -> Daily Brief 嵌入非行动型数据告警
  -> 使用现有 normal degraded delivery
```

以下结果一律不在本轮降级：

```text
provider_error | parse_error | not_attempted | integrity_error | unknown
```

它们继续使正常报告 authority 不可用，并使用现有 `fixed_failure`。

## 2. Work unit 边界

### 2.1 本轮包含

- `HK` / `US` opening candidate scan 使用的 OpenD/Futu `option_expiration` 与 `option_chain`；
- 在 fetch owner 处区分：
  - `success_rows`
  - `success_empty`
  - 非成功结果；
- 显式保留 expiration discovery 的成功空、失败和转换结果，禁止把 discovery exception 压成空列表；
- scheduled path 以 required-data plan-time discovery 作为每个 physical symbol 的唯一权威观察，
  fetch/prefetch 不得再次 discovery；
- 为 `success_empty` 发布合法的零行 JSON、CSV 和 quote receipt；
- 让现有 strategy status、candidate capture/source 接受该成功证据；
- Daily Brief 展示本轮 `success_empty` 告警；
- 验证现有 normal delivery、failure delivery 和 exact retry 不回归。

### 2.2 明确延期

不得在实现中顺手加入：

- 单 symbol `provider_error` 的局部降级；
- provider peer、health scope、时间窗口或故障比例判定；
- account-scoped degraded coverage proof；
- candidate capture/source v2；
- physical fetch 与 consumer projection 新模型；
- 产品 eligibility catalog 或新的 `not_applicable` 语义；
- expiration、DTE、strike、option side 的部分覆盖；
- Close Advice/open-position absence 新证明；
- position-scoped `not_evaluable` 新语义；
- 跨批次 `new / changed / persistent / recovered / scope_retired`；
- stable alert id、digest、baseline、数据库或 pointer；
- 新通知类型、独立告警消息或第二套 sender；
- Daily Brief revision、delivery envelope、delivery key、确认或 retry 改造；
- runtime feature flag、后台服务或管理界面；
- 自动交易、roll、close、账本写入、配置修改、真实通知、发布或升级。

### 2.3 失败边界

以下任一情况继续 fail closed：

- provider non-success retcode；
- auth、2FA、permission、rate-limit；
- timeout、断连、调用异常或 provider rejection；
- response 类型不符合 adapter contract；
- canonical conversion 或 row validation 异常；
- 部分 expiration 成功、部分失败；
- expected request 未执行、取消或编排提前终止；
- manifest、receipt、hash、bytes、run/account/path/freshness/integrity 失败；
- strategy scanner 自身异常；
- portfolio、ledger、FX、cash/share capacity 或 lifecycle authority 不完整；
- 未知或无法分类的结果。

即使同 run、同 endpoint 有其他成功标的，provider error 也不得在本 work unit 中转成 degraded。

## 3. 不变量

1. **空数据不等于获取失败。** `success_empty` 必须由 provider 成功与成功转换正向证明。
2. **错误不伪装成空数据。** 任何异常、失败或未执行都不能产生 `meta.status=ok`。
   expiration discovery 的异常不得通过 `expirations=[]` 进入无 expiration 的兜底 chain 调用。
3. **一次 observation 只有一个权威结果。** scheduled path 的 discovery outcome、时间和 exact targets
   由 run-scoped fetch plan 冻结；inprocess/subprocess fetch 都只能消费，不得重复调用 discovery endpoint。
4. **零行不产生行动。** 对应 strategy 的 candidate count 必须为 0。
5. **quote evidence 仍完整。** 零行 JSON/CSV 必须由现有 quote receipt 绑定准确 bytes、policy、
   run、symbol、market、observation time 和 freshness。
6. **candidate source 不降级。** `success_empty` 是可靠完成，不是省略 scope；
   candidate capture/source 继续使用现有 complete v1 contract。
7. **持仓语义不改变。** `success_empty` 只改变 opening scan 对 fetch 成功的分类；
   Close Advice 对 exact contract 缺失的现有判断保持不变。
8. **完整性规则不改变。** typed outcome 不能覆盖 receipt 或 identity failure。
9. **renderer 不重新分类。** renderer 只展示 assembler 已确认的成功空事实。
10. **局部投影不拥有全局 authority。** status warning projection mismatch 只影响该提示；
    只有 adopted source graph 或其他既有全局 authority 失效才使用 fixed failure。
11. **投递协议不改变。** successful Brief、failure artifact、delivery envelope 和 exact retry
   继续沿用现有实现。

## 4. Typed outcome

分类权威保留在 required-data acquisition owner：scheduled discovery 由 planning owner 持有，
per-expiration chain 由 option-chain fetch owner 持有。下游不得根据 CSV 是否为空、异常字符串或文件是否存在
重新推断 fetch 结果。

本轮只需要稳定区分：

| outcome | 含义 | 本轮行为 |
|---|---|---|
| `success_rows` | provider 成功、转换成功、存在 canonical rows | 现有成功路径 |
| `success_empty` | provider 成功、转换成功、canonical rows 为 0 | 新的零行成功路径 |
| `provider_error` | provider 返回失败或调用异常 | 现有失败路径 |
| `parse_error` | response 到 canonical rows 转换失败 | 现有失败路径 |
| `not_attempted` | expected request 未执行 | 现有失败路径 |

scheduled path 由现有 required-data planning 与 fetch 阶段共同形成，但每个 physical symbol 只允许一次
expiration discovery：

```text
required-data plan-time expiration discovery（唯一权威观察）
  -> discovery_success_rows | discovery_success_empty
  -> provider_error | parse_error

run-scoped fetch plan 冻结 outcome + exact targets + observation identity/time
  -> success_empty: prefetch 直接发布零行 evidence，不调用 symbol fetch
  -> success_rows: inprocess/subprocess 只使用 explicit targets
  -> projection_empty/error: fail closed，不调用 symbol fetch

per-expiration option chain（仅 exact targets 非空时执行）
  -> success_rows | success_empty
  -> provider_error | parse_error | not_attempted
```

在 `opend_symbol_chain_fetching.py` 增加内部 typed discovery result，现有
`list_option_expirations()` list-returning facade 保持兼容；`RequiredDataFetchPlanBundle` 只附加当前 run
所需的 discovery outcome/evidence。不得新增公共 schema、通用 result framework 或第二套 fetch service。

同一 symbol 因多个 account/config projection 合并时，prefetch planning owner 按 physical fetch binding
`(symbol, source, host, port, trading_date)` memoize discovery，一次结果注入全部 side plan；不得为每个
source config 重复调用 provider。

### 4.1 `success_empty` 的充分条件

必须同时满足：

1. plan-time provider discovery 或明确的 per-expiration chain 调用实际发生；
2. provider 返回成功码；
3. response 类型满足 adapter contract；
4. 请求的 expiration scope 已完成；
5. canonical conversion 成功；
6. row validation 成功，没有吞掉异常；
7. 合计 canonical row count 为 0；
8. 记录 source observation/completion time 和请求身份；
9. reason code 只能是：
   - `no_expirations`
   - `no_contract_rows`

reason 的证据来源固定为：

- `no_expirations`：expiration discovery 调用成功、response/转换有效且返回零个 expiration；
  该 plan-time observation 直接形成 `success_empty`，不得再进入 symbol fetch，也不得调用
  `get_option_chain(expiration=None)`；
- `no_contract_rows`：存在明确的 expected expiration targets，且每个 target 的 option-chain 调用和转换都成功，
  合计 canonical rows 为 0。

discovery 成功返回 expirations、但 DTE/config projection 后没有 expected target，不属于本轮
`success_empty` 证明；不得执行无 expiration 兜底，也不得发布成功 receipt，继续 fail closed。

`EMPTY_CHAIN` 不再作为错误字符串参与下游状态判断。它可以保留为兼容诊断文本，但必须由
`source_outcome=success_empty` 和受控 reason code 解释。

### 4.2 明确不是 `success_empty`

- provider 返回 non-success；
- expiration discovery 调用异常、non-success 或 response/转换无效；
- retry 最终失败；
- response 为异常对象或缺少必需结构；
- conversion/validation 抛错；
- 所有目标未实际执行；
- 部分 expiration success、部分 error；
- 因 failure budget、cancellation 或 worker failure 提前结束；
- discovery 有结果但 request projection 后没有 expected expiration target；
- 只发现旧缓存、旧 CSV 或上一个 run 的 receipt。

这些结果不得写成功 payload 或 quote receipt。

## 5. 数据与契约

### 5.1 Option-chain fetch result

#### Scheduled discovery ownership

scheduled path 选择 `build_required_data_fetch_plan()` 的 plan-time discovery 作为唯一权威观察。
`opend_symbol_chain_fetching.py` 提供内部 typed result：

```text
outcome = success_rows | success_empty | provider_error | parse_error
reason_code
expirations
observed_at_utc
completed_at_utc
request_identity = symbol | underlier | source | host | port | trading_date
```

现有 `list_option_expirations()` 继续返回 `list[str]`，供非 scheduled compatibility caller 使用；
scheduled planning 必须调用 typed surface，不能先丢失 outcome 再从空 list 反推。

`RequiredDataFetchPlanBundle` 增加内部 additive discovery evidence，并保留现有
`expiration_discovery_complete/error` compatibility projection。run plan identity 必须包含：

- typed outcome 和 reason；
- exact discovered expirations；
- projected per-side exact targets；
- observation/completion time；
- request identity。

timestamps 是当前 run evidence 的组成部分；receipt 的 fetch-plan identity 必须采用同一 frozen plan bytes。

#### Scheduled aggregation rules

- planning owner 对相同 physical fetch binding 只调用一次 discovery；
- discovery `success_empty`：生成 `success_empty/no_expirations` fetch result，prefetch 直接进入 output/receipt
  发布，不调用 `fetch_symbol()`、CLI 或 option chain；
- discovery `success_rows` 且 projected targets 非空：inprocess/subprocess 都显式传递 exact expirations，
  `fetch_symbol()` 因 explicit targets 跳过内部 discovery；
- discovery `success_rows` 但 projected targets 为空：`projection_empty`，本轮 fail closed，不调用
  `fetch_symbol()`；
- discovery error：global plan 保持 incomplete，现有 barrier fail closed，不重试第二次 discovery；
- symbol fetch 若收到 scheduled plan 却缺少 typed discovery evidence 或 explicit targets：
  `not_attempted/plan_identity_missing`，不得回退 compatibility discovery。

per-expiration chain 聚合规则：

- 存在 canonical rows，且全部目标完成：`success_rows`；
- 全部 explicit targets 成功完成且合计零行：`success_empty/no_contract_rows`；
- 任一目标 provider failure：`provider_error`；
- 任一 conversion/validation failure：`parse_error`；
- 任一 expected target 未执行：`not_attempted`；
- partial success 不在本轮处理，整体 fail closed。

当前把 empty expiration 写入 `errors[]` 的路径必须调整：成功空可以保留 diagnostics，但不能再让
symbol terminal status 变成 error。

当前 planning 与 fetch 的 exception-to-empty 路径必须移除。scheduled caller 不得用
`explicit_expirations=None` 或 `fetch_option_chains(expirations=[])` 表达 discovery/projection 成功空。
`fetch_option_chains()` 的空 targets compatibility 语义不做全局修改；scheduled owners 在进入该函数前
已持有 typed plan 并终止，避免影响其他直接调用方。

### 5.2 Required-data outputs

继续使用现有：

```text
required_data_quote_snapshot.v1
required_data_snapshot_manifest.v1
```

`success_empty` 输出：

- raw JSON：
  - `meta.status=ok`
  - `meta.source_outcome=success_empty`
  - `meta.reason_code=no_expirations|no_contract_rows`
  - `rows=[]`
- parsed CSV：
  - 保留 `REQUIRED_DATA_COLUMNS` 标准列头；
  - 数据行数为 0；
- quote receipt：
  - 使用现有 `publish_required_data_quote_snapshot()`；
  - snapshot、payload hash、policy hash、run、market 和 freshness 规则不变；
  - `no_expirations` 的 `source_observed_at` 使用 frozen plan-time discovery observation，
    不得使用 prefetch 发布时的 `datetime.now()` 伪造 source observation；
- required-data manifest entry：
  - `status=ready`
  - 保持现有 snapshot/receipt bindings；
  - 附加 `source_outcome=success_empty` 和 `reason_code`。

manifest 的新增字段不得从 prefetch summary 或 strategy status 复制。`seal_required_data_snapshot()` 在验证
quote receipt、payload 和 frozen raw bytes 时，从 receipt-contained raw JSON 的 `meta` 正向提取并验证：

- `source_outcome=success_empty`；
- reason 在 allowlist；
- `rows=[]`；
- raw/CSV 均与 receipt 绑定的 bytes 一致。

现有 frozen required-data resolver 返回已验证的 `source_outcome` 和 `reason_code`，供下游做 equality check；
下游不得自行读取 mutable raw JSON。

`success_rows` 可以附加 `source_outcome=success_rows`，但其 status 和既有消费行为不变。

manifest v1 的 status 与 receipt 语义没有改变：`ready` 仍表示存在当前 run、完整且新鲜的 quote
snapshot。新增字段只区分该成功 snapshot 有行还是零行。

### 5.3 Strategy status

继续使用现有：

```text
strategy_scan_status.v1
strategy_scan_status_index.v1
completed | unavailable | failed
```

映射：

- `success_rows` -> `completed`；
- `success_empty` -> `completed, candidate_count=0`；
- provider/parse/not-attempted -> 现有 `unavailable|failed` 路径。

对 completed-zero status 增加向后兼容的展示字段：

```text
source_outcome=success_empty
reason_code=no_expirations|no_contract_rows
```

status 必须继续绑定 success-empty quote 的 snapshot id 和 receipt path。

### 5.4 Candidate capture/source

不修改 schema，不新增 reliability 状态：

```text
position_advice_candidate_all_decisions_capture.v1
position_advice_candidate_all_decisions.v1
```

现有 complete 条件继续成立，因为：

- expected scope 已执行；
- strategy status 是 `completed`；
- candidate count 为 0；
- quote snapshot/receipt 存在且有效；
- quote dependency 不缺失；
- 没有省略或 unavailable scope。

candidate source 继续精确采用 success-empty quote dependency。零 candidate rows 是合法结果，
但至少存在一个 quote dependency 的既有要求不变。

不得增加 degraded coverage dependency，也不得让 provider-error scope 进入 complete capture。

### 5.5 Daily Brief

Daily Brief 的 normal authority 不依赖新降级契约：Position Advice source graph 仍然是 complete。

assembler 从 completed strategy status 投影本轮提示，并交叉检查：

- `source_outcome=success_empty`；
- reason code 在 allowlist；
- candidate count 为 0；
- snapshot id 和 receipt path 非空；
- 使用 `state/required_data_snapshot_manifest.json` 和现有 frozen required-data resolver 验证同 run、同 symbol
  的 ready entry、receipt、payload、raw/CSV bytes 与 freshness；
- strategy status 的 snapshot id、receipt path、`source_outcome`、reason 必须与 resolver 返回的已验证事实逐项相等；
- 同 scope 没有 candidate decision。

#### Authority 分流

校验失败必须按 owning authority 分流，不能仅因为 warning projection 有问题就阻断全局消息：

1. **adopted source integrity failure**
   - frozen manifest、receipt、payload、bytes、freshness 或 adopted candidate quote dependency 无效；
   - Position Advice source authority 因此不可用；
   - 沿用现有 `authority_conflict` / pipeline blocker / fixed failure。
2. **strategy-status projection mismatch**
   - frozen manifest/resolver 与 adopted candidate source graph 均有效；
   - 只有 strategy status 的 snapshot、receipt、outcome、reason 或 family projection 不一致；
   - 不展示该 scope 的 success-empty warning；
   - 嵌入：

```json
{
  "scope": "strategy",
  "market": "HK",
  "symbol": "9898.HK",
  "strategy_family": "covered_call",
  "reason": "strategy_status_projection_mismatch",
  "severity": "warning",
  "actionable": false
}
```

   - Brief 保持 `degraded`，其他 adopted source authority 完整时继续 normal delivery；
   - 不修改 `pipeline_succeeded`，不创建新 blocker，不修改 notification authority/repository。

规范化提示：

```json
{
  "scope": "strategy",
  "market": "HK",
  "symbol": "9898.HK",
  "strategy_family": "covered_call",
  "outcome": "success_empty",
  "reason": "no_contract_rows",
  "severity": "warning",
  "actionable": false
}
```

该提示只用于：

- 说明本轮 provider 成功但没有可扫描合约；
- 说明该 symbol/family 没有形成候选；
- 将 Brief 标记为 `degraded`；
- 继续发送其他可靠内容。

不得渲染成：

- “产品不适用”；
- “已退市”；
- “provider 获取失败”；
- position `not_evaluable`；
- 交易、roll、close 或人工执行建议。

若 status projection 缺失、身份不一致、candidate count 非零或任一 binding/outcome/reason 不相等，不把它
当作 success-empty 告警；先确认 adopted source graph 是否仍有效，再按上述两类 authority 分流。

### 5.6 Close Advice 与持仓

本轮不读取或修改 Close Advice plan，不新增 position absence proof。

同一零行 quote snapshot 若被现有 Close Advice 消费：

- exact contract 是否存在仍由 Close Advice owner 判断；
- Close Advice 产生的 unavailable、not-evaluable、blocker 或 data gap 继续按现有契约进入 Brief；
- opening scan 的 `success_empty` 不得覆盖、抑制或降级这些结果；
- 本计划不承诺带 open position 的账户一定可以 normal delivery。

### 5.7 Notification flow

不修改 notification decision 或 repository：

- `persist_daily_decision_brief_success()`
- `prepare_daily_decision_brief_delivery()`
- fixed report / candidate alert / fixed failure
- delivery key、source digest 和 rendered message freezing
- pending / confirmed / ambiguous
- exact retry
- 保持现有 authority capability 契约：normal 与 fixed-failure token 可以同时存在；
  `decide_daily_brief_notification()` 每次只选择一个 delivery action，repository 只准备一个对应 envelope

现有 flow 已接受：

```text
brief.status in {ready, degraded}
and actionability != blocked
and normal_delivery_allowed
```

本 work unit 只让可靠的 completed-zero scope 到达该既有路径。

## 6. 实施切片

### Slice A：成功空分类与 quote evidence

主要边界：

- `src/application/required_data_planning.py`
- `src/application/required_data_prefetch_planning.py`
- `src/application/multi_tick/required_data_prefetch.py`
- `src/application/option_chain_fetching.py`
- `src/application/opend_symbol_chain_fetching.py`
- `src/application/opend_symbol_fetching.py`
- `src/application/opend_symbol_outputs.py`
- `src/application/required_data_snapshot.py`

交付：

- typed terminal outcome；
- plan-time discovery 成为 scheduled path 唯一 observation，并按 physical binding memoize；
- frozen discovery evidence 与 exact projected targets 进入 run fetch plan identity；
- inprocess/subprocess 只消费 frozen plan，禁止第二次 discovery；
- expiration discovery 成功空与 exception 不再共享 `expirations=[]` 表达；
- discovery 成功空不再触发无 expiration chain fallback；
- projection-empty 不得转成 `explicit_expirations=None` 或 unscoped fetch；
- conversion/validation exception 不再被吞成空结果；
- prefetch budget、wave 和 global summary 使用 frozen typed discovery outcome：
  `success_empty`、`projection_empty` 与 discovery error 均计为 0 次 chain call，
  `success_rows` 只按 exact projected targets 计数，缺失 typed evidence 时 fail closed；
- success-empty raw JSON、零行 CSV、quote receipt 和 manifest entry。

Slice A 验收后再进入 Slice B。

### Slice B：completed-zero projection 与 Brief 展示

主要边界：

- `src/application/symbol_monitoring.py`
- `src/application/strategy_scan_status.py`
- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`

交付：

- completed-zero strategy status；
- manifest/status quote binding 的 frozen resolver equality check；
- projection mismatch 只形成局部非行动型 data gap，不创建 global blocker；
- candidate capture/source v1 回归证明；
- Daily Brief 非行动型 success-empty 告警；
- 现有 normal/failure notification 路径回归证明。

不得修改：

- `src/application/position_advice_source_producers.py` 的 public schema；
- `src/application/position_advice_source_receipts.py` 的 dependency policy；
- `src/application/daily_decision_brief_repository.py`；
- delivery confirmation 或 retry 状态机。

## 7. 验收矩阵

### 7.1 必须 normal degraded delivery

1. 单标的 `success_empty`：
   - provider success；
   - 有合法零行 quote receipt；
   - strategy completed、candidate_count=0；
   - capture/source v1 complete；
   - Brief degraded，包含非行动型告警；
   - 使用 normal delivery。
2. 一个标的 `success_empty`、另一个存在候选：
   - 空标的不产生 candidate；
   - 其他候选正常展示；
   - Brief degraded，包含准确 symbol/family；
   - 使用 normal delivery。
3. 全部 opening scopes 都是 `success_empty`：
   - 每个 scope 都有合法 quote receipt；
   - candidate source 为零 decisions，但 quote dependencies 非空；
   - Brief degraded、零候选、正常发送。
4. expiration discovery 成功返回空：
   - 形成 `success_empty/no_expirations`；
   - plan-time discovery 是唯一 provider observation；
   - 同 symbol 多 account/config projection 仍只调用一次 discovery；
   - inprocess/subprocess 都不再次 discovery；
   - 不调用无 expiration option chain；
   - 其余 evidence、completed-zero 和 normal degraded delivery 要求与单标的一致。
5. strategy-status projection mismatch、但 adopted source graph 完整：
   - 不展示 success-empty warning；
   - 展示非行动型 `strategy_status_projection_mismatch` data gap；
   - 其他可靠内容继续展示；
   - Brief degraded，使用 normal delivery。

### 7.2 必须继续 fixed failure

1. provider error，即使同 endpoint 有成功 peer；
2. auth、2FA、permission、rate-limit；
3. timeout、断连、provider rejection；
4. parse 或 validation error；
5. expected request not attempted；
6. 部分 expiration success、部分 failed；
7. manifest/receipt/hash/bytes/freshness/run/account mismatch；
8. strategy status 缺失、重复、unexpected 或 scanner failed；
9. portfolio、ledger、FX、capacity 或 lifecycle authority 不完整；
10. 未知异常。
11. expiration discovery exception、invalid response，或 discovery 有结果但 request projection 后没有 target；
12. discovery failure 后不得调用无 expiration chain，也不得发布成功 quote receipt。
13. frozen manifest/receipt/bytes 或 adopted candidate quote dependency 无效，导致 Position Advice
    source authority 不可用。

### 7.3 持仓边界

1. success-empty symbol 存在 open option：
   - opening candidate count 仍为 0；
   - Close Advice 继续独立判断 exact contract；
   - opening success-empty 不得把 Close Advice blocker 改成 warning；
   - 最终 normal/failure 结果服从现有 Close Advice 与 Brief authority。
2. Close Advice disabled：
   - 本轮不构造 position absence proof；
   - 不改变 ledger/lifecycle 现有行为；
   - 不据此开放 provider-error degradation。

### 7.4 不回归

- success_rows 行为不变；
- candidate capture/source v1 schema 和 dependency policy 不变；
- completed-zero strategy 仍属于可靠 decision source；
- provider-error path 仍不能发布成功 quote receipt；
- Daily Brief ready/degraded/blocked 既有判定不被放宽；
- Brief success-empty warning 只接受与当前 run frozen manifest 完全一致的 status binding；
- 仅 status projection mismatch 不创建全局 blocker；underlying adopted source integrity failure 仍 fail closed；
- scheduled path 每个 physical fetch binding 只有一次 expiration discovery；
- 单次 delivery decision 只选择 normal action 或 `fixed_failure`，不会同时生成两种 envelope；
  不要求互斥现有 normal/fixed-failure capability token；
- exact retry 回读同一冻结消息；
- `lx`/`sy`、HK/US artifact 和提示隔离。

## 8. 测试与质量门

Slice A focused tests：

```bash
python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_required_data_fetch_planning.py \
  tests/test_required_data_prefetch_budget.py \
  tests/test_opend_chain_cache_minimal.py \
  tests/test_fetch_market_data_opend_explicit_expirations.py \
  tests/test_opend_symbol_fetching_cli.py \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_required_data_snapshot.py
```

Slice A 必须包含以下直接断言：

- plan-time expiration discovery 是 scheduled path 唯一 provider observation；
- 同一 physical binding 的多 account/config projection 只调用一次 discovery；
- success rows 的 inprocess/subprocess fetch 都显式使用 frozen exact targets，不再次 discovery；
- expiration discovery success-empty 不调用 `fetch_symbol()`、CLI 或 option chain；
- success-empty、projection-empty 和 discovery error 的 symbol/wave/global chain-call budget 均为 0，
  多个 zero-call symbol 不会被错误拆 wave 或标记 oversized；
- success-rows 的 chain-call budget 与 frozen exact projected targets 数量完全相等；
- expiration discovery exception / invalid response 保留为 provider/parse failure；
- discovery failure 后无第二次 discovery 和 `expiration=None` fallback；
- 单 expiration empty、多个 expiration 全 empty 为 `success_empty/no_contract_rows`；
- empty + error、success rows + error、expected target 未执行均 fail closed；
- discovery rows 经 DTE/config projection 后无 target 不进入 success-empty，也不转成
  `explicit_expirations=None`；
- frozen plan identity、quote receipt fetch plan 与 observation identity/time 完全一致；
- `no_expirations` receipt 使用 discovery observation time，不使用 publish time；
- row validation exception 不产生 `meta.status=ok`、raw success payload 或 quote receipt。

Slice B focused tests：

```bash
python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_symbol_monitoring_fetch_spec_merge.py \
  tests/test_strategy_scan_status.py \
  tests/test_pipeline_watchlist_whitelist.py \
  tests/test_position_advice_candidate_capture_runtime.py \
  tests/test_position_advice_account_sources.py \
  tests/test_position_advice_source_producers.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_notification_flow.py
```

Slice B 必须包含以下直接断言：

- completed、零 decisions、合法 quote binding 使 candidate capture/source v1 保持 complete；
- candidate source 为零 decisions 时仍至少绑定一个 quote dependency；
- status 与 frozen manifest 的 snapshot、receipt、outcome、reason 全部相等时才投影 warning；
- status projection empty binding、错 family、snapshot/receipt/reason/outcome mismatch 且 adopted source graph
  仍完整时，不形成 success-empty warning，只形成非行动型 projection-mismatch data gap；
- manifest/receipt/hash/bytes/freshness 或 adopted quote dependency failure 导致 source authority 不可用时，
  使用 existing fixed failure；
- 正确 warning 为 `severity=warning`、`actionable=false`，Brief 为 degraded，
  `normal_delivery_allowed=true`，delivery decision 选择现有 normal action，且不生成 fixed-failure artifact/envelope；
- projection mismatch + valid source graph 同样选择 normal degraded delivery，且不生成 fixed-failure artifact/envelope；
- 任一既有 authority blocker 仍使 `normal_delivery_allowed=false`，delivery decision 选择 `fixed_failure`，
  且只生成 fixed-failure envelope；
- 上述断言不要求 `fixed_failure_delivery_allowed=false`，也不修改现有 capability token 生成契约。

Tick 回归：

```bash
python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py
```

若 import graph 改变：

```bash
python3.12 scripts/generate_dependency_graph.py
python3.12 scripts/generate_dependency_graph.py --check
```

最终执行 Ruff、完整 pytest 和 diff 检查。任何 slice 未通过，不进入下一 slice。

## 9. 完成定义

只有以下条件全部满足时，本 work unit 才完成：

1. provider success + valid zero rows 被稳定分类为 `success_empty`；
2. scheduled path 每个 physical fetch binding 只有一次 plan-time discovery，outcome、targets、identity 和
   observation time 被 run plan 与 receipt 共同绑定；
3. success-empty 与 exception/invalid response 有独立证据，且空 discovery、error 或 projection-empty
   均不触发第二次 discovery 或 `expiration=None` fallback；
4. provider、parse、not-attempted、partial、projection-empty 或 integrity error 不会落入成功空路径；
5. success-empty 发布当前 run 的合法零行 quote receipt；
6. strategy status 为 completed-zero；
7. candidate capture/source v1 保持 complete 且 schema 不变；
8. Daily Brief 仅在 status 与 frozen manifest/receipt 证据完全一致时展示 success-empty warning；
9. 仅 status projection mismatch 时 delivery decision 选择 normal degraded；adopted source integrity 失效时
   选择 fixed failure；单次只生成一种 delivery envelope，不改变可并存的 capability token；
10. provider error 即使存在 peer 也继续 fixed failure；
11. Close Advice、持仓、source manifest、notification persistence 和 retry 语义不变；
12. focused、tick、Ruff 和完整测试通过。

实现、commit/push、release、production upgrade 和真实通知仍是后续独立授权边界。
