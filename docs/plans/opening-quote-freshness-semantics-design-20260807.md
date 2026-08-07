# 开仓候选行情新鲜度语义修正设计确认稿

> 状态：对话已确认，尚未实施
>
> 日期：2026-08-07
>
> 适用范围：Sell Put / Covered Call 开仓候选的 OpenD 标的价格、期权盘口、候选状态和 Daily Brief 投影
>
> 不代表：代码已修改、生产配置已变更、版本已发布或远端已升级

## 1. 结论

本次修改不采用“把期权 5 分钟阈值放宽到 30 分钟或其他时长”的方案，而是修正时间字段的语义所有权：

1. OpenD 期权 snapshot 的 `update_time` 是最新价更新时间，不是 bid/ask 的更新时间；
2. 期权 `update_time` 退出候选可用性的 5 分钟硬门槛，只保留为最新价活跃度诊断；
3. 期权仍保留 5 分钟安全窗口，但它约束的是“OM 本次从 OpenD 取得 snapshot 后多久完成候选决策”；
4. 标的最新价在连续交易时段继续使用 5 分钟门槛；
5. 标的和期权在非连续交易时段都不应用 5 分钟门槛，而是投影为 `market_closed / planning_only`；
6. 请求失败、覆盖不完整、快照取得过久、盘口非法和现有策略硬门槛继续 fail closed；
7. “行情证据不可用”不得再被投影为“暂无候选”。

因此，本次修改是把 5 分钟约束绑定到正确的时钟，不是降低候选安全要求。

## 2. 当前问题与证据

### 2.1 OpenD 字段语义

当前安装的 Futu SDK 协议在
`.venv/lib/python3.12/site-packages/futu/common/pb/Qot_Common.proto:955`
明确说明：

```text
updateTime = 最新价的更新时间字符串，对其他字段不适用
```

这意味着 `update_time` 不能证明 bid/ask 最近何时变化，也不能证明一个仍然存在的盘口是否在最近 5 分钟发生过变化。

### 2.2 当前 OM 行为

当前实现存在以下语义串联：

1. [`opening_quote_evidence.py`](../../src/application/opening_quote_evidence.py) 使用同一个 `OPENING_QUOTE_MAX_AGE_SECONDS=300` 约束标的和期权；
2. 期权 observation 直接用 snapshot `update_time` 计算 `quote_age_seconds`；
3. 超过 300 秒即产生 `option_quote_stale`，合约状态变为 `data_unavailable`；
4. [`opend_symbol_fetching.py`](../../src/application/opend_symbol_fetching.py) 又把同一个 `update_time` 同时写入 `bid_update_time` 和 `ask_update_time`；
5. [`candidate_engine.py`](../../domain/domain/engine/candidate_engine.py) 拒绝所有 `opening_contract_status != ready` 的合约；
6. [`symbol_monitoring.py`](../../src/application/symbol_monitoring.py) 主要根据最终候选数判断扫描完成，零候选可能被投影成正常 `completed`；
7. Daily Brief 最终显示“本轮暂无符合条件的候选”，掩盖了实际的数据不可用原因。

### 2.3 生产现象

已观察到：

- OpenD 服务、登录状态和请求响应正常；
- 期权 snapshot 能正常返回 bid/ask、volume、OI 等字段；
- 大量已有当日成交量的 HK 期权，其 `update_time` 仍超过 5 分钟；
- 这些合约因 `option_quote_stale` 被统一 fail closed，最终形成虚假的正常零候选。

`volume > 0` 只能证明当日发生过成交，不能证明当前盘口新鲜，因此本设计也不使用 volume 作为 freshness override。

## 3. 目标语义模型

行情时间拆成三个相互独立的维度：

| 维度 | 权威证据 | 用途 | 是否为候选硬门槛 |
|---|---|---|---|
| 数据取得新鲜度 | OM 每批 OpenD snapshot 请求的接收时间 | 证明本轮确实刚刚向 OpenD 取得数据 | 是 |
| 最新价活跃度 | OpenD `last_price + update_time` | 描述最近成交或最新价变化时间 | 标的是；期权否 |
| 盘口可用性 | 本次 snapshot 返回的 bid/ask、身份、状态、tick 和 spread | 判断当前候选计算是否具备可用盘口 | 是 |

必须避免以下概念混用：

- OpenD 服务正常不等于每张期权最近 5 分钟都有成交；
- 请求刚刚成功不等于价格刚刚发生变化；
- 价格没有变化不等于当前 snapshot 已过期；
- 当日 volume 不等于当前 bid/ask 有效；
- 最新价时间不等于 bid/ask 时间。

