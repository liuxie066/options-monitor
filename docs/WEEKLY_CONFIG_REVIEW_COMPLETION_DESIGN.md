# 每周配置清单周报：指标补齐与本机落地设计

- 状态：**用户已于 2026-11-12 确认；已进入 Gateflow plan**。
- 核实日期：2026-11-12。
- Work unit：`weekly-config-review-completion`；整个方案只交付一条 draft PR。
- 基线：`origin/main = 0844bb12ecf2cfc58cfe4ce20e08d5f2faf9ef54`。
- 任务分支：`feat/weekly-config-review-metrics`。
- 独立工作区：`/private/tmp/om-weekly-config-review-20261112`。
- 本文是 goal confirmation 的审阅材料。确认前只写设计，不修改实现、不提交、不推送、不安装或触发任务。

## 1. 目标、边界和成功信号

这份周报帮助操作者判断：当前配置的 strike 目标是否值得人工复核。报告先给出且只给出三个结论之一：**建议调整 / 观察 / 无需调整**，随后给理由、数据覆盖情况和两项指标。结论是人工复核提示，不自动改变策略或给出新的 strike 数值。

本 work unit 完成以下行为：

1. 指标 A：在一周窗口内，统计封存候选决策中的 strike 边界拒绝观测，按标的、市场、边界方向展示，区分重复扫描、多原因拒绝、旧码与真正的零。
2. 指标 B：展示每个已配置边界的标的现价、边界值和距离百分比，保留配置来源及现价采集时点。
3. 固定三态阈值、计算口径和降级规则，写入代码与文档。
4. 复用现有 report center 和飞书投递，更新本机现有周报 job，kickstart 一次并核实新生成的真实报告与投递回执。
5. 用户确认后，将本设计作为一个 Gateflow work unit，串行完成 plan、reviews、implementation、aggregate deepreview、draft PR、PR review 和 final closeout。

成功信号：定向回归和项目规定的 outgoing checks 通过；真实 launchd 运行生成 `reports/config_review/<run_id>/report.md`、`report.json`、`run.json`；标题之后先出现三态结论；缺失的 A/B 数据清楚可见；投递状态可追溯；只有一条 draft PR，所有 Gateflow artifact 完整。

边界：

- 不修改 `configs/*.json` 的任何实际值。
- 不修改候选准入、strike 窗口、选股、交易或持仓规则。
- 不重新设计注册表、CLI 家族、报告存储和飞书传输。
- 不迁移 daily-brief / close-advice；保留已有登记。
- 不修改 code-health / code-entropy 的脚本、plist、开关或运行状态。
- 不发版，不升级生产，不切换生产 release，不为了补数据回放或重新评估历史候选。
- 不自动合并、mark ready、请求 reviewer、创建外部 issue 或删除分支/worktree。
- 一次消息只调用一个工具；内部 review 也串行执行。

## 2. 已核实的起点事实

### 2.1 代码与既有能力

2026-11-12 执行 `git fetch origin main` 后确认：

| 提交 | 已存在的能力 | 本轮处理 |
| --- | --- | --- |
| `41fcf9174` / #1145 | 报告注册表、`om report list/run/history/show`、config_review、launchd 模板 | 复用 |
| `958e2a3ca` / #1146 | put roll 使用点刷新报价证据 | 不修改 |
| `0844bb12e` / #1147 | `reports/delivery.py`、run.json 投递回执、CLI `--no-deliver`、plist 投递参数 | 复用 |
| `29d66c6f4` / #1144 | 四种方向化 strike 拒绝码，`hard_strike` 保留用于历史和缺失 strike | 消费，不重新拆码 |

直接代码证据：

- `src/application/reports/config_review.py::build_config_review` 已读取 HK/US 用户配置并合并已有 defaults，目前没有 A 或三态判断。复查确认 v1 距离公式实际为 `(spot - boundary) / spot * 100`；本设计已确认的 v2 定义改用边界作分母，见第 5 节。
- 同模块 `_load_market_configs` / `_symbol_payload` 已拥有配置边界清单，包括 `sell_put`、`sell_call`、Combo 明确支持的腿部边界。扩展此 owner，不另写一套配置继承规则。
- `src/application/reports/prices.py::resolve_price_snapshot` 已处理显式 prices 文件与本机 brief 中的最近价格观察；不在报告中另建行情客户端。
- `src/application/reports/runner.py::execute_report` 已负责生成、落盘、投递和最终 run receipt。`reports/delivery.py::deliver_report` 已调用现有 notification owner。
- `src/application/opening_candidate_snapshot.py::load_opening_candidate_snapshot` 与 `validate_opening_candidate_snapshot` 拥有封存快照完整性和决策合同；报告不能仅凭文件名信任 JSON。
- `src/application/tick_run_workspace.py` 的快照路径为 `output_runs/<run_id>/accounts/<account>/state/opening_candidate_snapshot.json`。
- `domain/domain/engine/candidate_engine.py::evaluate_opening_candidate_policy` 产生四种方向码。`threshold` 是当次生效边界，可能来自配置、spot、成本底价或窗口比例。
- `domain/domain/insurance_underwriting.py::underwriting_fields` 中，call 的 `policy_min_strike` 可能已经是 effective min，不能把这个字段无条件称为原始用户配置。
- `src/application/opening_candidate_snapshot.py::candidate_universe_summary` 拥有 scan completion / incomplete 的投影；报告复用，不重建完成状态解释器。
- 已按 `docs/API_INDEX.md` 搜索候选、扫描状态及身份 owner；目前未登记另一套周度 strike 拒绝指标。

