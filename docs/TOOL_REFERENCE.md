# Tool Reference

本文说明如何发现和安全调用 `om tools` Tool Gateway。它不手工复制每个工具的完整 schema。`om-agent` 保留为兼容入口，复用同一解析和执行实现。

运行时权威：

```bash
./om tools spec
```

源码权威：

- `src/application/agent_tool_registry.py`
- `src/application/agent_tools/*`
- `src/application/tool_execution.py`

## 统一入口 `om`

| 入口 | 受众 | 作用 |
|---|---|---|
| `./om` | 人工操作者 | 配置、扫描、账本、研究、服务和运维 workflow |
| `./om tools`（兼容 `./om-agent`） | 外部 agent、脚本、结构化集成 | JSON manifest、单工具 JSON envelope、账户配置命令 |
| `./om bot`（兼容 `agent`、`assistant`） | 消息入口与自然语言助手 | Control / Bot，不属于 Tool Gateway |

人工命令可用 `om help` 按场景查找，完整参数用 `om <命令> --help`。例如 `om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE` 默认预览并从标的识别市场，追加 `--apply` 后通过 YAML 配置事务重建快照；应先把占位符换成自己的标的和行权价上限。`om tools run --tool manage_symbols` 是同一配置源的结构化 Agent 入口。两者均不应直接编辑生成的 JSON。

`om tools` 不维护对话状态，不负责多步规划，也不是自动交易 Agent。`om agent` 是 Bot 兼容别名，不能用来调用 Tool Gateway。

### 参数、环境与输出兼容

`om tools` 完整承接 `om-agent` 的 `spec`、`run`、`add-account`、`edit-account`、`remove-account`，原子命令参数不变，可用 `om tools <子命令> --help` 查询。`run --input-file` 覆盖 `--input-json`；输入须为 JSON 对象，坏 JSON、不可读文件或非对象返回 `INPUT_ERROR` envelope 和退出码 2。参数错误由 argparse 报到 stderr、退出码 2；工具成功为 0、失败为 2，stdout 只输出原 manifest/envelope。

网关直接进入 `src/interfaces/agent/cli.py`，不经过人工命令的 `command_environment`。进程调用保留原网关环境引导：`run --env-file` 显式加载；未传该参数且未传 `--no-local-env-file` 时按原规则加载 `OM_ENV_FILE` 或代码根目录的 `.env/options-monitor.env`。`--no-local-env-file` 跳过自动引导，显式 `--env-file` 仍加载；它不会清除已存在的进程设置或 `OM_ENV_FILE`。密钥是否可从 env 文件读取仍由原 secret backend 决定。

实例选择由原 `resolve_runtime_root` 负责（显式作用域、有效 `OM_RUNTIME_ROOT`、用户记录、代码目录）；配置请求中的 `config_path` 优先于 `config_key` 默认路径，相对配置及输入文件相对调用 cwd，env 文件相对代码根目录。账户和市场仍由工具 schema、配置身份与既有业务 owner 校验；`om tools` 不另选账户、市场或实例。账户增改删保留 `--config-yaml/--config-path`、`--rebuild-runtime-root` 和原配置发布/审计流程；默认写操作仍需 `OM_AGENT_ENABLE_WRITE_TOOLS=true` 与 `--confirm`，`--dry-run` 只预览。

人工命令使用各领域原有格式与退出码；新增双格式的命令使用局部 `--format text|json`，不添加全局格式开关。`om position-advice evaluate` 默认人工展示；`--format json` 返回原 `position_advice.output.v1` 业务对象，`om tools run --tool position_advice_evaluate` 返回含该对象的原 envelope。`om position-advice roll-down` 同样默认人工展示，JSON 格式返回原 `short_put_roll.output.v1` 业务对象；Tool `short_put_roll_candidates` 保留 envelope。成功、部分结果、合法空结果、范围外未搜索、插件未启用/不可用均完成请求并退出 0，证据缺口保留原因；输入或执行失败为 2，不能只凭退出码判断查询结果是否可用。

## Manifest

查看完整 manifest：

```bash
./om tools spec
```

只列工具名：

```bash
./om tools spec | jq -r '.tools[].name'
```

查看一个工具的 schema、示例和风险：

```bash
./om tools spec |
  jq '.tools[] | select(.name == "runtime_status")'
```

每个工具至少声明：

| 字段 | 含义 |
|---|---|
| `name` | 稳定工具名 |
| `input_json_schema` | 实际输入约束 |
| `read_only` | 是否修改产品事实或配置 |
| `risk_level` | 当前执行风险分类 |
| `side_effects` | 可能物化的本地或外部结果 |
| `requires_confirm` | 是否需要确认语义 |
| `requires_env` | 依赖的环境 gate |
| `safe_default_input` | manifest 声明的安全默认值 |
| `output_contract` | 结果中可见事实、freshness 和证据形态 |