## 4. 市场状态与 5 分钟规则

### 4.1 标的价格

#### 连续交易时段

当 OpenD `market_state` 为正式支持的连续交易状态，例如 `MORNING` 或 `AFTERNOON`：

```text
underlier_ready =
    snapshot_identity_valid
    AND market_state_continuous
    AND sec_status_normal
    AND not_suspended
    AND last_price > 0
    AND 0 <= decision_at_utc - last_price_update_time <= 300s
```

标的 spot 直接参与 strike recall、收益和容量相关计算，因此最新价时间继续是正式证据。

#### 非连续交易时段

盘前、竞价、午休、收盘后以及其他未明确支持的市场状态不使用 5 分钟门槛：

- 返回 `market_closed` 或 `planning_only`；
- 最近已完成交易时段的价格可以显示为 observation；
- 不生成当前可执行开仓候选；
- 不因为收盘后自然经过 5 分钟而把正常收盘观察值标为数据故障。

示例：

- HK 午休 12:30 使用 11:59 的价格作为上午时段观察值，不标 stale，但不可执行；
- 收盘后使用 15:59 的价格做复盘或规划，不应用 5 分钟门槛；
- 次日进入连续交易时段后，前一交易日的价格不能继续生成正式候选。

### 4.2 期权价格与盘口

#### 删除的规则

```text
now - option.update_time <= 300s
```

该规则从期权候选 readiness 中完全移除。

#### 新规则

```text
option_snapshot_fresh =
    0 <= decision_at_utc - snapshot_received_at_utc <= 300s
```

期权 `last_price_update_time` 无论为最近 1 分钟、30 分钟或更久，都只影响活跃度诊断，不直接决定 bid/ask 是否可用于候选。

#### 非连续交易时段

期权同样不应用 5 分钟门槛：

- 返回 `market_closed / planning_only`；
- 可以保留最近 snapshot 作为复盘观察；
- 不生成当前可执行候选。

## 5. 期权候选 readiness 合同

一期正式规则为：

```text
option_ready =
    snapshot_request_success
    AND snapshot_coverage_complete
    AND snapshot_identity_valid
    AND snapshot_age_seconds <= 300
    AND underlier_ready
    AND option_standard_type == STANDARD
    AND stock_owner_matches
    AND stock_type_valid
    AND sec_status == NORMAL
    AND not_suspended
    AND multiplier_consistent
    AND price_tick > 0
    AND bid > 0
    AND ask >= bid
```

Candidate Engine 继续执行策略硬门槛，包括：

- `raw_mid > 0`；
- `spread_ratio <= 0.40`；
- DTE、strike、收益、费用、IV/RV、财报和账户容量规则；
- 不使用 last 替代 bid/ask；
- 不默认 multiplier；
- 不使用旧 required-data、CSV 或历史候选作为当前报价 fallback。

以下情况继续 fail closed：

- OpenD 请求错误、超时或返回为空；
- 请求代码集合与返回代码集合不一致；
- snapshot 重复、缺失或身份冲突；
- snapshot 取得后超过 300 秒才用于候选决策；
- 标的在连续交易时段的最新价超过 300 秒；
- bid/ask 缺失、非正、倒挂或 spread 超过策略上限；
- 合约状态、owner、类型、tick 或 multiplier 缺失/冲突；
- 其他必要策略证据不可用。

## 6. 字段与 schema 调整

### 6.1 期权 observation

建议将 `opening_option_observation.v1` 升级为 `opening_option_observation.v2`。

字段调整：

| 当前字段 | 目标字段 | 说明 |
|---|---|---|
| `quote_update_time` | `last_price_update_time` | OpenD 最新价原始更新时间 |
| `quote_observed_at_utc` | `last_price_observed_at_utc` | 最新价时间的 UTC 规范化结果 |
| `quote_age_seconds` | `last_price_age_seconds` | 最新价活跃度，仅诊断 |
| `bid_update_time` | 不再从 snapshot `update_time` 填充 | 无权威字段时保持缺失 |
| `ask_update_time` | 不再从 snapshot `update_time` 填充 | 无权威字段时保持缺失 |
| 无 | `snapshot_requested_at_utc` | 当前批次请求开始时间 |
| 无 | `snapshot_received_at_utc` | 当前批次成功收到响应时间 |
| 无 | `snapshot_age_seconds` | 候选决策相对取得时间的年龄 |
| 无 | `last_price_activity_status` | `recent / quiet / unknown / anomalous`，只作诊断 |