### 2.2 本机与生产事实

当前共享目录位于旧的 `tick-latency-instrumentation` 分支，包含其他工作；本轮未修改其中内容。任务工作区从最新 `origin/main` 新建，没有继承作废 chat 的分支或设计。

本机已存在：

- job：`com.options-monitor.report.config-review.weekly`，周一 08:00。
- plist：`~/Library/LaunchAgents/com.options-monitor.report.config-review.weekly.plist`。
- 包装脚本和已安装源码：`~/Library/Application Support/options-monitor/report-config-review/`。
- 当前包装脚本只通过 SSH 获取生产 OpenD 的只读价格快照，再运行现有 `om report run`。它尚未获取生产候选历史。
- 实际 code-health 是每天 09:17；code-entropy 日任务为 04:20，周任务为周日 01:20。
- 本地报告根目录已有两份报告；其飞书回执均为 `failed`，渠道返回 `10014 / param secret is invalid`。这不是未配置状态，也不是投递成功。

只读 SSH 观测已绑定实际生产主机、应用 symlink 和数据根目录。应用解析至 `<release-root>/4.6.31`，数据根目录为 `/var/lib/options-monitor`；主机标识与详细路径只保留在本地验收记录。

探索性统计窗口是**观测时刻向前七天**：留存快照共 1,169 份，其中窗口内标准扫描 192 份（HK 122 / US 70），读取错误为 0，只有旧 `hard_strike`，该码记录 42,018 次，没有四种方向码。这个数是探索性观测，不能直接充当正式报告的完整日历周指标。

生产仍为 4.6.31，当前方向化源码在 main。**本轮不发版，因而不能承诺首份真报告已有方向分布。首份报告必须能以 A 无数据、整体“观察”的形式正常交付。**

## 3. 报告结构：先结论，再证据

报告顺序固定为：

1. 标题、报告周期、生成时刻。
2. **结论：建议调整 / 观察 / 无需调整**，一至三条直接理由；列出触发结论的市场、标的和边界。
3. 数据说明：A 覆盖状态、快照数、市场/日期覆盖、旧码数、失败/缺失数量；B 价格来源和采集时点。
4. 指标 A：市场汇总 → 标的与方向明细；分列扫描观测数、不同合约数、仅因 strike 拒绝数。
5. 指标 B：每个配置标的/策略/边界的现价、边界值、带符号距离、边界压力、行级三态理由。
6. 原有配置清单和系统总览，保留配置来源、启用状态、DTE 等已有内容。
7. 判定规则、口径提示和数据缺口。

示意：

```text
结论：观察
理由：该期没有可证明完整的方向化拒绝数据，不能判断边界是否持续阻挡候选。
周期：2026-11-02 00:00 ～ 2026-11-09 00:00（Asia/Shanghai，右端不含）

指标 A：该期无数据
旧 hard_strike：<按本次日历周实际读取的次数>，方向不可恢复，不计为 0。

指标 B：逐标的列出现价、max/min、距离与数据时点。
```

飞书正文直接消费这份结论优先的 Markdown，不另写一套摘要判断。

## 4. 指标 A：来源、计数和可比性

### 4.1 周期

默认周期为 **Asia/Shanghai 时区上一完整自然周，周一 00:00 到下周一 00:00，左闭右开**。CLI 手动运行和周一定时运行使用相同算法。北京时间由显式 `ZoneInfo("Asia/Shanghai")` 定义，不依赖机器恰好处于哪个时区。

例如 2026-11-12 手动首跑，周期为 2026-11-02 00:00 至 2026-11-09 00:00，即 UTC `2026-11-01T16:00:00Z` 至 `2026-11-08T16:00:00Z`。

