# 按监控窗口生成研究采集清单

当前长期方案：`watchlist-schedule-run` 支持无 end_date 的 `research.watchlist-schedule.v2`，前三个交易日仅首轮验收，之后继续积累，不逐三日续期。旧有限日请求、有限 schedule v1 和模板用法保留，不能作为长期运行默认结束条件。

2026-10-31 获批研究部署：固定研发包 `f0d46fbf48261177ede3a36adc64f2d3714079351e843b5c8397b7113bfeaf9b` 已在 `liuxie-incus` 独立安装；716份源码、离线依赖、两市场真实Linux隔离、正式启动和独立回读通过，研究timer均enabled/active。FX及公司行动提前300秒供给、报价仍每日一次，总预算301次应用请求/日。11月2日起无结束日期运行，当前真实观察0；研究占用988,672,000字节、余量3,147,141,120字节，符合2GiB/2GiB边界。新归档使用批准数据根内 `archive/{hk,us}/ancillary-f0d46fbf4826/`，避免覆盖旧不可变schedule；旧包及原件保留，生产OM未改。这是独立研究包部署，不是OM发布版本；下文“本地开发/未部署”保留各切片原验收时点，不代表当前部署状态。真实首三交易日与来源用途/覆盖尚未验收。

每日编排入口：`om research watchlist-day-run --request <固定请求.json> --expected-request-sha256 <摘要> --day-dir <独立每日目录> --budget-root <研究计量根> --runtime-root <生产runtime隔离边界> --prune-root <清理边界> --opend-host <端点> --opend-port <端口>`。默认只预览；`--confirm` 仍只执行该日显式安全窗口允许的发现/观察，不启动扫描。当前由获批独立研究schedule调用，不属于生产扫描服务。

请求 `research.watchlist-day-request.v1` 包含固定 `date`、`previous_day` 与 `scan_input/calendar/policy/discovery_budget/observation_budget/discovery_window/observation_window` 七份 `{path,sha256}` 引用。名单输入为已导出的 `research.watchlist-scan-input.v1`；不读取生产配置或选择 latest。两份安全窗口均为原 `research.safe-window.v1`、绑定原 scan-input SHA；仅在派生输入及新计划生成后重绑摘要，时间区间不变。发现窗口必须早于观察窗口，预算分别沿用原发现/多片 owner，并使用同一组明确研究计量目录。

`previous_day` 为 null 或 `{path,request_sha256}`：绝对路径指上一日的 `prepared/`，核验其 `day.json` 和全部固定原件后承接 tracking。前驱缺失不隐式改选更旧记录；也不能同时提供另一份 previous_tracking。新名单版本必须由 owner 重新导出并用于后续日期，不在运行时偷读实时监控配置。

每日期独立准备目录及 archive，首次执行发现并固定计划，后续触发只核验原件并复用已有多片 collector。在发现窗口内首次准备前，以 OM instrument policy 的市场时区核对名单标的的当地日期与请求日期；错配直接拒绝，预览及确认模式都不查询行情、不创建每日目录。后续采集复用原计划，不以其他市场已跨午夜为由拒绝。重复触发不重做发现或抢已有 claim；漏窗保存 missed，中断标 abandoned 且不自动重放；没有资产则保存 no_observable_scope 与发现缺口。独立调度应覆盖发现窗口与每市场观察窗口，具体 timer/路径/配额须在启用单中批准；本轮没有启用调度。导出仍使用原 `research archive watchlist-export`，OB 通过显式日包集合验包，不导入 OM runtime。

状态：隔离开发，未部署。`watchlist-scope` 只读取显式导出的离线文件，不连接 OpenD，不读账户配置或账本；daily/discovery/collect 的真实行情请求另受 confirm、安全窗口与预算控制。

独立调度模板为 [每日 service](../services/systemd/options-monitor-research-watchlist-day.service.in) 和 [每日 timer](../services/systemd/options-monitor-research-watchlist-day.timer.in)。它们是安装前的文本模板，不是已安装服务。每个获批日期各渲染一对唯一名称的 unit：冻结该日 request 原件和外部 SHA，填入固定安装 Python/source、已有用户、独立日目录、SDK HOME、完整预算根及生产隔离目录。`ExecStart` 只调用本节的每日入口，禁止改成完整扫描入口；真实安装前还要先运行去掉 `--confirm` 的同一命令预览。

