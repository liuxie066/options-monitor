# Gateflow Goal Confirmation — Required Data and Multi-Account Integrity

- Work unit: `required-data-multi-account-integrity`
- Gate: `goal confirmation`
- Date: 2026-08-04
- Status: confirmed by user
- Branch: `fix/required-data-multi-account-integrity`
- Base: `origin/main@ed2531e9`

## User problem

期权行情获取已经有“跨账户单次预取、run-scoped receipt、冻结 manifest、逐账户消费”的主路径，
但获取完成条件、替代调度入口和账户事实共享仍存在七组不一致。它们会让不完整数据被声明为
ready、让公开旧入口绕过 frozen authority，或让同一 run 的账户读取不同代配置、持仓、事件和汇率。

## Proposed goal

把 canonical `tick` 建立为 scheduled 多账户运行的唯一执行 authority，并让一个 run 内所有消费者只使用
与该 run、账户、fetch plan 和事实 generation 明确绑定的数据。缺失、错账户、计划不匹配或 provider
失败必须显式 fail closed；不得通过缓存路径、旧 CLI 或重试时序静默降级成成功。

本 work unit 对应用户上一轮编号的七项问题：

1. 期权 snapshot strict subset 或 required RV 失败仍可发布成功 receipt / complete manifest。
2. `scheduler --run-if-due` 直接调用 `scan-pipeline`，绕过 account override、run workspace 和 frozen manifest。
3. `config.override.json` 先写长期账户目录且非原子，重叠 run 可消费另一 run 的配置。
4. Close Advice barrier 在 T0 规划行情，pipeline 在 T1 重读账本，但 plan 未绑定完整 position generation。
5. Option Context 缺 exact-account postcondition；零持仓账户没有合法共享 slice，导致重复账本读取和空壳缓存。
6. 跨账户 union 会越过 `--symbols` scope、重复获取 spot，并把精确 side-expiration 计划扁平化成笛卡尔积。
7. run 级共享事实没有闭合：FX 仍按账户抓取、event prefetch 失败后账户可分叉、error payload 仍把 gateway 标为成功。

## Motivation and direct evidence

- `opend_market_snapshot_fetching.py::_fallback_fetch_missing_snapshots()` 只统计返回记录数，不核对请求 code
  与返回 code 的 exact set；`multi_tick/required_data_prefetch.py` 又在新抓取后直接保存并发布 receipt，coverage
  只用于 fetch 前缓存判断。
- `scan_scheduler.py::run_scheduler()` 的 `run_if_due` 分支仅执行
  `scan-pipeline --config <config>`；`account` 只参与 decision/state，不进入子进程。
- `account_run.py` 通过 `state_repo.write_account_state_json_text()` 把 runtime config 写到
  `output_accounts/<account>/state/config.override.json`，而 repository 使用普通 `Path.write_text()`；pipeline
  消费后才复制到 run artifact。
- `tick_account_execution.py::_build_close_advice_barrier_plan()` 在预取前读取 ledger；
  `pipeline_context.py::load_option_positions_context()` 随后再次读取 ledger。requirement identity 只绑定
  lot id、quote key、RV 和 fetch binding。
- `pipeline_context.py::load_option_positions_context()` 对 fresh account cache 直接返回，没有验证
  `filters.account` 或 position row account；shared builder 又只为实际出现 lot 的账户创建 slice。
- `required_data_prefetch_planning.py::build_cross_account_prefetch_config()` 从未过滤的 base config 重加需求；
  `_build_prefetch_fetch_plan()` 为 base/lx/sy 各自规划，而 spot 没有 expiration 已具备的 run-local cache。
- `pipeline_context.py::load_exchange_rates()` 接收却不使用 `shared_state_dir`；event prefetch 失败不会阻止当前
  account pipeline；required-data prefetch 在读取 payload status 前调用 gateway `mark_success()`。

## Success signals

1. snapshot 返回严格子集、required RV 缺失、fetch plan/receipt 不匹配时，不发布 ready receipt；manifest 对该
   symbol 显式 failed/partial，完整数据仍正常通过。
2. 所有 scheduled execution 都经过 canonical tick；以 `sy` 调度不能运行 `lx` 配置，缺 frozen authority 的
   scheduled `scan-pipeline` 不能执行。
3. pipeline 只消费自己 run/account 的原子 config artifact；两个重叠 run 的配置路径、bytes 和归档互不覆盖。
4. barrier 后 ledger 变化不能静默混入同一 frozen run：各账户消费同一 position generation，或 identity mismatch
   时在通知前 fail closed。
5. 所有 Option Context 来源统一执行 exact-account validation；零持仓账户得到合法空 slice，单 run 不因空账户
   重复读取全量 ledger。
6. 显式 `--symbols`/market scope 不被 opening/base union 扩大；持仓 Close Advice 明确声明的额外 symbol 仍保留并
   可追踪；同一 physical binding 每 run 只观察一次 spot；provider 只收到 plan 声明的 side-expiration 组合。
7. 同一 run 的账户复用同一 FX/event generation；run-level event barrier 失败时账户一致 fail closed；error payload
   调用 gateway failure 路径而不是 success 路径。
8. 新增针对上述反例的 deterministic tests；相关 tick、required-data、scheduler、context、Close Advice 回归、
   compile/analyze 和 `git diff --check` 通过。

## Non-goals and scope boundary

- 不改变 Sell Put、Covered Call、Combo Yield 或 Close Advice 的评分、阈值、排序和通知文案。
- 不新增 provider、账户级 OpenD route、数据库 schema、业务状态或通用分布式锁框架。
- 不修改 `config.yaml`、生成配置、secret、生产 runtime artifact、Feishu、broker 或 option-position 数据。
- 不发送真实通知，不发布、不部署、不升级、不合并 main。
- 不在本 work unit 清理上一轮未编号的候选 CSV TOCTOU、manual multiplier 漂移、failure-budget 观测、path-risk
  死代码、receipt O(N²) 扫描或未来 multi-binding 文件碰撞；它们保留为独立后续 work unit。
- 不把当前已完成的 PR 132 lifecycle 改动带入本分支；本 work unit 独立基于 `origin/main@ed2531e9`。

## Overdesign deliberately excluded

本轮复用现有 run workspace、manifest、receipt、account context 和 provider pool，只在它们的 owning boundary
补齐 identity、atomic publication、共享 observation 和 fail-closed 条件。不会增加第二套调度器、第二套 ledger
投影、新缓存服务、新后台进程或通用事件总线。

## Blocking open questions

无。用户已于 2026-08-04 确认上述七项映射、成功信号和明确排除项。