选择完整周是为了让同一周期的手动复查与定时结果可比较；现价 B 仍是本次生成时的最新可用观察，报告明确 A 与 B 的时间含义不同。

以封存的 `sealed_at_utc` 判断是否落窗；不以文件 mtime 或 run_id 字符串替代业务时间。无时区、不可解析或未来的封存时间列为数据缺口，不猜日期。

### 4.2 候选观测与方向码

只消费 `scan_mode=standard` 的真实账户封存快照，保留账户/市场/策略范围；experience 等不混入正式配置复盘。

读取 `candidate_decisions[].opening_decision.rejects[]`：

| reason | 方向 | 解释 |
| --- | --- | --- |
| `put_strike_below_min` | put 下界 | strike 低于该次生效下界 |
| `put_strike_above_max` | put 上界 | strike 高于该次生效上界 |
| `call_strike_below_min` | call 下界 | strike 低于该次生效下界 |
| `call_strike_above_max` | call 上界 | strike 高于该次生效上界 |
| `hard_strike` | 方向未知 | 不根据 spot、strike 或今日配置追算方向 |

计数规则：

- 一次观测身份为 `(run_id, account, market, candidate_id)`。复制同一快照不能重复计数；重复身份内容冲突是数据缺口。
- 一条候选在同一方向出现重复 reason 只计一次；“至少一种方向拒绝”的总数取候选并集，不能简单加总方向行。
- 同一合约在多个 tick/账户出现是多次扫描观测。正文明确称“拒绝观测数”，不称独立交易机会。
- 辅助列“不同合约数”按 `(market, symbol, strategy_family, option_code)` 去重，显示持续扫描与合约广度的差别。
- `accepted=true` 但带拒绝、身份缺失或决策不符合 owner 合同的记录不猜修；通过快照 owner 校验后才参与统计。
- 同时有收益、Delta、容量、财报等其他拒绝原因的候选计入方向拒绝总量，但不计入“仅因 strike 被拒”。
- Combo 只统计 `opening_decision` 真正封存的方向证据；不从 helper 文案或未封存的腿部诊断扩大分子。无法证明腿部映射时保留方向总量，不能映射成某条配置边界的调整证据。

### 4.3 生效边界与配置边界

A 的全部方向拒绝描述策略实际运行；它不自动等于“配置太严”。为了避免这种误读，展示两层数据：

1. **方向拒绝总量**：按封存 reason 原样汇总，可能包含动态边界。
2. **与当前配置同值的边界观测**：策略/put-call 方向明确，封存 `normalized_input.policy_min_strike` / `policy_max_strike` 与当前配置边界同值；拒绝明细的 `threshold` 也与该边界同值。数值比较只容忍浮点表示误差（`rel_tol=1e-9, abs_tol=1e-8`）。

第二层用于三态规则。它的名称必须保留“同值”，不能宣称已证明是用户配置唯一造成的拒绝。报告不重新计算 spot/成本/80%/120% 窗口，不重演策略，也不推断改掉配置后候选必然被接受。

某边界的可比决策分母 `N`：该标的、策略、方向中，具有有效封存决策、且相应 `policy_*_strike` 与当前配置同值的观测数。边界变更后，旧阈值的拒绝仍在 A 总量中展示，但不混入当前边界的比率。

- `R`：这 `N` 条中，在该边界方向上且 `reject.threshold` 同值的拒绝观测数。
- `C`：`R` 中没有其他非 strike 拒绝原因的观测数。
- `U_C`：`C` 的不同合约数。
- `D_N`：`N` 覆盖的不同上海日历日期数。
- `D_C`：`C` 覆盖的不同上海日历日期数。

这些都是已封存决策的描述，不代表报价池所有理论合约，也不证明未留存 run 的情况。

### 4.4 旧快照、无方向码和真正的零

当前 v3/v4 快照的 schema 名称本身不能证明生产者支持方向码。只看“未出现这四种 reason”就写 0，会把旧格式误报为真实零。

为使未来零值可证明，本 work unit 在现有快照 owner 中增加一个最小、可选的能力字段：

```json
{"strike_boundary_reason_codes_version": 1}
```

- 新 producer 写入该字段，并将它纳入现有 snapshot content hash。
- owner 校验：缺失代表未知，存在时只接受受支持的整数版本；不改候选决定或既有快照文件。
- 保持现有 v3/v4 和决策合同；这是附加能力声明，不把旧快照重新封装为新版。
- 本轮不升级生产，所以首跑仍可能没有能力声明。这是明确接受的降级。

A 的内部覆盖结果只用于数据说明，报告结论仍只有三态：