每个 batch 必须记录自己的请求和响应时间，不能用 option-chain cache 的 `source_observed_at` 冒充 quote snapshot 取得时间。

### 6.2 策略和 sealed snapshot 绑定

- normalized candidate input 增加 `quote_evidence_schema_version`；
- 策略 hash 显式绑定新的 quote evidence policy version；
- 新旧策略产生不同的可审计 identity/hash；
- 历史 sealed snapshot 保持只读，不重写；
- 当前 `opening_candidate_snapshot.v1` 是否需要整体升级，由实现阶段根据消费者兼容性决定；不得在同一 schema 下静默改变字段含义。

## 7. 状态与通知事实口径

### 7.1 合约级分类

合约必须区分：

- `ready`：证据完整，可进入 Candidate Engine；
- `ineligible`：证据完整，但合约身份或状态明确不适用；OpenD 明确返回
  `bid=0` 且其余身份/盘口/状态证据完整时，属于“当前无有效买盘”的合法
  市场状态，归为 `ineligible`（`option_no_current_bid`），不按证据缺失处理；
- `data_unavailable`：必要证据无法证明；`bid` 缺失、非有限或为负仍归此类别；
- `market_closed`：当前不在可执行市场状态。

Candidate Engine 的拒绝记录也应区分：

- `contract_ineligible`；
- `evidence_unavailable`；
- `policy_rejected`。

不能再把三者都压缩为泛化的 `input_invalid`。

### 7.2 策略级分类

每个 symbol/strategy scope 保存：

```text
requested_contract_count
returned_contract_count
evidence_ready_count
contract_ineligible_count
policy_rejected_count
evidence_unavailable_count
accepted_count
unavailable_by_reason
```

策略状态按以下规则投影：

| 条件 | 状态 |
|---|---|
| 完整 universe 已评估，存在候选 | `candidates_found` |
| 完整 universe 已评估，全部为合法不入选 | `no_candidate` |
| 部分合约不可评估，其余结果可用 | `partial_data` |
| 没有足够证据评估任何相关合约 | `data_unavailable` |
| 市场非连续交易状态 | `market_closed` |

### 7.3 Daily Brief

- 只有 `no_candidate` 可以显示“本轮暂无符合条件的候选”；
- `partial_data` 显示“本轮部分行情证据不可用，候选结果不完整”；
- `data_unavailable` 显示“本轮行情证据不可用，未形成当前候选”；
- `market_closed` 显示规划或闭市观察语义；
- 不向普通通知展开数百条合约错误，只展示聚合数量和主要 reason code；
- Agent 的 `candidate_filter_explain` 保留逐合约完整证据。

## 8. 可选二期：HK order book shortlist 复核

Futu `get_order_book` 对 HK 提供：

- `svrRecvTimeBid`；
- `svrRecvTimeAsk`；
- 当前 order-book levels。

协议同时说明，OpenD 重启或首次推送缓存数据时，接收时间可能为零。因此这些字段不能简单替换成另一套“最近 5 分钟必须变化”规则。

二期可仅对最终 shortlist 执行：

1. 重新读取当前一档 bid/ask；
2. 记录 per-side server receive time；
3. 重新计算 mid、spread、费用、收益和排序；
4. 若候选失效，从后续 provisional candidate 有界补位；
5. 达到调用预算仍无法完成时返回 partial，而不是静默使用旧候选。

一期不把逐合约 order-book 查询作为前置条件，原因是它会引入额外订阅权限、限频、调用量和运行时延迟风险。

## 9. 实施切片

### Slice 1：策略合同与唯一 freshness owner

主要文件：

- `docs/candidate_strategy.md`
- `domain/domain/quote_freshness.py`
- `src/application/opening_quote_evidence.py`

内容：

- 修订现有“标的 spot 与期权 bid/ask 都不得超过 5 分钟”的旧合同；
- 把标的 last-price freshness、snapshot acquisition freshness 和期权 activity 分开；
- 删除 application/domain 两套重复判定。

### Slice 2：OpenD snapshot receipt 与字段迁移

主要文件：

- `src/application/opend_market_snapshot_fetching.py`
- `src/application/opend_symbol_fetching.py`
- `src/application/candidate_models.py`
- `src/application/opend_symbol_outputs.py`

内容：

- 为每批 snapshot 记录 request/receive 时间；
- 停止伪造 bid/ask update time；
- 输出 observation v2 字段；
- 保持 snapshot coverage、identity 和 provider error 合同。