timer 只填入获批日期的发现触发点和港美观察触发点，均用明确的 `YYYY-MM-DD HH:MM:SS UTC`，不填通配日期；观察点来自 owner 的交易日/半日市与时区规则，不全年硬编码美国 UTC 时间。无对应市场观察的日期应删去该市场的 OnCalendar 行。发现触发点必须落在批准的发现窗口内，发现结束早于首个观察窗口；每个实际取数仍受原安全窗口与预算约束。`Persistent=false` 且 service 不自动重启，错过窗口不追采。有限期限结束后，按启用单停用全部本次研究 timer，保留原件。

service 只允许写入当日日目录和 SDK HOME；计划、源码及前驱日原件由 `ProtectSystem=strict` 保持只读，生产 runtime/config/清理目录不可访问，沿用现有用户且不新建账号。部署前必须核对所有占位符已替换、路径存在且不重叠，在目标 Linux 运行 `systemd-analyze verify`，并回读实际生效的 namespace、写入边界和全局 drop-in。本地 CLI 测试不证明 Linux 隔离生效；共享 OpenD 配额和具体磁盘额度仍须另行批准，模板没有默认额度。

每日新输入可引用昨日 `tracking.json`（外部原字节 SHA 和可知时间）；旧计划和旧分母不改。空名单也允许继续跟踪旧研究池。未知截止不凭到期字符串或缺报价退出，已知权威截止才允许退休。

`simulation_tracking` 是可选的整份 OB `optionbench.simulation-tracking.v1` 引用，字段为 `path/sha256/pointer/observed_at_utc`，Pointer 必须为根 `""`。OB 从明确的模拟状态和冻结 provider 身份导出期权/未结束订单及非零股票，不传播现金、数量或实盘账本。跟踪状态 v2 保留这份义务快照；没有新报告继续沿用，明确的新完整空快照才表示模拟义务结束。旧 v1 期权跟踪仍可读取。

没有对应期权的股票另分成 `hk-s0000/us-s0000` 一类分片，每片最多 200 个；单片计划 v4 只请求股票快照，不伪造期权包。共享预算中股票片通常一次请求，期权片通常两次。日历、观察窗口、限流、独占目录和导出合同继续复用；OB 独立核对标的观察分母，期权零分母仍为 null。每日请求编排也已有本地入口（见文首），尚未部署或启用自动任务。

```bash
om research watchlist-scope \
  --input /private/research/input.json \
  --expected-input-sha256 <输入原字节的SHA256>
```

默认仅预览。加 `--confirm --output-dir /private/research/new-scope` 才保存；目录须在 Git checkout 外且不存在。输入根下的来源文件不可为链接，必须匹配摘要。输出目录 0700、文件 0600，不覆盖原件。

## 输入

根字段为 `schema_version=research.watchlist-scope-input.v1`、`scope_id`、`as_of_utc`、显式非空 `fx_pairs`（OM native `HKDCNY` / `USDCNY`）、非空 `symbols`、`previous_tracking`。

每个标的包含 `underlier_code`、非空 `owner_windows` 引用数组、`chain` 引用。每个引用固定为 `path`、`sha256`、`pointer`、`observed_at_utc`；路径相对于输入目录，Pointer 使用 JSON Pointer，空字符串指根；来源时间不得晚于范围时间。

- `owner_windows` 指向 OM `RequiredDataFetchPlanBundle.to_debug_dict()` 完整输出。校验 `symbol` 后使用各 `side_plans[].strike_window`；不接受没有标的身份的单个 side plan，不复制 OM 窗口公式。
- `chain` 指向 provider 原始行数组。保留整份原件，核对 `code`、`stock_owner`、`option_type`、`strike_price`、`strike_time`；不接受相互矛盾或重复的合约。
- `previous_tracking` 为 null 或上一份 `tracking.json` 的引用。非空时沿用未结束合约和初次入池时间，不因出带、名单删除或到期字符串而停止。仅在明确 `last_trade={cutoff_ms,source,observed_at_utc}` 已到期时退休；来源须为 OM lifecycle owner 支持的权威来源，未知保持 null。

## 范围和输出

每份 OM 窗口 `[L,U]` 扩成 `[0.95L,1.05U]`，包含端点。多个窗口取并集，不填空隙。按各市场当地日期取 0—90 天普通 Put/Call，完全不按 Delta、收益率、价差、资金或策略入选结果缩减范围。

输出包含原始输入、引用原字节、窗口及计数清单 `watchlist-scope.json`、累计 `tracking.json`、每市场最多 200 合约的 `batches/<id>/scope.json`。分片可交给已有 `research observation-series-plan`；建计划后，采集时应将范围产物目录作为 `--ancillary-dir`，以随导出保留窗口/链来源，不能只交付分片中的代码列表。