注意：

- `read_only=true` 不等于“绝不写任何本地文件”。部分工具不修改产品状态，但会物化 report/cache。
- `defaults.write_tools_enabled` 反映当前环境是否允许写工具；工具自身 metadata 不会因开关而消失。
- 不要假设所有写命令都有同一套 `--apply` / `--confirm` 参数；以单个 manifest 为准。

## 当前工具分类

以下分类基于当前 registry，完整 schema 仍以 `spec` 为准。

### 诊断与运行

- `healthcheck`
- `config_validate`
- `runtime_status`
- `scheduled_tasks_read`
- `runtime_runs`
- `runtime_logs`
- `scheduler_status`
- `operation_timeline`
- `quality_status`
- `version_check`
- `version_update`
- `project_context`
- `project_files`
- `receipt_read`

### 候选与 symbol

- `scan_opportunities`
- `option_candidates`
- `candidate_rank_explain`
- `candidate_filter_explain`
- `symbol_resolve`
- `symbol_config_read`
- `manage_symbols`

### 收益与桥接

- `option_performance_report`
- `trade_attribution_read`
- `portfolio_pnl_bridge`
- `portfolio_cash_bridge`

`option_performance_report` 提供 MTD/YTD/自然月/自然年的期间期权净现金流
（`period_option_net_cashflow`，含以前开仓、本期平仓的现金）及独立展示的开仓批次现金结果
（旧 `option_net_cashflow`）。卖方/买方胜率、收益率的原数值与口径不变。
`schema_version=option_performance.report.v3` 和 `metric_semantics` 声明两种选择口径；
rows/breakdowns 仍是开仓批次明细，分页也携带 schema/口径并将其绑定到来源哈希，
symbol 页不附全账户期间总额。旧服务未返回新字段时，期间现金明确不可用，不拿旧字段替代。
实际日均未了结占用本金含跨期持仓，仅按已发生时间加权，不作为收益率分母。
现金保留原币，各现金指标的 CNY 总额只消费已落库 `cash_conversion.v1` 证据；不查当前汇率。
成交附带费用随成交时间入期，迟到更正重述原期间；有独立事实时间的新扣费/退款才按独立时间入期。
`actual_fee_missing` / `fee_estimated` 表示本次账本快照缺少所需实际费用证据，
不能单独证明正在同步、同步失败或重建丢失；受影响的实际净额保持不可用，不以估算补齐。
只有独立、当前的同步证据支持时才可称“手续费待同步”，失败或长期异常仍需明确保留。
输入哈希变化不能证明重建主体、丢失原因或必须重建/backfill。
`freshness.as_of` 表示统计截止，不是费用同步时间；`/income` 按此标签显示原值。
正股交易、指派/行权交割现金、PnL 和行情刷新不属于该报告。HTTP 外层保持 v1。
纯领域离线接口及冻结证据限制见[离线合同](OPTION_CASHFLOW_OFFLINE_CONTRACT.md)。

### 持仓、现金与组合

- `option_positions_read`
- `query_cash_headroom`
- `get_portfolio_context`
- `portfolio_query`
- `portfolio_assignment_scenario`

账户事实必须通过对应工具取得，保留账户、来源、币种和数据时间；不能用记忆、旧回答或自行推算补齐。
`portfolio_query` 返回 PM 的记录，不等同于券商实时资金。其 `scope` 是实际 HTTP 查询范围；
PM v1 部分响应不回传账户，因此 `assessment.scope_binding=request` 不声称独立验证了响应账户。
响应中出现与请求矛盾的账户时，适配器返回错误，不把其他账户的数据重新标成请求账户。

普通 PM 查询新增 `coverage` 与 `assessment`。`assessment.status=reported_facts_only`
只支持引用 `supported_claims` 中的数据源报告事实；`unable` 表示当前账户结论无法判断。
外层 `ok=true` 只表示调用成功。缺少完整记录数、局部失败、缺少有效时间或来源标记过期时，
原始记录仍保留用于诊断，但不能当成完整、当前账户事实。`freshness.observed_at_utc` 是来源时间，
`retrieved_at_utc` 是读取时间，不能刷新旧事实；时效政策由 PM 数据集负责，不混用券商现金 TTL。
`full_report` 的完整性只针对报告摘要，不代表完整持仓清单；分布和历史查询也只覆盖所请求的视图。

`portfolio_query(view=cash)` 包含 cash 与 MMF，`amount` / `by_currency` 保留 PM 原始持仓数量和
声明币种，不证明货基赎回价值、可提现余额或可交易资金。未返回券商、币种等字段就标注未观测；
不能按港股持仓猜测现金为 HKD，也不能因加总相等而断言不存在重复资产。资产占比优先读取
`distribution` / `overview`，不从历史答案或不完整明细拼凑总额。