### Slice 3：Candidate Engine 与状态闭环

主要文件：

- `domain/domain/engine/candidate_engine.py`
- `src/application/candidate_scanning.py`
- `src/application/pipeline_watchlist.py`
- `src/application/opening_candidate_snapshot.py`
- `src/application/symbol_monitoring.py`
- `src/application/daily_decision_brief_renderer.py`

内容：

- 区分 evidence unavailable、contract ineligible 和 policy reject；
- 根据评估覆盖而不是单纯候选数生成 scan status；
- 保证 `no_candidate / partial_data / data_unavailable` 与通知一致。

### Slice 4：离线验证与兼容性

- provider-shaped unit tests；
- required-data 和 snapshot schema tests；
- Candidate Engine 和 status aggregation tests；
- Daily Brief 文案 tests；
- 现场数据离线 shadow replay；
- focused tests、dependency checks 和全量测试。

## 10. 验证矩阵

| 场景 | 预期结果 |
|---|---|
| 15:00 取得新 snapshot，期权最新价时间为 09:30，bid/ask 合法 | `ready`，不因最新价安静被拒 |
| 15:00 取得新 snapshot，期权 `update_time` 缺失，bid/ask 合法 | 最新价 activity 为 unknown，盘口仍可评估 |
| 期权 `update_time` 在未来 | activity 为 anomalous，不伪装成 bid/ask 时间 |
| snapshot 响应距候选决策超过 300 秒 | `data_unavailable` |
| snapshot 请求失败或覆盖不完整 | 最小受影响 scope fail closed |
| 标的在连续交易时段超过 300 秒未更新 | 标的 unavailable，依赖它的候选 fail closed |
| HK 午休使用 11:59 标的观察值 | `market_closed/planning_only`，不是 stale 故障 |
| 收盘后自然超过 5 分钟 | 保留最近 session observation，不生成可执行候选 |
| bid/ask 缺失、倒挂或 spread 超限 | 合约拒绝 |
| 所有证据完整但全部不满足策略 | `no_candidate` |
| 部分合约证据不可用且无候选 | `partial_data` 或 `data_unavailable`，不显示“暂无候选” |

现场 replay 至少覆盖 15:00、15:30、15:50 三个时点，并比较：

- 原 `option_quote_stale` 数量；
- 新 evidence-ready 合约数量；
- 进入策略硬筛的合约数量；
- 最终候选及排序变化；
- spread、收益和 IV/RV 分布；
- 是否有请求失败、覆盖不完整或跨 run 旧数据被错误放行。

离线验证不得触发真实 tick、Feishu 通知、交易、持仓或生产数据写入。

## 11. 完成定义

本 work unit 只有同时满足以下条件才算完成：

1. 代码不再用期权 `update_time` 判定 bid/ask 的 5 分钟 freshness；
2. 代码不再把期权 `update_time` 复制成 bid/ask update time；
3. 每个候选能追溯到本轮 OpenD snapshot 的 request/receive receipt；
4. 超过 300 秒的是 OM 持有的 snapshot，而不是“市场没有变化”；
5. 标的连续交易时段仍保持独立的 5 分钟最新价保护；
6. 非连续交易时段不使用 5 分钟门槛，也不生成可执行候选；
7. `no_candidate / partial_data / data_unavailable / market_closed` 能被 sealed snapshot、Agent 和 Daily Brief 一致解释；
8. 现场 replay 能解释恢复的合约和最终候选变化；
9. focused tests、全量测试和静态/依赖检查通过；
10. 未通过新增 fallback、volume override 或市场特例掩盖证据缺失。

## 12. 明确不采用的方案

- 把 HK 期权阈值从 5 分钟简单改成 30 分钟、60 分钟或全天；
- `volume > 0` 时绕过 freshness；
- US 与 HK 各维护一套无证据的硬编码时间阈值；
- 用 last price 替代 bid/ask；
- 用历史 required-data、CSV、收盘价或旧候选补当前行情；
- 因为 OpenD 服务健康就跳过请求覆盖和盘口完整性校验；
- 因为最终候选数为零就自动认定扫描完整成功。

## 13. 发布与运行边界

本文只确认设计。后续边界彼此独立：

1. 修改策略合同和代码；
2. 本地验证和离线 shadow replay；
3. 提交、推送和合并；
4. VERSION/tag/GitHub Release 发布；
5. 远端升级、服务重载和运行时验证。

任何前一步授权都不自动包含后一步。生产配置修改、真实通知、远端升级和服务变更仍需单独明确授权。