| 覆盖情况 | 展示 | 能否驱动“无需调整” |
| --- | --- | --- |
| 本周无有效快照，或所有快照仅旧码/无法证明支持方向码 | **该期无数据**，方向计数为 `null`，另列旧码数 | 否 |
| 部分快照可用、存在旧码、读取失败、能力未知或 scan 不完整 | **部分数据**，只展示已观测分子与明确分母，标明不足以代表全期 | 否 |
| 周期内留存快照均通过校验、能力已声明、相关扫描完整，无未知方向缺口 | **已留存快照完整可判读**，可以出现真实 `0` | 可以，仍需满足样本门槛 |

未声明能力但已经存在方向 reason 的快照，可以贡献已观测方向次数，但不能仅凭出现一个新码就把所有其他观测提升为完整覆盖。`hard_strike` 在新快照中仍可能表示缺少 strike；这种记录保持“方向未知”，不追算。

“完整可判读”只针对本次能列举和取得的留存快照；报告同时显示最早/最晚封存时间、各日期/市场/账户数量与 export 是否成功，不宣称证明了所有历史 tick 都未被清理。

## 5. 指标 B：配置清单与距离

沿用 `config_review.py` 的配置 defaults 合并与 `canonical_symbol`，每个配置标的都显示已有 owner 支持的 strike 边界，不因无现价或无拒绝数据而消失。零或负数保持既有“未设置有效边界”的语义，不参与除法。

每条边界输出：市场、标的、策略、边界键、配置来源、是否启用、边界值、spot、现价来源/采集时点、距离、边界压力、行级结论与理由。

v2 的带符号距离采用已确认定义（v1 以 spot 为分母，此处改为 boundary，并以报告 schema 升版和测试明确该变化）：

```text
distance_pct = (spot - boundary) / boundary * 100
```

为了比较 min/max 两类约束，增加一个展示和判定用的压力值：

```text
max 边界：pressure_pct = distance_pct
min 边界：pressure_pct = -distance_pct
```

正压力表示现价已经位于更容易触碰该配置约束的一侧。例如 max=100、spot=120，distance=+20%、pressure=+20%；min=100、spot=80，distance=-20%、pressure=+20%。绝对距离大本身不是建议调整的理由。

价格继续来自 `resolve_price_snapshot`：

- 定时任务优先使用本轮成功采集的生产 OpenD prices 文件；不在报告核心增加网络调用。
- 显示已有 `as_of` 的真实含义。现有外部 prices 文件提供的是采集时间，不得描述为最近成交时间；若 provider 提供交易时间，单独展示来源。
- 采集时点缺失、未来时间、距生成超过 **96 小时**、价格非正或非有限，不能驱动调整结论。96 小时覆盖通常的周末，长休市进入观察，避免用缺失数据作配置判断。
- 采集失败不得沿用上次导出的文件冒充本轮成功。可以使用现有 owner 的本机价格降级，但保留其真实来源和时间，按同一时效规则判断。
- 当前配置文件缺失、符号无法归一或边界非法，使用现有报错/数据缺口合同，不悄悄补配置。

## 6. 三态判定：固定阈值与理由

阈值作为报告 owner 的代码常量冻结，写入 `report.json.rules`。不新增用户配置键。本轮不声称这些是经过收益回测得到的最优阈值；它们是保守、可解释的人工复核门槛。

### 6.1 每条启用边界

先检查数据资格：当前边界有效、价格可用且满足时效、A 对相关范围完整可判读、`N >= 30` 且 `D_N >= 3`。不满足时为 **观察**，并明确缺的是价格、方向证据还是样本。

在数据合格时按下表判定：

| 结论 | 必须同时满足的条件 | 理由 |
| --- | --- | --- |
| **建议调整** | `pressure_pct >= 15%`；`C >= 20`；`C/N >= 30%`；`U_C >= 5`；`D_C >= 3` | 价格与配置有明显偏离，且多个日期、多个合约持续只被 strike 挡住，值得人工复核目标；减少单 tick、单合约和多原因拒绝的误导 |
| **无需调整** | `pressure_pct <= 5%` 且 `R/N < 10%` | 价格没有明显越过约束一侧，当前同值边界在已观察决策中影响较低，未形成调整信号 |
| **观察** | 其余情况 | 5%～15% 是缓冲带；拒绝较多但可行动证据不足、或价格与拒绝量相互矛盾时继续观察 |

精确边界：15% 和 30% 包含等号；5% 包含等号；10% 不包含等号。`N=0` 不做除法。配置禁用的策略仍展示清单和 B，但不进入整体结论；行级说明“策略未启用，不纳入整体判定”。