`ok=true` 只代表命令执行成功。当前原始链行不能独立证明全到期链发现覆盖，因此始终报告 `chain_coverage_unverified/incomplete`，空返回不等于不存在合约。快照批次数不含链发现、重试、共享配额和耗时，不是部署预算。

扫描预筛封存、原生回执、独立 0—90 天链发现及每日请求编排已完成本地切片；OB 共同日终索引、显式跨日包汇集和休市结转已有本地验证。仍未部署每日滚动调度或远端全分片采集，本地测试不代表真实采集已续期。

## 扫描原生回执输入（本地开发）

现有扫描在策略筛选前封存名单规划、实际取得的链和快照。市场快照保留 SDK 返回的额外列及逐次物理调用回执；缓存保留原观察时间，不产生新的物理响应。成功、空返回和失败均可保存，研究封存不会增加行情请求，也不授予 `quotes` 资格。标准进程、inprocess 和 subprocess 路径均已接入；没有 run ID 的旧调用不新增研究回执。

名单和预筛原件分别在显式 required-data 根的 `source_receipts/research_watchlist/`、`source_receipts/research_prescreen/`，文件 0600，内容不可覆盖。它们只包含名单/规划和市场资料，不导出账户配置或账本。Provider 原件指 SDK 响应的 JSON 投影；非有限数保留为 null，不声称网络传输字节原件。封存完成时间不证明报价更新时间。

`watchlist-scope` 同时接受 `schema_version=research.watchlist-scan-input.v1`。字段为 `scope_id`、`as_of_utc`、`fx_pairs`、`watchlist`、`captures`、`previous_tracking`；跟踪语义与上文一致。`watchlist` 指一份名单规划 receipt，`captures` 是同 run 的预筛 receipt 引用数组；每个引用仍包含 `path/sha256/pointer/observed_at_utc`，其中 Pointer 必须为空字符串，时间必须等于对应 receipt 的完成时间。路径从显式导出根解析，receipt 引用的 payload 也必须存在且摘要匹配；不搜索生产目录。

适配器复用原始 owner 的 `fetch_plan`，逐端点窗口取并集；名单中的同一标的只出现一次。同 run 多次链请求按合约身份去重，冲突拒绝。输出保留 receipt 和 payload 原字节及相对路径，另写 `derived-input.json`；不会改写原输入。没有取得链或窗口的名单标的保留明确缺口，不因缺失而从发现范围消失。

扫描可能只请求策略期限或窗口内的一部分链和报价。此入口不把它升格为 0—90 天全链，不把 5% 余量冒充扫描已有报价；仍保留 `chain_coverage_unverified/incomplete`，余量报价需独立研究采集并记录其真实时间。

## 独立链发现（本地开发，未部署）

`watchlist-discover` 消费上述带外部摘要的 scan-input v1，以原名单为固定分母，直接调用已有 OM 到期日、DTE 选择及完整链 owner。每标的重新枚举到期日，按请求时的市场当地日期选择 0—90 天（含端点、无到期个数截断），对每个到期日请求 Put/Call 完整静态链，不传行权价或策略过滤。禁用研究发现缓存、不自动重试；共享 OpenD 配额仍有消耗。

```bash
om research watchlist-discover \
  --input <scan-input-v1.json> --expected-input-sha256 <输入摘要> \
  --safe-window <固定窗口JSON> --expected-safe-window-sha256 <窗口摘要> \
  --budget <发现预算JSON> --expected-budget-sha256 <预算摘要> \
  --output-dir <全新研究目录> --budget-root <研究预算根> \
  --runtime-root <排除的生产runtime> --prune-root <排除的清理目录> \
  --opend-host <明确主机> --opend-port <明确端口>
```

默认预览不联网、不写目录；只有 `--confirm` 才允许连接。发现预算为 `schema_version=research.watchlist-discovery-budget.v1`、正整数 `max_calls/max_bytes/min_free_bytes`，其中 max_calls 是本次发现的总应用请求预约数，失败不退款，不含 SDK 握手。预算根可重复，须包含本次研究输出；磁盘是逐请求操作预算，不是硬配额。安全窗口复用 `research.safe-window.v1`，其 `series_plan_sha256` 在此绑定 scan-input 原字节摘要；显式 UTC 半开 intervals 不变。每次物理请求在 owner 限流等待、预算核查完成后重验窗口；不改共享生产限流目录。

