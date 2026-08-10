# Gateflow Goal Confirmation — HK Combo Capture / Failure Notification

- Work unit: `hk-combo-capture-failure-notification`
- Gate: `goal confirmation`
- Date: 2026-08-10
- Status: confirmed by user
- Branch: `fix/hk-combo-capture-failure-notification`
- Base: `main@0d635e11`

## User problem

2026-08-10 北京时间 09:40 和 10:00 的 HK 期权监控都没有发出消息。两次故障发生在
通知 provider 调用之前，但是由两个不同的失败点触发：

- 09:40 run `20260810T014016Z-581238` 在 required-data prefetch 期间获取
  `0700.HK` 期权到期日超时，pipeline 未执行；
- 10:00 run `20260810T020017Z-6a4e0b` 的 `lx` / `sy` pipeline 都因
  `combo_yield` capture status 被送入只允许 `put` / `call` 的 opening validator，抛出
  `ValueError: unexpected opening scan scopes` 后退出。

两轮都是 `decision=none`、`message_count=0`，没有 provider attempt，也没有
`delivery_confirmed`，因此不是飞书传输层故障。

## Confirmed goal

1. 修复 capture status 的 owner 路由：opening snapshot 只消费 Sell Put / Covered Call
   状态，SP+LC Combo Yield 和 CC+LP 变体各自消费、归约并封存自己的状态与候选。
2. 在 account/config/portfolio identity 已验证的前提下，于 required-data prefetch
   之前发布当前 Account Run 的 portfolio source receipt，并让后续完整 source graph
   复用同一份不可变 receipt。这使 prefetch barrier 和 pipeline nonzero 这类运行故障
   可以在既有通知 authority 门禁下准备受限的 `fixed_failure`。

## Motivation and direct evidence

- `src/application/pipeline_watchlist.py::run_watchlist_pipeline_default()` 先将所有 capture
  status 放入仅包含 `(symbol, put|call)` 的 `expected_scopes` 校验，然后才过滤
  `combo_yield` / `variant=cc_lp`，所以合法 Combo status 必然被当成 unexpected scope。
- `src/application/symbol_monitoring.py` 在普通 Combo success/failure 路径会发布
  `strategy_mode=combo_yield`，但 `FrozenRequiredDataUnavailable` 分支只为 `put` / `call`
  发布 capture status，造成 Combo 早期数据失败与扫描失败的状态语义不一致。
- `src/application/account_run.py::run_one_account()` 在 pipeline nonzero 时立即返回；当前
  portfolio receipt 只在其后的 `publish_account_position_advice_sources()` 中发布。
- `src/application/tick_account_execution.py` 在 global prefetch barrier 上直接组装 terminal
  outcomes，不进入 `run_one_account()`。但此前已经完成 account config generation 冻结、
  prepared portfolio context 加载与 identity 校验，具备发布当前 run portfolio receipt
  的充分事实。
- source receipt 是 write-once，receipt bytes 包含 `completed_at`。早期与后期各发布一次
  会存在字节冲突；正确边界是发布一次并复用同一份已验证 receipt。

## Success signals

1. opening snapshot 的 scope 只有 `put` / `call`；合法 `combo_yield` 不再触发
   `unexpected opening scan scopes`。
2. 启用 Combo 的真实 default pipeline 组装测试同时封存 opening snapshot 和对应
   Combo/CC+LP snapshot，不用手工构造封存结果替代该路径。
3. unknown strategy mode、unknown combo variant、unexpected scope 和 duplicate scope 仍然
   fail closed。
4. SP+LC Combo 与 CC+LP 的 `data_unavailable` / `partial_data` / `no_candidate` /
   `not_applicable` 分类由各自 scope 状态决定，不互相污染。
5. 合法 prepared portfolio identity 在 prefetch 前发布一份当前-run portfolio
   receipt；后续 pipeline 成功时完整 source graph 复用它，不产生第二份 receipt。
6. required-data prefetch barrier 和 pipeline nonzero 在已有合法通知计划时可准备
   `fixed_failure`；身份、config generation、authority 或 receipt 冲突仍为 no-send。
7. 本 work unit 聚焦测试、相关 tick/notification 回归、compileall 和
   `git diff --check` 通过。

## Non-goals and scope boundary

- 不修改 scheduler processed-target / retry 语义；09:50 / 10:10 跳过的问题留给独立
  work unit。
- 不修改 OpenD 超时、重试、数据新鲜度或 provider 调用策略。
- 不修改服务退出码、通知文案、fixed-failure authority 校验标准或飞书适配器。
- 不修改 production config/runtime data，不重放、不发测试或真实通知。
- 不修改 `VERSION`，不 release、merge、deploy 或升级远端。
- 现有与本 work unit 无关的脏改动保持原样，不编辑、不暂存、不提交。

## Overdesign deliberately excluded

本轮不新增 status bus、snapshot schema、notification state、retry queue 或新服务。候选修复收敛在
`pipeline_watchlist` 已有 capture/seal 边界；身份修复复用已有 portfolio source producer
与 receipt validator，只调整发布时机和复用契约。

## Blocking open questions

无。