建议调整的文案必须是“建议人工复核该边界”，并注明方向；不输出“应当提高/降低到某价格”，也不声称释放的候选一定通过容量、风险或收益校验。

### 6.2 整份周报

- 任一启用边界满足“建议调整” → **建议调整**；逐条列明触发边界，并保留其他范围的数据缺口。
- 所有启用且配置有效的边界均为“无需调整”，市场文件/证据没有缺口 → **无需调整**。
- 其他情况，包括首份报告缺少方向化历史、没有启用边界、相关样本不足 → **观察**。

不增加“未知”“失败”等第四个结论。运行/投递失败仍保留在原有 `run.json` 状态，不能用业务结论“观察”掩盖执行故障。

## 7. 生产证据到本机的最小数据路径

本机没有上述生产候选历史。只改报告渲染无法让 launchd 得到真实 A，因此需要补齐现有包装脚本的只读证据预取。

数据流：

```text
本机同一个 weekly job
  -> 现有 SSH 价格采集
  -> SSH 只读读取 /var/lib/options-monitor/output_runs 下的候选快照
  -> 生产既有 snapshot owner 验证原件，输出字段白名单投影
  -> 本机 config_review 统一聚合 A、计算 B 和三态
  -> 现有 runner/store 写报告
  -> 现有 delivery.py 尝试飞书并写 run.json
```

选择紧凑投影而不是复制完整历史目录：封存候选包含大量报价和计算证据，周报只需要拒绝、身份、边界输入、时间与覆盖状态。投影会包含原路径/内容摘要，明确标为 `config_review_candidate_evidence.v1`，不伪装成原始 opening snapshot。

投影字段仅包括：

- 请求周期和导出时刻；远端 hostname、release realpath/version、runtime root。
- 每份快照的 run/account/market、`sealed_at_utc`、原始 content SHA、能力版本、原 owner 给出的扫描完成摘要。
- 每条参与统计的候选身份、标的、策略、期权类型/代码、必要 `policy_*_strike`、`accepted` 和 `rejects` 的 reason/stage/threshold。
- 读取/校验失败及旧码/未知能力情况；错误不包含账户配置或 secret。

不导出完整 `account_run_config.json`，不复制 Feishu 凭证、OpenD 登录信息、持仓明细或完整历史树。导出只做验证和字段投影，A/B/三态算法只在本机报告 owner 中计算一次。

只读 exporter 直接复用 `reports/strike_metrics.py` 的 `--export-root / --as-of` 执行入口；`deploy/launchd/run-config-review-weekly.py` 从批准的本机安装版本读取同一源码，经 SSH stdin 交给生产 venv Python，不安装文件、不修改生产 release。调用生产已经存在的快照加载/校验接口。未知接口或不兼容格式视为数据缺口，不降级为裸读并声称完整。这样不再维护一份独立的字段投影实现。

本机 CLI 增加可选 `--candidate-evidence-file`，同 `--prices-file` 一样仅用于 `config_review`；默认未传时读取显式 `runtime_root/output_runs` 中的快照，便于本机/离线运行。两条入口共用同一投影与聚合合同，不合并不同 provenance 的数据集。

外部证据必须与本次报告周期匹配，且来自本轮成功预取。预取失败以明确的空/失败证据结果继续生成“观察”报告；不得误读上次 evidence 文件。报告自身可读，但 A 明确无数据。网络错误不被当成 0。

把本次使用的投影作为 `candidate_evidence.json` 一并存入该报告 run 的不可变产物；`report.json` 保留聚合计数、分母、阈值与 source 摘要，保证后续能解释报告。继续使用现有 report store，不新建数据库或历史索引。

## 8. plist、安装和首跑

### 8.1 调度

保留现有 label 和周一 **08:00**。launchd 使用本机时区；安装前核对本机为 Asia/Shanghai 对应时区，否则先说明调度换算，不能悄悄改系统时区。

与现有任务的关系：

| 任务 | 实际调度 | 本轮动作 |
| --- | --- | --- |
| code-entropy daily | 每天 04:20 | 只读核对 |
| code-entropy weekly | 周日 01:20 | 只读核对 |
| code-health | 每天 09:17 | 只读核对 |
| config-review weekly | 周一 08:00 | 更新现有 job |

本任务 SSH 预取设有限超时，整次目标在 30 分钟以内完成，避免长时等待接近 09:17；超时后按数据缺口出报告。正常一次 kickstart 不再并发启动第二个同 label 实例，不新增独立采集 job。

### 8.2 安装产物与预览