先落盘固定名单及来源，再连接。每次请求保留预约、实际请求/返回时间、完整 SDK 行投影或失败，保存原响应后交回现有 owner 校验。空到期集合不会触发 owner 的“不限到期”查询；成功空返回、解析失败、未执行和预算拒绝分开记录。目录不可覆盖，限流文件只留研究本地，不作为交付证据；中断后不得盲重放原目录。

产物 `discovery-plan.json` 固定分母；`discovery-manifest.json` 记录每标的结果、请求数和原件清单。`scope-input.json` 为 scan-input v2，比 v1 多 `discoveries` receipt 引用数组；原名单、预筛和 tracking 引用保持不变，新增完成时间只表示本次发现可用时间。该输入可直接交回 `watchlist-scope`，保留扫描与独立发现的链原件，并按既有 5% 窗口选报价范围。缺一份发现回执、失败或冲突不得删除名单标的。

发现 `complete` 仅指 owner 成功枚举并请求了选定的到期日；**不是报价或回测合格**。当前 OB 尚未独立认证 provider 全链完备性，scope 继续保留 `chain_coverage_unverified`；不以 producer 自报 complete 关闭缺口。合约最后交易日、新鲜度和附属资料仍单列。

## 多分片计划、预算和交付（本地开发，未部署）

全名单不要分别启动每个 200 份分片。使用同一个冻结计划：

```bash
om research watchlist-series-plan \
  --scope-dir <scope目录> --expected-scope-sha256 <watchlist-scope.json摘要> \
  --calendar <日历原件> --policy <OM政策原件> --budget <显式预算JSON> \
  --start <开始日期> --end <结束日期> --output-dir <全新计划目录>

om research watchlist-series-collect \
  --plan-dir <计划目录> --expected-plan-sha256 <watchlist-series.json摘要> \
  --safe-window <固定安全窗口原件> --expected-safe-window-sha256 <窗口摘要> \
  --archive-root <独立长期目录> --budget-root <研究占用计量目录> \
  --runtime-root <排除的生产runtime> --prune-root <排除的清理目录> \
  --opend-host <明确主机> --opend-port <明确端口>

om research archive watchlist-export \
  --series-dir <独立长期目录> --expected-plan-sha256 <计划摘要> \
  --cutoff <明确UTC时间> --output-dir <全新导出目录>
```

`watchlist-series-collect` 默认预览；只有显式 `--confirm` 才写研究目录并允许请求。`--budget-root` 可重复，必须覆盖研究归档，并应计入研究安装、计划和导出等预算内路径；重叠根只计一次。`--prune-root` 可重复且不能为空。运行时不读取这些排除目录。

预算原件固定为 `schema_version=research.watchlist-budget.v1`、正整数 `max_calls_per_slot`、`max_bytes`、`min_free_bytes`。安全窗口固定为 `schema_version=research.watchlist-safe-window.v1`、`watchlist_series_sha256`、`intervals`；每个 interval 为 UTC 半开区间 `start_utc/end_utc`，应完整覆盖允许采集的 60 秒槽。这里没有扩大采集的远端默认额度。

计划按市场和观察时点聚合所有分片；常规每片一次期权、一次标的快照，重试同样消耗全槽预算。复用同一 OM 限流 owner 和研究目录内的状态；持久请求预约在 owner 实际调用之前落盘，中断或失败不退款。拿不到预算的请求不会被标为已调用 provider。文件锁阻止同归档的并发采集/导出。达到磁盘或请求上限时禁止新的请求，原件保留；磁盘是逐请求操作预算，不是硬配额，单次返回或落盘可能跨过阈值。

采集前初始化全部分片的固定输入。漏采、失败、晚到、不满足安全窗口和已有 claim 均不删除分片；既有 claim 不重采。全部分片使用同一个导出 cutoff，包含窗口/链/tracking 原件并绑定复合实现摘要。归档、计划、安全窗口和原件不允许覆盖。

OB 使用 `scripts/verify_watchlist_series_offline.py` 聚合验证，每片复用已有 `verify_observation_series_offline.verify_series`；不导入 OM 或复制金融资格公式。分别输出应采合约观察覆盖、已取得原件的可追溯率及研究用途覆盖；零分母为 null。所有目前的报价观察版本仍保留截止未验证、盘口源时间未知等不足，`exit=2/incomplete` 不等于采集失败，也不等于完成回测。