CSP 现金担保与余量使用 `query_cash_headroom`。其 `assessment.supported_claims`
复用现有现金快照、占用完整性与汇率资格；原币可用不代表 CNY 合计可用。
`query_cash_headroom` 与 `get_portfolio_context` 均要求显式非空账户，不从默认账户推断。
前者是现金余量，后者是资金/持仓上下文；都不是新合约的完整开仓批准。
`option_positions_read` 是本地账本，不单独证明券商当前仓位；`portfolio_assignment_scenario`
只模拟已有合约全部指派。`option_candidates` 的合约合格也不代表账户能卖。
没有同账户的有效完整开仓能力证据时，回答“无法判断账户能否加仓”，不能手算一个可卖张数代替。

### Wheel 生命周期

- `wheel_end`
- `wheel_call_intent`
- `wheel_call_linkage`
- `wheel_intent`
- `wheel_linkage`
- `wheel_branch_decision`
- `wheel_activation`

### Close Advice

- `close_advice_read`
- `position_advice_evaluate`：按需行情及隔离请求报告，不写账本或发送通知
- `short_put_roll_candidates`：已有单笔 short put 的向下移仓查询，按需行情及独立请求报告，不下单、不写账本、不通知
- `covered_call_roll_candidates`：有股票覆盖的单笔 short call 上滚查询，分别展示净收现金、净支出及条件性交股价提升，不下单、不写账本、不通知

### 通知

- `preview_notification`：用 `account`、`market`、`date`、`revision` 选择已持久化的成功 Daily Brief，并通过 canonical Daily Brief query renderer 生成只读正文；不扫描、不发送、不改变 delivery state，也不接受 legacy alert/change 文本或路径
- `notification_perception_read`
- `daily_decision_brief_read`

历史 `monthly_income_report` 和 `portfolio_capital_bridge` 已移除。Research 的 collect 与 archive inventory/pull/verify 使用 `./om research` 人工 CLI，不注册成通用 Tool Gateway 工具。

## 常用调用

### 健康检查

```bash
./om tools run --tool healthcheck \
  --input-json '{"config_key":"us"}'
```

`healthcheck` 对 Ledger 只做 SQLite read-only inspection：数据库不存在时会报告
warning，但不会创建数据库、迁移 schema 或重建 projection。Tool Gateway envelope
的 `ok` 表示工具执行成功；readiness 结论以 `data.summary.ok` 为准。对应的人类
`om healthcheck` / `om doctor` 命令会在 readiness 为 false 时返回非零退出码。

### Runtime 状态

本地 checkout：

```bash
./om tools run --tool runtime_status \
  --input-json '{"config_key":"us"}'
```

生产 release：

```bash
./om tools run --tool runtime_status \
  --input-json '{"config_path":"/var/lib/options-monitor/config.us.json"}'
```

指定 `run_id` 时，该 run 自身的 tick metrics 是通知计数权威；只有字段缺失且
shared `last_run.json.run_id` 与所选 run 相同时才会回退 shared 计数。通知明确
失败、部分失败或 unresolved duplicate risk 会进入 `summary.warning_codes`，
并令 `summary.ok=false`。

### 通知感知证据

```bash
./om tools run --tool notification_perception_read \
  --input-json '{"runtime_root":"/var/lib/options-monitor","limit":10}'
```

root 解析顺序是显式 `runtime_root`、`OM_RUNTIME_ROOT`、repo fallback；结果会报告
root 来源及每个 JSONL 文件的 `ok`、`missing`、`valid_empty`、`tail_only`、
`partially_corrupt` 或 `unreadable` 状态。共享审计文件超过 1 MiB 时只读最近尾部，
`tail_truncated=true`、`summary.status=partial`；历史匹配总数未知，不能解释为“没有通知事件”。

### 指派后资产分布

```bash
./om tools run --tool portfolio_assignment_scenario \
  --input-json '{"accounts":["lx","sy"]}'
```

