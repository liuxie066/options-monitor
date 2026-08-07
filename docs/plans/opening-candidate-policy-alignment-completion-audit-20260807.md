# 开仓候选策略对齐完成审计

> 日期：2026-08-07
>
> 分支：`feat/opening-candidate-policy-alignment`
>
> 结论：源码实施、离线门禁与本机 OpenD no-send 实机验证通过；
> 尚未推送、合并 main、发布或升级远端。

## 1. 合同与实施顺序

- `b4bcf2ad` 独立提交 Sell Put / Covered Call 策略合同，父提交为
  `main@ba3a9243`。
- `6332fd43` 独立提交已批准实施方案，父提交为策略合同提交。
- 第一笔源码提交 `d30a9dcb` 的父提交为方案提交，证明先锁合同与方案、后修改代码。
- 67 项策略结论的实施 owner 与退出证据映射保存在实施方案 §7；正式数值和语义只以
  `docs/candidate_strategy.md` 为准。

## 2. 阶段证据

| 阶段 | 主要提交 | 完成证据 |
|---|---|---|
| OpenD 数据合同 | `d30a9dcb`、`8fa361eb`、`ef54a626`、`22458f28` | 财报 SDK capability、5 分钟报价、合约身份、期限匹配 RV、每市场/run 财报覆盖均有 provider-shaped contract tests |
| Candidate Engine | `4170388e` | Sell Put / Covered Call 的计算、硬筛、召回和锚定收益带排序由 domain engine 唯一所有；underwriting 只投影字段并委托 engine |
| 物理账户能力 | `68d5a74c` | 现金、货币基金、持仓和 SQLite locks 按物理账户冻结；同币种优先，必要时使用最长 24 小时 OpenD FX，无 haircut |
| 不可变快照与消费者 | `5a1f045d` | 每账户/run 封存五状态 snapshot；Agent、Daily Brief、Position Advice 校验并读取同一 seal/hash |
| 旧路径删除 | `10da8e63` | 当前开仓路径无 yfinance、Sina FX、旧事件 resolver、旧评分或 runtime candidate CSV/JSONL 权威入口 |
| Shadow 与回归 | `c0586d2e`、`f2169777` | US/HK、Put/Call、空/partial/closed 重放稳定；已批准差异分类，任何未知字段差异 fail closed 并阻断晋升 |

保留的历史字段仅限 Close Advice、Combo Yield、research/archive/shadow 等已声明的只读或
独立策略边界，不进入当前 Sell Put / Covered Call 正式开仓 decision。

## 3. 2026-08-07 验证结果

| 门禁 | 结果 |
|---|---|
| Focused opening contract suite | `141 passed` |
| 完整非 HTTP pytest | `4515 passed, 10 skipped, 6 warnings` |
| Agent plugin contract/smoke | `108 passed, 1 warning` |
| HTTP quality gate | `4 passed` |
| Ruff（本次改动 Python） | 通过 |
| `compileall domain src` | 通过 |
| dependency graph `--check` | `production_modules=575, cycles=0` |
| US/HK example config validate + build dry-run | 均通过，`write_applied=false` |
| `git diff --check` | 通过 |
| OpenD 实机修正回归 | `202 passed` |

跳过项和 warning 与既有测试基线一致，均非本 work unit 新增失败。测试和实机验证
未发送真实通知，未写 Feishu/交易/券商数据，也未修改远端生产配置。

## 4. 本机 OpenD 实机验证与修正

- 本机 Futu SDK 与 OpenD 已对齐到 `10.9.6908`，并在登录后完成只读能力探针。
- OpenD 财报日历对 US/HK 均能返回数据；无事件日期能成功返回空集，因此空集
  是可信的 authoritative absence，不是 provider 失联。
- 实机发现新 SDK 的日 K 字段名为 `KL_FIELD.DATE_TIME`；网关现在将旧的
  `time_key` 合同显式映射到该字段，修复了历史行日期被 SDK 静默省略导致的 RV 不可用。
- OpenD 不支持原先假设的 `FX.USDCNH` / `FX.USDHKD` 行情代码。汇率改为从同一
  `REAL` 账户的 `accinfo_query(currency=...)` 资产展示换算比值生成，且仅在返回币种
  与 CNH/USD/HKD 请求一致时才接受；否则 fail closed。
- 本机忽略配置 `config.yaml` 及其 US/HK 生成快照已显式设为 `trd_env: REAL`；
  可提交的 example config 同步了该必填语义。
- 三次隔离、强制 no-send 的 shadow tick 均完成：
  `20260807T002550Z-71fb19` (US)、`20260807T002730Z-b52144` (HK)、
  `20260807T002831Z-ef7f74` (US 重放)。财报、组合、汇率、期权和 RV 数据均 ready；
  开仓结果为 `no_candidate`，而非缺数据；`sent=false`。两次 US 财报证据和策略 hash
  稳定一致。

## 5. 静态收敛审计

- `domain`、`src`、`scripts`、`requirements`、`configs` 中无 yfinance/Sina 正式依赖。
- opening Candidate Engine、Agent、Daily Brief、Position Advice 当前消费者无旧 score
  weights、旧 opening score 或旧 RV estimate 输入。
- `score_weights` 仅保留在配置校验器中用于明确拒绝已经删除的公共配置。
- Daily Brief 仍读取的 `combo_yield_candidates.csv` 属于独立 Combo Yield 策略，不是
  Sell Put / Covered Call opening snapshot fallback。
- Position Advice 的 `candidate_path` 局部变量指向同一 opening snapshot receipt，未生成
  第二份候选事实。

## 6. 上线边界

本审计证明当前分支源码、离线门禁和本机 no-send 实机验证通过。以下动作仍未授权、
未执行，也不得由本结论
推导：推送分支、合并 main、修改版本、创建 tag/Release、远端升级、服务重启、生产配置
迁移、真实通知或业务数据写入。