当前已具备扫描预筛封存、独立发现及显式每日请求编排；名单来自冻结导出，尚未自动同步实时配置，也未启用新的远端研究定时任务。这个有限冻结范围入口不能冒充动态名单持续采集已完成；还须容量预览、部署审批和真实限定试运行。

本地相关回归最终1666项通过，其中清单39项、多片25项；OB单包/序列/多片消费者136项通过。属于本地合成与受影响回归，未跑全仓交付门禁、未部署、未续采真实数据。

2026-10-31 本地休市估值接线复用领域估值 owner 的原生日历回执解析，观察序列的复合来源清单相应纳入该 owner 文件（当前46份），旧包及其45份来源锚不改写。OB 日终索引 ready 仍只表示原件输入齐备；实际休市结转须固定 OM 验证资格，不刷新旧报价时间，不把原件覆盖认作收益认证。

## 每日准备与日历维护（本地开发，未部署）

### 显式附属资料原件（2026-10-31，本地增量，未部署）

`watchlist-series-plan` 可选同时传入 `--ancillary <manifest.json>` 和
`--expected-ancillary-sha256 <原字节SHA-256>`。两者必须同时提供；不提供时保持旧行为。
manifest 只接受 `schema_version=research.market-ancillary.v1` 和非空 `files` 数组；每项精确包含
`path`、`size`、`sha256`，其中 path 相对 manifest 所在目录，size 是原件字节数。只接受有摘要的私有常规 JSON
原件，拒绝链接、越界路径、重复路径、保留的 `manifest.json`、limiter 及含账户/凭证内容的资料。

每日请求 v1/v2 可选增加 `ancillary: {path,sha256}`，指向同一 manifest；有限 schedule v1 继续消费这些每日请求。
预览先校验外锚及所有原件，不联网、不写文件；正式准备保存到 `prepared/ancillary/`，计划保存到
`scope/supplemental/`，每片继续通过原 ancillary 路径封存并导出。包括 manifest 在内均保留原字节，来源变化拒绝，
不允许覆盖或修补。本节显式输入验收时尚无长期自动供给；后续本地接线见下一节，已部署固定包没有因此更新。

原件可随包交付不等于资格成立：FX 仍交既有 OM owner 验证，OB 独立验包继续保留
`fx_validation_unverified`；公司行动空响应及 `corporate_actions_coverage_unverified` 不能当作无事件证明。
本轮复合来源清单为47份，旧包的来源锚不改写。实际 OM planner/fetcher/limiter/export 的合成产物已由 OB 原样消费；
373项 OM 受影响回归、guardrails 和172项 OB 消费者测试（另12项 subtest）通过，非全仓交付或真实收益验收。

### 估值前附属资料自动供给（2026-10-31，本地增量，未部署）

每日请求 v1/v2 或长期 schedule v2/v3 的 `inputs` 可显式增加 `ancillary_capture` 引用（每日请求在顶层，
schedule 在 inputs 内），仍使用 `{path,sha256}`。原件精确格式为：

```json
{
  "schema_version": "research.market-ancillary-capture.v1",
  "lead_seconds": 60,
  "max_calls": 5,
  "max_bytes": 100000000,
  "min_free_bytes": 1
}
```

以上只作单标的测试示例，不是远端批准值或默认值。所有数值必须是正整数；无此引用时原流程不变。
该模式只接受单市场、单观察槽的每日请求。设原报价观察点为 T，自动供给窗口为 `[T-lead_seconds,T)`，
须由该日明确批准的 observation window 完整覆盖；半日市和夏令时继续由既有日历/计划 owner 决定 T。
报价仍在原60秒槽内采集，**没有新增盘口采样频率或放宽报价用途时间**。

原每日 CLI 在准备完成后等待附属窗口，复用 OM `current_exchange_rate_snapshot` 的两源解析/资格规则，
通过受控传输回调保存实际解码响应文本、源时间及原生 FX envelope；不复制解析或资格公式，不读取生产缓存，
不写新的共享缓存。公司行动调用原网关及 `underlier_history` 的 `get_rehab` 响应处理，仅查询固定分片的全部标的
（含 stock-only），不额外获取日线、日历或账户。使用原研究限流和逐请求存储检查，先持久预约再调用，不自动重试。

每市场每日最多两次 FX HTTP 请求、一次 OpenD quote preflight、每标的一次 rehab 请求，即 `3+N` 次应用请求；
SDK握手不在该数字中。`max_calls` 小于完整范围需求则拒绝，不裁剪范围。此预算独立于原链发现和报价预算，
**没有自动追加到原282次/日的授权**，上线前须核算总额并批准新固定包、网络来源和时间窗口。