必须以 `deploy/launchd/com.options-monitor.report.config-review.weekly.plist.template` 渲染最终 plist。模板指向本机稳定安装位置中的包装脚本；不能让定时任务依赖会被清理的 `/private/tmp` worktree。

批准的源码和专用脚本安装在现有 `~/Library/Application Support/options-monitor/report-config-review/` 下，以已通过 review 的 commit 标识记录版本。使用现有可工作的 venv Python 访问外置卷，避免用 `/usr/bin/python3` 进行外置卷预检。配置根和报告 runtime root 仍显式指向用户指定的工作目录；不把开发 worktree 的缺省 configs 当真实配置。

实施顺序：

1. 记录当前 plist、包装脚本、已安装源码版本和三项巡检 plist 的 SHA-256；备份目标周报文件。
2. 在任务目录生成最终 plist，`plutil -lint` 通过。
3. **在对话贴出最终完整 plist 内容，再执行安装**。不得只给模板或局部 diff。
4. 安装批准源码/脚本和 plist，保留现有巡检 job 不动；按当前加载状态 bootstrap 或 reload 同一 label。
5. `launchctl kickstart gui/$(id -u)/com.options-monitor.report.config-review.weekly` 触发一次。
6. 以本次开始时间和新 run_id 绑定 `run.json`，核实新 `report.md` / `report.json` / `candidate_evidence.json` 存在且内容符合本周期，不能拿已有两份报告充当结果。
7. 核实任务退出状态、报告生成状态、投递状态，并再次比对配置文件和三项巡检 plist 的 hash。

这些步骤在本设计确认后的实施范围内执行；本轮设计阶段不会提前安装或 kickstart。

### 8.3 飞书结果和可证明范围

完全复用 `reports/delivery.py` 的 `feishu_app` 通道、`resolve_feishu_bot_config`、`send_feishu_app_message` 和既有 receipt，不新增渠道参数、传输器或第二套重试。

| 实际情况 | 操作者看到的说明 | 持久化状态 |
| --- | --- | --- |
| 明确 `--no-deliver`、可选插件缺失或应用凭证未配置 | **已跳过投递**，附真实原因 | 原有 `skipped` |
| 已配置但渠道拒绝，例如当前 10014 | **飞书凭证不可用，投递失败，未确认送达** | 原有 `failed` 与脱敏 reason |
| 渠道返回成功 | **渠道已接受**，记录真实 submission result | 既有 `delivered`，不升级成更强回执 |
| 有 OM bot 可读回的消息或用户在目标会话确认 | **已在 OM bot 确认收到** | 引用实际观察，不捏造消息 ID |

用户要求“凭证不可用时明确说明”在这里落实为显式结果；配置存在而失效不能诚实地记为“未配置/跳过”。当前已知 10014 是外部限制，代码与本机落地可以完成，真实收件确认需要用户在本机安全更新凭证后复测。设计和首跑结果都不得声称已完成这项尚不可证明的确认。

## 9. 实现切片与修改面

这是一条 work unit、一条 draft PR。使用两个按可验证行为划分的切片，避免为了模块数量机械拆分；每片完成实施与一次 review loop 后继续推进。

### S1：离线输入能生成可解释的完整周报

产出：固定周期、A/B、三态、降级和 CLI 输入全部打通，fixture 既可得到三个结论，也能得到真实零/未知历史的不同展示。

预计修改：

- `src/application/reports/config_review.py`：保留清单 owner，编排 A/B/结论与 Markdown、JSON、summary；更新过时的“拒绝量不在范围内”说明。
- `src/application/reports/strike_metrics.py`（新增、单一指标 owner）：时间窗、快照投影输入、去重、分组、覆盖、同值边界证据与三态纯计算。不拆成 registry/plugin/通用规则引擎。
- `src/application/opening_candidate_snapshot.py`：仅增加可选能力字段的写入/校验，不改准入逻辑。
- `src/application/reports/runner.py`：context 增加可选 evidence 输入，把实际使用的 evidence 一并落为现有 store artifact。
- `src/interfaces/cli/report_ops.py`：一个可选参数及现有调用链透传，不引入新 CLI 家族。
- `tests/test_config_review_report.py`、`tests/test_report_cli.py`、`tests/test_opening_candidate_snapshot.py`，必要时新增 `tests/test_config_review_strike_metrics.py`。
- `tests/test_report_delivery.py`：只扩充结论优先正文与既有投递状态的集成断言，不改传输实现。

合同：报告 payload 升为 `config_review.v2`，保留原有配置/系统字段，新增 `period`、`conclusion`、`rules`、`strike_rejections`、边界评估与 provenance。现有 run/store/delivery schema 保持原义。快照能力字段为向后可选，旧 sealed 文件不改写。

