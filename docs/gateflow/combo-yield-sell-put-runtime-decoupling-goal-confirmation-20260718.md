# Gateflow Goal Confirmation — Combo Yield 与 Sell Put 运行解耦

- **Gate**: goal confirmation
- **Work unit**: Combo Yield 与 Sell Put 的运行耦合审计与解耦
- **Date**: 2026-07-18
- **Branch**: `codex/diagonal-combo-yield-lifecycle`
- **Status**: awaiting user confirmation

## 目标

让已经拥有独立产品身份的 Combo Yield，在每个 symbol 的运行编排中也成为独立策略 step：是否运行只由 Combo Yield 自身配置和其必要输入决定，不再要求 Sell Put step 被启用或成功完成。

## 动机

当前产品架构已声明 Combo Yield 与 Sell Put、Covered Call 平行，但实际执行仍通过 `run_sell_put_scan_and_summarize()` 尾部触发。这导致 Combo Yield 的可用性和故障域被 Sell Put 的开关、前置过滤及异常路径隐式控制，产品所有权与运行所有权不一致。

## 直接代码证据

1. `src/application/symbol_monitoring.py`
   - `want_yield_enhancement = bool(market_want_put and yield_enhancement_policy.enabled)`：Combo Yield 明确依赖原始 Sell Put `enabled=true`。
   - symbol pipeline 只有一个 `run_sell_put_scan_fn` dependency；当 `want_put or want_yield_enhancement` 时调用 Sell Put runner。
   - Combo Yield 没有独立 dependency、独立调用或独立异常边界。

2. `src/application/sell_put_steps.py`
   - `run_sell_put_scan_and_summarize()` 先执行 Sell Put，再无条件在函数尾部调用 `run_combo_yield_scan_and_summarize()`。
   - 两个策略共享同一调用栈；Sell Put scan、label、cash filter、underwriting 或其外围代码抛出异常时，控制流到不了 Combo Yield。

3. `src/application/combo_yield_steps.py`
   - Combo Yield 实际已具备独立 runner。
   - 它会独立扫描自己的 funding-put universe，并不需要 Sell Put 的候选结果才能配对；`df_sell_put_labeled` 只用于 legacy inline attachment。
   - 因此“Sell Put 无候选”理论上不应阻断 separate Combo Yield；但当前调用位置仍使其受 Sell Put runner 的前序执行影响。

4. required-data / prefetch 路径仍存在相同 gating：
   - `src/application/required_data_prefetch_planning.py`
   - `src/application/multi_tick/required_data_prefetch.py`
   - 两处均以 `want_put and yield_policy.enabled` 判断 Combo Yield 数据需求，Sell Put 禁用时不会为 Combo Yield 请求 put/call 数据。

5. 现有测试只覆盖了“Sell Put 被账户 prefilter 禁用但市场配置仍启用”时继续调用复合 runner；没有覆盖产品级 Sell Put disabled、Sell Put runner exception、Sell Put empty candidates 三个核心状态的独立行为矩阵。

## 已确认的当前行为矩阵

| 场景 | 当前行为 | 期望方向 |
|---|---|---|
| Sell Put 配置 `enabled=false`，Combo Yield `enabled=true` | Combo Yield 不取数、不执行 | Combo Yield 独立取数并执行 |
| Sell Put 原始启用，但账户 prefilter 将 `want_put=false` | Combo Yield仍可通过 Sell Put runner 执行 | 保持可执行，但改由独立 step 表达 |
| Sell Put scan/label/filter 抛异常 | 同调用栈中断，Combo Yield不执行 | Sell Put 失败不应自动阻断 Combo Yield；各自 fail-closed、独立记录 |
| Sell Put 无候选但没有异常 | Combo Yield会再次扫描独立 funding-put universe，仍可能产出组合 | 保持此语义，并用显式测试锁定 |
| Combo Yield 自身无 funding put / 无 pair | 产出空结果及 trace reason | 保持 fail-closed 和可诊断性 |

## 成功信号

1. symbol orchestration 中存在显式、独立的 Combo Yield step/dependency，而不是由 Sell Put runner 尾部触发。
2. `sell_put.enabled=false + combo_yield.enabled=true` 时，required-data planning、prefetch 和 symbol execution 都仍请求所需 put/call 数据并运行 Combo Yield。
3. Sell Put runner 失败时，Combo Yield 的运行由独立故障边界决定；Combo Yield 自身可继续运行并返回/记录自己的结果。
4. Sell Put 无候选时，Combo Yield 仍基于自身 funding-put universe 扫描，不复用空 Sell Put 候选作为阻断条件。
5. Sell Put 与 Combo Yield 的 summary 顺序、通知汇总、既有 separate 输出契约保持兼容。
6. focused tests 覆盖至少上述三种关键场景，并通过相关 pipeline、required-data、Combo Yield 和 notification tests。
7. 架构文档明确产品所有权与运行所有权均已独立。

## Scope boundary

### 本 work unit 包含

- per-symbol orchestration 的 Sell Put / Combo Yield step 分离；
- Combo Yield 独立 enablement 计算；
- required-data planning 与 scheduled prefetch 的独立 Combo Yield gating；
- 必要的 dependency injection、summary aggregation、故障隔离与诊断；
- 回归测试和对应架构文档更新。

### 本 work unit 不包含

- 改变 Combo Yield 配对、评分、underwriting、现金过滤或生命周期业务规则；
- 改变生产配置值、账户配置或通知开关；
- 改变 trade intake、position pairing、close advice 或 broker-facing state；
- 为未来策略建立通用 plugin framework / strategy registry；当前只做最小的第二个明确 step；
- 清理所有 legacy `yield_enhancement` 命名；只在触及的运行边界使用当前 canonical `combo_yield` 语义。

## 关键设计约束

- Combo Yield 可以复用 Sell Put 的 funding-put 配置参数和底层扫描/过滤函数，但复用能力不等于依赖 Sell Put step 的执行结果或成功状态。
- separate 输出是当前 canonical 行为；legacy inline/both 模式如继续支持，必须明确其对 Sell Put artifact 的依赖，不能让该兼容路径重新绑死 separate Combo Yield。
- 所有策略失败都应 fail-closed；“独立故障域”不代表吞掉异常或伪造成功结果。
- 不修改真实通知、生产配置、持仓或 broker-facing 数据。

## Blocking open question

无。默认产品决策为：当 Combo Yield 自身启用且输入可获得时，它应独立运行；Sell Put disabled、失败或无候选都不是 Combo Yield 的隐式禁用条件。

## Residual risks at this gate

- legacy `output_mode=inline|both` 需要 Sell Put labeled artifact；计划阶段需决定最小兼容边界。
- symbol-level exception handling 当前可能由更上层统一承担；计划阶段需确认独立故障隔离放在 orchestration 还是 runner adapter，避免吞掉基础设施级致命错误。
- dirty worktree 含其他已确认保留的在途改动；后续 commit 必须精确 stage 当前 work unit 文件。

## Next gate

用户确认本目标与边界后进入 `plan`。