原件保存至 `day-dir/market-ancillary/`，含不可变 request、两源响应文本回执、FX envelope、公司行动响应及调用结果；
manifest 复用 `research.market-ancillary.v1`。原件在首次报价归档初始化前冻结，所有分片的
`inputs/ancillary/captured/` 保存相同原字节，并参与原摘要、压缩与完整日封存。重复触发只核验复用，失败/中断不重采；
漏过窗口的资料保持 missing，不用稍后响应回补。附属源失败不阻断正常报价，摘要变化仍拒绝。

正常返回不是资格认证：FX仍可能不满足观察用途，OB保留 `fx_validation_unverified`；公司行动空响应不能证明无事件，
继续 `corporate_actions_coverage_unverified`。实际OM每日planner、FX解析、网关/限流、报价及export的合成链路已验收：
新增13项，受影响32文件共1315项回归通过，OB176项消费者测试及12项subtest通过；复合实现来源为48份。
未访问真实来源、未部署、未修改原远端任务。

`om research watchlist-day-run` 通过 `--request` 与 `--expected-request-sha256` 消费冻结每日请求，默认预览不联网、不写目录；`--confirm` 在发现安全窗口内准备一次，再复用原多片 collector。其余必填参数是 `--day-dir`、可重复 `--budget-root`、`--runtime-root`、可重复 `--prune-root`、`--opend-host/--opend-port`。

请求 v1 为 `schema_version=research.watchlist-day-request.v1`、`date`、`previous_day` 和七份 `{path,sha256}` 输入引用：`scan_input/calendar/policy/discovery_budget/observation_budget/discovery_window/observation_window`。`previous_day` 为 null 或显式 `{path,request_sha256}`，path 指上一日绝对 `prepared/` 目录；承接原 tracking，不自动搜索 latest，不读取实盘。名单是冻结版本，变更需显式新导出。

请求 v2 为 `research.watchlist-day-request.v2`，只把上述 `calendar` 引用换成 `calendar_query`。查询原件示例：

```json
{"schema_version":"research.watchlist-calendar-query.v1","markets":["HK","US"],"start":"2026-11-02","end":"2026-11-30"}
```

日期是有限查询范围示例，不是已批准运行期限。市场列表须排序去重、包含名单及延续跟踪需要的市场；必须覆盖请求 date。首次准备时复用 OM `get_trading_days_with_receipt`，每市场一次，与链发现共用网关、研究限流和 `discovery_budget.max_calls`；不额外建连接、不重试。每次物理请求在限流等待后核验安全窗口/磁盘/请求预算，预约及失败不退款，SDK readiness/握手仍按原启用单单列。

完整原响应和请求/接收时间保存在 `prepared/discovery/calls/`；查询描述保存在 `prepared/calendar_query.json`，验证后的日历保存 `prepared/calendar.json` 并绑定每日计划和导出摘要。可知时间取真实接收时间，不取请求起点或旧日期。v1 原件路径不变。日历失败/不完整时保存发现回执、停止准备；重复触发为 abandoned，不偷偷降级至旧日历或自动重放。新日期使用新请求/目录，已冻结日历及计划不覆盖。

日历维护应在固定市场、期限、范围和请求/磁盘预算内与持续采集一次批准；当前本地代码不授予真实网关或部署权限。半日市及休市复用原 planner；安全窗口与实际唤醒时刻仍须覆盖生成的槽。扩大采集未安装、未启用，合成验证不能替代真实连续日验收。

### 多日自动执行

`om research watchlist-schedule-run` 让一个 timer 执行整份已冻结的多日任务清单，不再要求每天人工启动或为每份日历单独批准。清单为 `schema_version=research.watchlist-schedule.v1` 与非空 `requests` 数组；每项仍是 `{path,sha256}`，path 相对清单目录指向上述每日请求。日期必须唯一且递增，第二日起的 `previous_day` 必须精确绑定本清单上一项的请求摘要和 `<archive-root>/<date>/prepared/`，不能静默丢失跟踪义务。

```bash
om research watchlist-schedule-run \
  --schedule <冻结的多日清单JSON> --expected-schedule-sha256 <外部摘要> \
  --archive-root <独立研究日期目录根> --budget-root <数据计量根> \
  --budget-root <安装计量根> --runtime-root <隔离的生产runtime> \
  --prune-root <隔离的清理根> --opend-host <明确主机> --opend-port <明确端口>
```