S1 完成信号：同一 report CLI 可验证三态、首份历史缺口、真实零和明确分母；已有报告、CLI、snapshot 与 delivery 定向回归通过。

### S2：本机定时入口读取真实数据并产生首份报告

产出：包装脚本获得真实价格和候选证据，模板可渲染且稳定安装，launchd 首跑产生新报告和真实投递状态。

预计修改/增加：

- `deploy/launchd/com.options-monitor.report.config-review.weekly.plist.template`。
- `deploy/launchd/run-config-review-weekly.sh.template`：把现有本机包装行为纳入仓库，只增加候选证据预取和明确降级。
- `deploy/launchd/export-config-review-evidence.py`：通过现有 snapshot owner 验证，再导出最小字段投影；不执行策略。
- `deploy/launchd/fetch-config-review-prices.py`：收编现有只读价格脚本，保留现有行情路径与符号转换 owner；只为本次报告采集价格。
- 对 exporter 投影和真实 CLI + 预取边界增加最小必要集成验证，模拟 SSH 成功、失败、旧文件残留。
- `docs/REPORT_CENTER.md`、`docs/API_INDEX.md`、`docs/INDEX.md`：登记指标 owner、参数、阈值、覆盖/降级和本机运维入口。
- 本设计文档按最终裁决回写；本机安装回执与 Gateflow 状态另存 task artifact，不向仓库提交实际凭证、生成报告或安装副本。

先完成 S2 可测试实现与 review，再冻结安装候选 commit。使用该 commit 做本机安装验收；若验收要求代码变化，则修复、定向 re-review 后重新绑定安装版本。之后进入 aggregate deepreview，任何会改变已安装行为的修复都重新安装和验收。

S2 完成信号：完整 plist 已先贴出；新 run 与本次 kickstart 绑定；首份 Markdown 和数据产物真实存在；A 的旧码限制、B 时点、飞书结果明确；配置和其他 jobs hash 不变。

## 10. 验证矩阵

设计阶段不运行实现测试。确认后由 Gateflow plan 写成可直接执行的精确命令，至少覆盖：

| 行为 | 必要断言 |
| --- | --- |
| A 四方向 | 各方向准确映射；分组总数取并集；重复 reject 不加倍 |
| A 身份与扫描重复 | 复制文件不加倍；多 run/账户分别计观测；辅助不同合约数去重 |
| A 多原因拒绝 | 计入 R，总量保留；不计入 C，不能当作调整即可释放的候选 |
| A 周期 | 上海周界左闭右开；UTC 转换；运行跨周；不从 mtime 猜时间 |
| A 历史 | 仅 hard_strike → null/该期无数据；新旧混合 → 部分；未声明且零新码不写 0 |
| A 真零 | 能力字段受支持、扫描完整、样本合格且无方向拒绝 → 可证明 0 |
| A 生效边界 | 动态阈值与配置不同只入总量；call effective min 不冒充原始配置；缺证据不重算 |
| A 范围 | standard 与 experience 隔离；账户/市场/策略保留；损坏/hash 错误与缺失快照分别可见 |
| B | 按 v2 以边界为分母计算 signed distance；min/max 压力方向正确；所有已配置标的都出现 |
| B 降级 | 缺价格、非法数、无时点、未来时点、超过 96 小时均为观察理由 |
| 三态 | 15%/5%/30%/10% 的等号、N=30、C=20、U=5、日期=3；不足样本不能无需调整 |
| 整体结论 | 任一建议优先；只有所有启用边界合格且无缺口才无需调整；首份无方向历史为观察 |
| CLI/store/delivery | 新参数透传；report.json 版本和产物一致；生成状态不覆盖投递 failed/skipped |
| 预取边界 | SSH/验证失败可出观察报告；上次价格/evidence 文件不充当本轮成功；周期不符明确拒绝 |
| launchd | plist lint；本次新 run；安装 commit 绑定；现有巡检 jobs 和配置 hash 一致 |

预期定向入口：`tests/test_config_review_report.py`、新的指标测试、`tests/test_report_cli.py`、`tests/test_report_delivery.py`、`tests/test_report_store.py`、`tests/test_report_registry.py`、`tests/test_opening_candidate_snapshot.py` 与新增 exporter/包装入口测试。依据项目入口通过正确的 `.venv`/pytest 包装执行，清除 ambient `PYTHONPATH` 影响。

新增 import 后执行现有 dependency-graph `--check`；如存在合法图更新则用生成器更新。按 `docs/GUARDRAILS.md` 与 OM pre-push skill 对实际 outgoing tree 执行必要 gate，不用 main push 的绿灯代替对应 PR 检查。无关全量重跑不充当新增验证结论。