该工具必填 `accounts`，可选 `scenario=short_put|short_call|all_short`（默认 `all_short`），
分别假设仅全部未平 short Put、仅全部未平 short Call、全部未平 short Put/Call 被指派。按账户所选券商的股票和现金作为底仓（富途经 OpenD，含 MMF；长桥走独立适配器）；
`portfolio.holdings.enabled=true` 时只补充 PM Holdings 中明确为非富途来源的资产，
PM 的富途副本及同账户已直接读取的长桥镜像不计入。OM SQLite 的 open short put/call lot 提供指派输入，
配置的行情能力提供股票及期权标的现价；PM 仅在 Holdings 开启时补充非富途资产。
启用通过 `om config holdings set` 预检 PM scoped 来源质量；查询自动纳入所请求账户的合格非富途资产，
不保存券商批准名单。来源清单缺失、不一致或 broker 无法分类时拒绝该 PM 补充，保留已核验的直接券商基线并标 `partial`。
PM 补充现金不增加直接券商期权资金覆盖；缺少同账户、同券商完整基线时，起始/终局股数、净资产及依赖权重未知。
长桥现金估值、费用及身份限制见[情景设计](PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md#券商与-pm-可选能力扩展2026-11-01)。
输出 `portfolio.assignment_scenario.v1`，主资金口径为 CNY，
Long Option 完全排除。工具是纯读；业务 `status=partial|unavailable` 仍可处于成功的 Tool Gateway envelope 中，调用方必须同时检查 envelope `ok` 和业务 `data.status`。

结果新增 `position_sizing`：统一以指派费用前的非期权净资产计算 `net_assets_cny` 与
`weight_of_net_assets_by_symbol`，与 CSP / CC 候选使用同一股数和现金变换。
指派明细、费用、资金覆盖、分布和 `position_sizing` 均使用所选情景的同一组合约；
未选方向不产生假设交割或费用，剩余期权市值仍不纳入报告。
CLI 使用 `./om portfolio assignment-scenario --accounts lx --scenario short_put`。
原 `distribution` 的总资产权重、费用与负债语义保留；证据缺失或净资产非正时新比例为 null。

### 查询运行历史

```bash
./om tools run --tool runtime_runs \
  --input-json '{"limit":10}'

./om tools run --tool runtime_logs \
  --input-json '{"run_id":"<run-id>","kind":"tool","lines":50}'
```

未显式传路径时，这两个工具按 `OM_RUNTIME_ROOT` 定位 `output_runs` 和 `logs`，
未配置时才回退到 repo root。

### 指定标的卖出开仓候选

`option_candidates` 查询当前合约条件，入口为 Tool Gateway 和 Bot；历史过滤原因仍用
`candidate_filter_explain`。输入只有 `queries`，1–5 项，每项必填 `symbol` 和
`strategy`（仅 `short_put` / `short_call`）。标的接受代码别名，例如 `00700`、
`HK.00700`、`0700.HK`、`US.NVDA`；中文名称由对话层先唯一校准，不作为结构化输入。

```bash
./om tools run --tool option_candidates \
  --input-json '{"queries":[{"symbol":"NVDA","strategy":"short_put","max_abs_delta":0.25,"dte":[7,60]},{"symbol":"NVDA","strategy":"short_call","max_abs_delta":0.25,"dte":[7,60]}]}'
```

可选 `strike` 为有限正数，`dte=[min,max]` 为整数闭区间，`1≤min≤max≤365`；
`max_abs_delta` 为 `[0,1]` 内的有限数字，按 `|Delta| ≤ 上限` 筛选 Put 和 Call。
同时查询两种方向时，为同一标的各提交一项 `short_put`、`short_call`，行情事实共用。
省略时继承有效配置/策略默认值；显式参数只覆盖本次查询。拒绝 null、额外字段、
字符串行权价、裸 put/call、买入/平仓策略及规范化后的重复查询。调用者不能填写
account、config_key、路径、runtime root 或 OpenD endpoint。

标的不必已加入监控配置。未配置标的使用现有策略默认值，行情路由仅复用该市场唯一的有效
OpenD endpoint；必要配置缺失或路由冲突时返回 `unable`。不要把
`symbol_config_read` 的 `found=false` 当成查询禁令，也不要为一次查询直接编辑生成的
`config.us.json` / `config.hk.json`。

结果按输入顺序给出 `results`：每项 `outcome=found/none/unable`，并独立保留
`coverage.status=complete/partial/unavailable`；`none` 只用于完整评估且无合格合约，
部分数据下找到候选仍可为 `found`。`effective_filters` 包含行权价上下界、DTE 和
`max_abs_delta` 及其 `max_abs_delta_source=request/config`；
无界端点为 null，配置不可用时整个 filters 为 null。每项最多展示 3 个候选，含合约、
到期日、行权价、Delta、参考卖价、单份净权利金、期间净收益率、年化净收益率和履约义务；`displayed/omitted`
说明展示量。候选字段 `period_net_return` 和 `annualized_net_return` 都以小数比例返回，对话中用百分比并列展示。
年化直接复用引擎结果，按 `期间净收益率 × 365 ÷ reference_dte` 计算；实时行情的 `reference_dte` 等于 `dte`，
休市参考使用报价参考日到到期日的期限。年化是估算，不代表未来一年可持续获得同样收益。
整体 `completion` 单独说明完成程度，输出超限时可将 evidence 置为 null 或压缩候选；保留候选的两项收益率字段。

仅筛合约，不检查账户现金、股票覆盖、成本底线或集中度。财报不淘汰合约，只展示查询日
至到期日内事件及 `days_before_expiry`；同日事件不判断日内先后，缺失时明确“财报信息不完整”。
参考卖价不保证成交，费用及净收益是估算。查询不写账本、配置、正式报告或通知；
既有行情缓存和共享限频记录仍可能更新，FX 查询固定 `write_cache=False`。

应用总响应预算至多 60 秒，并受 Host 剩余预算限制；超时保留已完成结果，未完成项保留原因，
在途 SDK 调用不保证硬中断。Bot 对整批市场授权后才执行，详见
[Bot 合同](BOT_DESIGN.md#指定标的卖出开仓候选)。Hermes bridge 位于其所属环境，按 OM manifest
自动发现只读工具并添加 `om_` 前缀。桥接的离线 schema/JSON 转发验收不替代真实 MCP 握手和远端部署验收。

OM 升级不会自动刷新已建立的 Hermes 会话工具清单。若旧会话没有 `om_option_candidates`，
在实际使用的 Hermes 会话执行 `/reload-mcp`，完成其确认步骤后核对工具可见；不要仅凭另一
主机或新起的桥进程能列出工具，就判定原会话已恢复。验收还应通过 MCP 调用标准查询，并
将完整返回文本解析为单个 OM JSON envelope。OpenD 控制台诊断写入 stderr，不能混入结果
stdout。

休市时按需读取富途快照，并以完整交易日历核验正股源日期属于最近有效交易日；支持收盘后、
周末、节假日及港股午休，下一常规时段开市即停止沿用。`data_quality.mode=closed_market_reference`
表示“休市参考”，同时返回 `reference_date`、`underlying_observed_at` 和 `valid_until`；
`evaluated_at` 仅表示本次评估时间，不替代源时间；候选 `snapshot_received_at` 保留本次快照接收时间。
期权快照仍须在本次决策前 300 秒内取得，
bid/ask、Delta、IV 没有独立源时间，不能宣称为精确收盘价或保证成交价。

每个合约仍核验身份、乘数、最小价位、证券状态及双边报价。休市 bid 为零或报价缺失时保留
数据缺口，不以最新成交价补齐；Delta 缺失也不能判断满足上限。`none` 仍只用于完整评估后的
空结果。`dte` 按查询当天筛选；候选 `reference_dte`、年化和 term-matched RV 绑定参考行情日期，
避免跨日后仅因时间缩短而提高参考年化。财报展示区间仍从查询当天起算。
普通计划扫描不采用本查询的休市资格，继续要求实时证据。

### 候选解释

```bash
./om tools run --tool candidate_rank_explain \
  --input-json '{"run_id":"<run-id>","account":"lx","mode":"put","top_n":5}'

./om tools run --tool candidate_filter_explain \
  --input-json '{"run_id":"<run-id>","account":"lx","symbol":"NVDA"}'
```

普通 CSP/CC 的新策略记录区分 `risk_delta`（超过上限）和 `risk_delta_unavailable`（来源、时间、数值等不可验证），保留具体原因。delta 是硬门槛，不是 rank driver；历史记录不得凭新默认补造阈值或来源。

两者读取已有 artifact，不重跑扫描；运行根路径按显式 `runtime_root`、
`OM_RUNTIME_ROOT`、repo fallback 的顺序解析。

`candidate_filter_explain` 还支持从通知投递证据定位轮次：`run_selector=latest_notification`
解析该账户在 `notification_date`（ISO 日期，默认按运行时主机本地时区的今天）实际送达的
最近一次通知对应的 run；找不到匹配通知时 fail-closed 返回 `DEPENDENCY_MISSING`
（`details.reason=no_notification_run`），不会静默回退到其他轮次。

### 扫描

```bash
./om tools run --tool scan_opportunities \
  --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":5}'
```

`scan_opportunities` 会读取外部数据并物化本地报告。它不发送通知，但不是 no-write 诊断。

### 现金与持仓

```bash
./om tools run --tool query_cash_headroom \
  --input-json '{"config_key":"us","account":"lx"}'

./om tools run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'

./om tools run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"events","account":"lx","position_effect":"close","limit":5}'
```

`query_cash_headroom` 是 pure-read，并以 `write_cache=false` 查询，不持久化本次 cash query。

现金读取与扫描、Wheel、指派情景共用 `portfolio_context_service`：只按独立现金源时间和有效配置 `runtime.portfolio_context_ttl_sec`（缺省 900 秒，必须为正整数）决定缓存复用；`get_portfolio_context` 不再接受单独的 `ttl_sec`。

返回的 `cash_snapshot` 包含 `status`（fresh/stale/unknown）、`reason_codes`、`source_observed_at`、`evaluated_at` 和 `max_age_sec`；仅 fresh 可用于现金容量。FX 缺失影响换算值，不改变原币现金可信度。历史报告保留封存时判定，缺少原现金证据时保持不可用。

`option_positions_read` 不写账本，但当前时点查询可能从 OpenD 读取报价。

`action=list` 支持结构化 `query`；非空白的 `query.account` 优先于顶层 `account`，省略、null 或空白时回退到顶层账户。质量门禁与账本查询使用同一个有效账户，并保留配置账户范围校验。此优先级适用于直接 Tool Gateway；Bot 仍拒绝顶层、嵌套或可信固定账户之间的冲突。

`option_positions_read action=events` 读取 canonical SQLite `trade_events`，不会读取报价：

- `limit` 默认 10，范围为 1–20；`position_effect` 当前只接受 `close`。
- 结果按 `trade_time_ms DESC, event_id DESC` 排序。`has_more=true` 时，将原样返回的
  `next_cursor` 用于下一次调用；后续页可改变 `limit`，不需要重复筛选条件。
- cursor 固定首次查询的 market、账户权限、筛选条件和事件成员边界。首次查询后写入的事件，
  即使业务时间更早，也不会进入该 cursor 流。
- `symbol` 先按标的身份规范化；例如 HK 查询中的 `700.HK` 会匹配账本里的 `0700.HK`。
  无法识别或不属于所选市场的代码会报输入错误，不会返回看似完整的空页。
- `include_total=true` 才计算冻结成员集合的 `total_count`。cursor 有效期为 30 分钟；过期、
  签名错误、权限或筛选条件变化都会明确失败，不会自动从头查询。
- cursor 签名子密钥由运行服务从既有 `inbound.operation_hmac_key` 做固定域派生，不需要
  单独配置游标密钥；缺少 inbound 密钥时返回 `DEPENDENCY_MISSING`。轮换该主密钥会使
  尚未过期的 cursor 失效。
- `snapshot_exhausted=true` 表示当前冻结集合已读完。过期后重新查询会建立新集合，因此可能
  与旧查询已经返回的记录重叠。
- 首次查询即 `snapshot_exhausted=true` 时，返回结果覆盖当前完整查询，Bot 会明确说明
  已全部返回且没有更多记录；`has_more=true` 时则明确说明仍有下一页。继续页即使读到末尾，
  单页证据仍只覆盖该页，不能单独声称覆盖完整查询。
- 请求超过单页 20 条时不得静默截断；应说明单页上限并使用分页。cursor 过期后不得自动续读，
  应说明查询快照已过期并要求重新发起查询。

继续读取示例：

```bash
./om tools run --tool option_positions_read \
  --input-json '{"config_key":"us","cursor":"<next_cursor>","limit":5}'
```

### Option Performance

```bash
./om tools run --tool option_performance_report \
  --input-json '{"config_key":"us","account":"lx","period":"mtd"}'
```

### Close Advice

读取由计划 Tick 生成的 sealed 报告：

```bash
./om tools run --tool close_advice_read \
  --input-json '{"config_key":"us","query":{"option_type":"put","side":"short"}}'
```

`close_advice_read` 不刷新持仓或行情，也不生成新建议。调度与主动评估均使用同一 sealed required-data 路径；显式历史路径中的非 sealed 行会投影为 `not_evaluable`。
新报告只支持 short put/call，并仅返回 `close`、`hold` 或
`not_evaluable`。它不生成新仓、roll、replace 或 reallocate 建议。

主动评估（需安装持仓建议可选包并启用）：

```bash
./om tools run --tool position_advice_evaluate \
  --input-json '{"config_key":"us","account":"sample","symbols":["NVDA","AAPL"]}'
./om position-advice evaluate --config-key us --request-file /path/to/request.json
```

手工请求示例：`{"account":"sample","positions":[{"input_id":"manual-1","input_as_of_utc":"2026-10-30T15:00:00Z","symbol":"NVDA","option_type":"put","expiration":"2026-12-18","strike":100,"contracts_open":1,"multiplier":100,"currency":"USD","premium":2}]}`。请填写自己的持仓事实与时间；此示例不是有效行情。休市参考保留原始 DTE/价格；没有兼容封存记录时不可用。该工具显式声明行情读取与本地报告写入，属于本地物化能力，不是 pure-read；业务账本与通知无写入。

### Short Put 向下移仓

`short_put_roll_candidates` 是 Position Advice 的同级主动查询，复用同一可选插件及启用边界；原 Close Advice 止盈政策仍不产生 roll 建议。

```bash
./om position-advice roll-down --config-key us --request-file /path/to/roll-request.json
./om position-advice roll-down --request-file /path/to/roll-request.json --format json
./om tools run --tool short_put_roll_candidates --input-file /path/to/roll-request.json
```

请求文件为 JSON 对象：`{"account":"sample","lot_id":"<existing-lot-id>","max_new_expiration":"YYYY-MM-DD","min_new_strike":90,"limit":10}`。请替换账户、真实 lot 与日期；`account`、`lot_id`、`max_new_expiration` 必填，最大到期日没有默认值。`min_new_strike` 可省略，提供时必须为正数且低于旧 strike；进入移仓范围后还必须严格低于有效现价，否则为输入错误且不请求期权链。`limit` 为 1..50 的整数，默认 10，只限制排序后的展示。可在文件中选择 `config_key`；CLI 的 `--config-key us|hk` 覆盖文件值，账户/市场必须与旧 lot 相符。

查询仅替换一个旧 lot 的全部剩余张数；旧仓价内、平值或价外幅度不超过 5% 才搜索。新仓同标的、同张数、较低 strike 且严格价外，到期日不早于旧到期日且不超过请求上限，两腿扣费净增现金必须大于零。人工展示并列显示 strike 降幅、延期、新仓价外幅度、净增现金年化、新仓到期归零最高年化及相对旧仓变化（pp），并保留两腿费用、资金分母、DTE、来源/时点及缺口原因。净增现金是现金流，不等于交易盈利；旧 ask 买回与新 bid 卖出是单腿现金估算，不保证组合执行。

JSON 业务输出为 `short_put_roll.output.v1`，含旧仓快照、评估时间、请求范围 `scope`、覆盖 `coverage`、已排序 `candidates`、展示/省略数、原因及独立报告路径。`status` 表示执行/插件状态，`outcome` 区分：`not_applicable` 为旧仓价外超过 5%、`coverage=null` 且未搜索；`none` 为覆盖完整的合法零候选；`unable` 为数据缺失/过期或未评价范围使结论不足；`found` 为已找到候选，仍可能 `status=partial`。部分排名仅覆盖已评价集合，不能据此排除缺口中的机会。`disabled` 表示插件未启用；`unavailable` 为查询不可用，插件缺失与行情依赖失败通过原因码区分。新腿已确认不合格且证据完整时计入排除；真实缺失或陈旧证据仍保留覆盖缺口。查询按需读取行情并写独立请求报告；报告写入失败以 `REPORT_WRITE_FAILED` 返回执行错误，CLI 退出 2，不提供未保存成功的报告路径。不下单、不改账本、不发通知，也不接入 Tick 或 Daily Brief。完整经济口径见[移仓合同](CLOSE_ADVICE_CONTRACT.md#short-put-向下移仓主动查询)。

### Covered Call 上滚

`covered_call_roll_candidates` 与 Short Put 滚仓共用 Position Advice 安装/启用边界，人工入口为：

```bash
./om position-advice roll-up --config-key us --request-file /path/to/cc-roll-request.json
./om position-advice roll-up --request-file /path/to/cc-roll-request.json --format json
./om tools run --tool covered_call_roll_candidates --input-file /path/to/cc-roll-request.json
```

JSON 请求必填 `account`、已有 short call 的 `lot_id`、`max_new_expiration`（YYYY-MM-DD，必须晚于旧到期日，无默认值）；可选 `max_new_strike > old_strike`、整数 `limit`（1..50，默认 10，仅限制展示）。旧仓价内/平值/价外不超过 5%，先以合格账户股票快照核实全部未关闭 Call 的覆盖；新合约须更高 strike 且严格更晚到期，可仍价内。

业务对象为 `covered_call_roll.output.v1`，保留与 Put 相同的 `status/outcome/coverage` 语义；`stock_coverage` 独立记录持股依据。确认无股或覆盖不足返回 `success/not_applicable`、`coverage=null`；缺失/陈旧持股证据为 `unable`。`credit_roll`、`cash_neutral_roll`、`debit_roll` 分开标注；debit 的双腿扣费净支出必须严格小于 `(新 strike−旧 strike)×覆盖股数`，输出同时展示净支出、每股与总行权价提升、条件性交股净改善。资金分母统一为 `spot×覆盖股数`，按条件性交股改善年化排序；这是两边最终均交股的条件比较，不是已实现利润或预期收益。交股是 wheel 第二步，接受原仓交股仍是有效选择，上滚不保证消除提前指派。只查询并保存独立报告，不下单、不写业务账本、不通知；详情见 [CC 滚仓契约](CLOSE_ADVICE_CONTRACT.md#covered-call-上滚主动查询)。

### Daily Brief

```bash
./om tools run --tool daily_decision_brief_read \
  --input-json '{"account":"lx","market":"US"}'
```

该工具读取最近一次可靠成功快照，不扫描、不发送、不确认 delivery。

### Symbol 写入

只读 list：

```bash
./om tools run --tool manage_symbols \
  --input-json '{"config_key":"us","action":"list"}'
```

编辑请求先看 manifest 中的 schema、write predicate、env gate 与 confirm 要求。不要把 `OM_AGENT_ENABLE_WRITE_TOOLS=true` 当成自动授权；它只打开执行门，仍需精确目标和工具级确认语义。

## JSON envelope

调用：

```bash
./om tools run --tool <tool-name> --input-json '<json>'
```

成功或失败都返回结构化 envelope，常见字段包括：

```json
{
  "schema_version": "1.0",
  "tool_name": "runtime_status",
  "ok": true,
  "data": {},
  "warnings": [],
  "error": null,
  "meta": {}
}
```

集成方应：

1. 先判断 `ok`；
2. 保留 `warnings`、freshness 和 evidence；
3. 读取 `error.code` / `error.details`，不要解析 traceback 文本；
4. 不把 missing / partial / stale 转成零或成功；
5. 不根据自然语言 description 猜 schema。

完整 envelope 与 launcher 合同见 [Agent Integration](AGENT_INTEGRATION.md)。

## 权限与副作用

Tool Gateway 的写门禁针对“实际请求产品/配置写入”的非只读工具。以下两类需要区分：

1. 产品或配置写入：例如 symbol edit、VERSION apply，需要匹配工具的 env gate、dry-run/confirm 和精确目标。
2. 本地物化：例如 scan、portfolio input preparation、Close Advice report，可能写 report/cache，但不会因为 `read_only=true` 自动经过统一 write-tool env gate。

自动化调用前应检查 manifest 的 `side_effects`，并为允许的输出目录设置明确 runtime root。未知工具名会返回结构化错误，不要 fallback 到任意 shell 或内部 Python 模块。

## 与 Bot / Control 的关系

Tool Gateway 与消息入口是不同能力面：

- Tool Gateway：外部调用方选择一个公开工具；
- Bot：Host 投影允许的只读工具，回答自由问题；
- Control：显式命令、pending operation、人工确认和审计。

Bot 不能因为 Tool Gateway 注册了某个写工具就直接写入。详细边界见：

- [OM Capability Surfaces](OM_AGENT_CAPABILITY_MAP.md)
- [Inbound Control](INBOUND_CONTROL.md)
- [Bot v2 Design](BOT_DESIGN.md)

### `trade_attribution_read`

只读当前配置账户的成交策略归属、候选身份、原因和数量覆盖。输入 `account`（必填），可选
`execution_key`、`symbol`、`status`、`cursor`、`limit`（1–100）及配置选择。响应返回 `market`、`rows`、
`next_cursor` 和证据完整性。`symbol` 使用规范化标的身份；无法识别或与所选市场不符会报输入错误。
`next_cursor` 是不透明的续页值，绑定首次查询的账户、市场和筛选条件；续页可省略筛选条件，
但显式改动会报错。游标有效期为 30 分钟；旧版仅含事件 ID 的游标须重新查询。
账户、物理 broker 身份与配置不一致时不能确认；查询不更新 Inbox 或 Control operation。
`prepare_confirmation=true` 会观察当前持仓容量并生成确认所需的 `input_hash`。
已标为 `proposal_expired` 的组合可在此预览中重新匹配当前完整开放两腿和历史已送达候选；
只有唯一的精确匹配可供人工确认。默认本地读取仍阻断过期提案，并显示 `combo_proposal_expired`。
预览不更新提案；确认事务重新核对当前证据和哈希，同时写入两腿及组合身份。旧预览须重新生成，
重复确认沿用既有幂等回读。不会延长旧提案期限或自动重新激活。
人工操作见 [Inbound Control](INBOUND_CONTROL.md#已入账成交的策略归属)。

归属规则只有一套当前写入路径。人类 CLI `om trade-intake attribution-migrate` 负责按物理来源和账户切换到 v2；
`--effective-from-ms` 指定未来的成交时间切换点，默认只预览。预览列出旧 v1 启用时间 T0、v2 切换时间 T2、来源和成交数量。
写入前须停流并排空 writer，提供 `--writers-stopped`、同一来源的完整预览 manifest、新备份目标与 `--apply --confirm`。
迁移只追加 v2 生效记录，不改旧行。T0 至 T2 之间及未切换来源的未归属成交，在所需证据完整时仍可人工确认；仅成交时间不早于 T2 的成交可自动归属。
已启用 v2 的来源不能重复迁移，也不能回填过去的启用时间。历史候选证据缺失时先恢复权威证据，
否则需要另行设计并授权历史修复；本入口不能通过改启用时间或直接确认单腿绕过证据门。
部署与生产迁移各需单独授权；简报和成交回执持续显示待人工确认，不把缺失证据或账户总额变化当作归属证明。