默认预览验证请求及输入摘要，不联网、不落盘；加 `--confirm` 后，仅在某份请求明确的发现/观察窗口内进入原每日 runner。每天分别冻结范围、日历、计划及原件；重复唤醒不重做发现、不重抢观察 claim。窗口之外只检查清单，不调用网关；全期到期返回 expired，不再取数，也不自动续期。缺前驱或中断准备照实失败，不推断空仓、不退回更旧 tracking。漏采和遗漏日期仍须在交付报告中保留，不以 timer 成功证明连续覆盖。

本地提供 `services/systemd/options-monitor-research-watchlist-schedule.{service,timer}.in`：分钟对齐唤醒、`Persistent=false`、`Restart=no`；只执行研究入口，并保留生产路径隔离及指定归档/SDK HOME 可写边界。最终安装单应一次批准多日范围、所有窗口、日历更新及总预算，并安排期末停用 timer。模板不自动安装/启用，也不授予延长日期、扩大名单或额外配额的权利；持续期间名单仍是明确导出的冻结版本，不自动读生产配置。

每日准备在 owner 生成计划后检查全部槽：每个槽须完整包含在一个已批准观察区间内，不能拼接相邻区间或自动扩大窗口。夏令时、半日市导致时点变化时，错误窗口明确拒绝；发现/计划原件保留，准备未完成时后续触发报告 abandoned、不自动重试。日历未知前，离线 preview 不能证明未来窗口与实际交易时段一致。

本地集成已执行 10/30 → 11/02 两天的实际合成报价与导出，覆盖美国夏令时切换及半日市；不是只验证两天准备成功。OB 用未修改导出验证当日新报价与日终原件索引，研究资格不足仍保留；未访问远端、未部署或启用，不能当作扩大范围的真实连续采集验收。

## 长期循环与压缩导出

同一 `watchlist-schedule-run` 入口接受 `research.watchlist-schedule.v2`：精确字段为 schema_version/start_date/timezone/calendar_markets/inputs/discovery_windows/observation_windows。inputs 为 scan_input、policy、discovery_budget、observation_budget 四份 `{path,sha256}` 引用，path 相对配置目录；窗口每项为 `{timezone,start_local,end_local}`，当地 HH:MM、同日不跨午夜，时区转换不能有歧义。HK/US 可使用各自时区表达。首次窗口内自动冻结当日日历及每日请求，不读实时配置；配置原件不可覆盖。

每次检查调度时区昨日与今日，覆盖亚洲午夜后的美国观察。承接最近已验证的准备原件和 tracking，缺失日期另存 gap，不把断档冒充完整连续性；准备中断不自动重做。未来日期不因三日结束而过期，所有实际调用仍受原批准窗口、磁盘和请求上限约束。长期 service/timer 模板不自动安装，启用前统一批准长期范围、预算和停止条件。

存储预算同时返回 logical_bytes（普通文件内容之和）和 allocated_bytes（文件系统上报的文件/目录分配块，按设备和 inode 去重）。used_bytes 取两者较大值，与原 max_bytes 比较：小 gzip 对象不能按几十字节计量却漏掉分配块，稀疏文件也不能逃过原逻辑大小上限。嵌套计量根不重复统计，free_bytes 仍取目标卷实际余量；这是请求前检查，不是磁盘硬配额，也不保证单次写入绝不越线。

`research archive watchlist-export` 可选 `--object-root <独立共享对象目录>`；不传时保持旧完整目录。传入时原始逻辑文件按原字节 SHA 共用确定性 gzip，输出目录仅有 `packed-export.json`，记录原路径、size、sha256 和原 watchlist-export 外部摘要。对象为 `sha256/<前两位>/<原SHA>.json.gz`，相同原件不重复写；对象冲突或已有输出拒绝覆盖。对象目录不能与源码、生产 runtime/prune、采集归档或本次输出重叠。复用 required_data_blobs 编码，不引入硬链接或数据库。

OB 使用外部原 manifest SHA 还原并验证原字节、引用和完整性，原 manifest 上限 16 MiB；跨包集合 v2 显式绑定共享 object_root。验证/导入后只清理由本次操作创建的临时还原目录，不删除采集源目录、旧包或对象。总预算必须包括安装、活跃日、源归档、压缩对象、导出清单及临时空间。生产压缩比和真实连续采集未验收。

### 完整日源归档封存与恢复