## 11. Gateflow 执行与交付

用户确认本文后，goal confirmation 固定为本设计目标，不自动增加新架构或生产改动：

```text
plan -> planreview -> fix/re-review -> accepted plan checkpoint
-> S1 implementation -> deepreview -> fix/re-review -> S1 checkpoint
-> S2 implementation -> deepreview -> fix/re-review -> S2 checkpoint
-> aggregate deepreview -> fix/re-review -> checkpoint
-> ready-to-open-draft-PR -> push -> create one draft PR
-> PR review -> fix/re-review -> accepted PR review evidence -> final push
-> draft-PR-pass -> final closeout
```

实际本地 commit 第一行服从仓库 hook：`<type>(<scope>): <subject>`；在 subject 表达对应 accepted gate，而不直接使用会被 hook 拒绝的裸 `gateflow:` 模板。

每个 gate 记录 artifact、decision、验证、finding 状态、已分类的剩余风险和下一个未完成入口。所有 review 串行。确认后不在普通 gate 结束处额外等待确认；遇到改变本设计范围的阻塞才停。

final closeout 报告：变更、验证、docs、finding 状态、首份真实报告的本地路径、调度及投递状态、剩余限制/owner、唯一 draft PR URL、下一入口。本 work unit 不是 issue，不额外创建外部 issue/comment。

本轮止于 draft PR 和 final closeout，不包含合并；未合并分支/worktree 保留，后续合并和删除依用户单独授权。

## 12. 风险、限制和回滚

| 风险/限制 | 处理 | 分类与 owner |
| --- | --- | --- |
| 生产方向码尚不可用 | 首份 A 无数据，整体观察，保留旧码数；不发版补数据 | 本轮降级覆盖；未来生产升级是用户管理的独立工作 |
| 历史没有能力声明，无法证明真零 | 新 producer 最小声明；旧期保持未知/部分，不回写历史 | 本轮覆盖；既有历史的信息损失不可恢复 |
| 留存目录不等于所有历史 tick | 明示可枚举范围和逐日/市场/账户数量，不承诺全量未删 | 本轮说明覆盖；留存策略改造不在范围内 |
| 同一合约反复扫描使次数很大 | 观测数与不同合约数并列，三态要求多日期/多合约 | 本轮覆盖 |
| 方向码包含动态边界和多原因拒绝 | 只用同值边界、可比分母、strike-only 作提示；不声称唯一因果 | 本轮覆盖 |
| 预取数据量和 SSH 时延 | 只导出周窗口必要字段，有限超时，失败明确降级；不复制完整历史 | 本轮覆盖 |
| 飞书 secret 当前无效 | 保留 failed 和 10014，明确未送达，不静默重试或更换渠道 | 用户安全更新凭证后完成收件复测；本轮不读取或修改 secret |
| 渠道 sent 不等于目标会话读回 | 只报告证据可支持的级别 | 若无读回接口，用户在 OM bot 确认 |
| 阈值是复核规则而非收益模型 | 固定、透明、有样本门槛，不自动改配置 | 后续阈值变化需独立确认，不在本轮扩展回测 |

代码回滚：保持既有 registry/runner/store/delivery 合同；需要回退时生成针对本 work unit 的反向改动，经同一项目检查后交付，不触碰用户配置。

本机回滚：使用安装前保留的周报 plist、包装脚本和源码版本恢复**同一个周报 label**并 reload；不修改另外三个巡检任务，不删除已生成报告、旧安装快照、分支或备份。本轮安装授权不包含清理这些产物。

若只有 SSH 或渠道不可用，优先保留可解释的本地报告，明确 A/投递缺口，不自动改远端、改密钥或更换通知渠道。

## 13. 等待确认的具体方案

请审阅以下绑定选择：

1. 用上一完整上海自然周做 A，生成时最近价格做 B，两种时间明确并列。
2. 三态使用第 6 节固定门槛；方向总量与“同值配置边界”证据分开。
3. 旧快照不写假零；添加最小 producer 能力字段支持未来真零，本轮不升级生产。
4. 通过已有 weekly job 的 SSH 预取获得真实候选投影，核心算法只有一份；不新增投递实现。
5. 保留周一 08:00；完整 plist 先贴后装；首跑允许因历史缺口得到“观察”。
6. 两个行为切片、一个 Gateflow work unit、一条 draft PR；已知 Feishu secret 无效和生产尚无方向码如实作为外部限制交付。

**用户确认已收到。下一入口依 Gateflow 状态 artifact 记录推进；本设计确认范围保持不变。**