回测交付包不保存 limiter 等运行文件，不能替代完整源归档。`research archive watchlist-day-pack` 在每日观察安全窗口全部结束后，取得原 day collector 锁，核验每日准备摘要，将准备、发现/静态链、跟踪、报价、回执、请求预约、limiter、锁文件及空目录一并记录。文件复用上述共享 gzip 对象，输出新目录的 `day-pack.json`，返回必须另存的外部 SHA；不删除或修改源原件，不设置自动清理。相同日原件再次封存的清单相同，已有对象零新增。

```sh
om research archive watchlist-day-pack \
  --day-dir /private/research/days/2026-10-30 \
  --expected-request-sha256 '<每日请求SHA256>' \
  --object-root /private/research/objects --output-dir /private/research/packs/2026-10-30 \
  --runtime-root /var/lib/options-monitor --prune-root /private/production-prune

om research archive watchlist-day-restore \
  --manifest /private/research/packs/2026-10-30/day-pack.json \
  --expected-manifest-sha256 '<外部封存SHA256>' \
  --object-root /private/research/objects --output-dir /private/research/restored/2026-10-30 \
  --runtime-root /var/lib/options-monitor --prune-root /private/production-prune \
  --min-free-bytes 1073741824
```

以上为本地路径示例，不代表已批准生产布局或 1 GiB 的新预算。恢复只接受全新输出目录，逐文件验证原字节 SHA/大小、目录清单、每日准备、窗口及计划；恢复文件 0600、目录 0700。原 limiter owner 在私有 limiter 根内部产生的普通权限子文件也保留原字节，恢复时收紧权限；不改变 owner 或源文件模式。外部锚、坏对象、链接、路径越界、清单缺项及磁盘余量不足均拒绝，不修补原件。恢复失败可能留下本次新建的部分目录，不能当作成功归档，也不会自动覆盖重试。

完整恢复后可继续使用原 watchlist-export。显式 schedule v3 已另接自动封存及压缩源接续（见下节），无需改写冻结前驱路径；旧 v1/v2 不自动封存，跨机器迁移仍需显式交接请求。每日封存不包含调度根的配置/请求清单、跨日 gap 等，不能宣称单日包能恢复整个调度器。当前不执行源副本替换或对象 GC；前三交易日仍只作长期任务的首轮验收。

### 自动封存与压缩源接续

长期 schedule v3 与 v2 字段相同、均无 end_date，差别是 v3 在发现/观察窗口之外自动封存已结束且准备完整的日目录。使用原 `pack_watchlist_day`，对象位于 `<archive-root>/objects`，清单及原字节外部锚位于 `recurring/sealed/<date>/{day-pack.json,anchor.json}`。重复唤醒不重封、不重查行情；封存发布中断只可在源目录和对象仍完全一致时补完锚。仍保留源目录，不隐式删除或设置保留天数。

自动封存每一日前、以及压缩包写入后发布锚前，均复用现有磁盘计量，对发现和观察两份预算检查全部计量根。压缩保留源副本，不能把预计压缩收益抵扣实际占用；超额或最低余量不足则失败，不发布该日成功锚、不继续封存后续日。源原件和本次已写对象保留，释放足够空间后只允许核验并完成原封存，不重新取行情。仍是操作预算而非硬配额，单日封存过程中可能越线；手动 pack 的既有接口不因此新增全根预算合同。

若随后按另行批准的清理移除了冗余日目录，冻结 `previous_day.path` 仍使用原绝对 `prepared/` 路径；读取器从上述固定布局核验封存锚和原字节后承接 tracking。丢锚/坏对象拒绝，不能退回更旧状态。原 `watchlist-export --series-dir <原日目录>/archive` 可在独立输出父目录临时恢复、调用原 exporter 后移除本次临时副本；支持原可选 object-root，原 export 摘要不变。恢复空间及最小磁盘余量仍要求满足，输出不得置于该研究归档树内。重复调用已关闭日的 daily collector 返回 sealed，不补采或重建原日目录。

这是本地能力，未安装/启用 v3，未清理真实源副本。计量仍包括源目录、共享对象、清单、调度配置/请求/gap、输出和临时恢复，不能把封存成功等同于空间已经释放。

交付接线：`research archive watchlist-export` 由每日编排层的 `watchlist_day.export_watchlist_series` 判断是否需要恢复封存日，再调用 `watchlist_series` 的普通导出器；底层分片导出器不反向依赖每日编排。此调整消除研究模块循环依赖，不改变 CLI、原件内容或导出摘要合同，也不更换已部署的固定研究包。
