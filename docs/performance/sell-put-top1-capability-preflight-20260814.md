# Sell Put Top1 W0 Capability Preflight — 2026-08-14

> **历史阶段性证据**：本文只记录 2026-08-14 当时的 W0/W0R 判断，不是当前 Strategy Lab
> readiness 或实施状态。当前运行合同见
> [Strategy Lab Current Contract](../STRATEGY_LAB_DESIGN.md)，动态状态以运行时 readiness、Research
> Receipt 和 Final Receipt 为准。以下正文按原始审计语境保留，不逐段现代化。

- 执行时间：`2026-08-14T14:40:08Z` 至 `2026-08-14T14:54:18Z`（UTC）
- 最后复查：`2026-08-14T19:33:27Z`（UTC）
- 源码：`main@c1d759ae10352d2a5664739e2053bb396e698919`
- 当前费用合同 Gateflow：`feat/sell-put-top1-hk-terminal-fee-contract`，accepted plan `8b879390`
- 输入合同 SHA-256：
  - `sell-put-top1-optimization-loop-mvp-20260814.md`：`cc44035985f1f21ab13126dff31058917027183ee275b6aac6e2e9e47c105a31`
  - `sell-put-top1-modular-technical-implementation-plan-20260814.md`：`dfa65116f040b076c4dc233a447ba25e1967a11192b6e43f7106c3a1f21d16d2`
  - `sell-put-top1-modular-implementation-control-20260814.md`：`b622d24bbfc14df1eca46e2cb73b607b14a1ec2cfcdce511cf5fb6173690c0ed`
- 首轮 W0 写入范围：仅本文件；未调用 provider facade，未写 limiter、Futu log 或 runtime artifact。后续经用户单独授权的 fee remediation 源码/测试变更及复查见文末。

## 唯一结论

**W0R runtime_no_go；HK terminal fee source contract locked**

HK 终态费用公式与 domain 统一入口已为 green：`fee_calc.py` 现在是 HK 股票交收七项公式和 assignment/exercise/expired-worthless 终态费用的唯一算术来源，并区分 assignment 零行使费、exercise HK$2/张和 expired-worthless 零费用。该源码合同可作为 W1B 纯经济计算的前置，但 `lx` 当前佣金减免/平台收费套餐仍没有可审计 receipt；两个现有 consumer 不信任普通 event/position mapping 中的 `account_fee_plan`，因此正确 fail closed，不产出客户净费用。history K-Line quota 和未复权 exact-expiration close 的 project source contract 已为 green，但 live quota/close receipt 仍为 unknown；OpenD live observation、calendar 和 exact-expiration terms capacity 也仍为 unknown，所以 provider-dependent research/validation 与真实试点继续禁止。40 日语料不足单独记为 `research_corpus_warming`。

## 实际执行命令

以下命令均从仓库根目录执行。两个 Python stdin 块只使用 stdlib `json`、`pathlib`、`collections` 和 `math`，只读遍历并在 stdout 输出统计；没有落盘。

```bash
git rev-parse HEAD
git branch --show-current
git status --short --branch
shasum -a 256 docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md docs/plans/sell-put-top1-modular-implementation-control-20260814.md
wc -l AGENTS.md docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md docs/plans/sell-put-top1-modular-implementation-control-20260814.md
sed -n '1,240p' AGENTS.md
sed -n '1,1248p' docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md
sed -n '1,420p' docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md
sed -n '1,430p' docs/plans/sell-put-top1-modular-implementation-control-20260814.md

find output_shared/research/remote_archive/prod/output_runs -name scheduler_decision.json
find output_shared/research/remote_archive/prod/output_runs -name opening_candidate_snapshot.json
find output_shared/research/remote_archive/prod/output_runs -name candidate_snapshot_manifest.v1.json
jq '.summary' output_shared/research/remote_archive/prod/manifests/inventory.latest.json
jq '.runs[0].candidate_evidence | {counts,strict_replay_authority,reason_code}' output_shared/research/remote_archive/prod/manifests/inventory.latest.json
git log --diff-filter=A -1 --format='%H %cI %s' -- src/application/opening_candidate_snapshot.py
git log --diff-filter=A -1 --format='%H %cI %s' -- src/application/candidate_snapshot_manifest.py

PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json, math
from collections import Counter
from pathlib import Path
root = Path('output_shared/research/remote_archive/prod/output_runs')
rows = []
for run in root.iterdir():
    tick_path = run / 'state/tick_metrics.json'
    account_path = run / 'accounts/lx/state/account_metrics.json'
    if not tick_path.is_file() or not account_path.is_file():
        continue
    tick = json.loads(tick_path.read_text())
    account = json.loads(account_path.read_text())
    decision = tick.get('scheduler_decision') or {}
    target = decision.get('scheduled_target_market')
    if (decision.get('should_run_scan') is True and target
            and 'HK' in (tick.get('markets_to_run') or [])
            and account.get('ran_scan') is True
            and 'HK' in (account.get('markets_to_run') or [])):
        duration = int(account.get('scheduler_ms') or 0) + int(account.get('pipeline_ms') or 0)
        timeout = (tick.get('trigger_context') or {}).get('timeout_seconds')
        rows.append((target[:10], duration, timeout))
def nr(values, q):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * q) - 1)]
per_day = Counter(date for date, *_ in rows)
durations = [duration for _, duration, _ in rows]
print(len(rows), dict(sorted(per_day.items())))
print(nr(list(per_day.values()), .50), nr(list(per_day.values()), .95), max(per_day.values()))
print(nr(durations, .50), nr(durations, .95), max(durations))
print(sorted(set(timeout for *_, timeout in rows)))
PY

PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
from pathlib import Path
roots = [
    Path('output_runs'),
    Path('output_shared/research/remote_archive/prod/output_runs'),
    Path('output_shared/research/remote_archive/prod/manifests'),
    Path('output_shared/research/remote_archive/prod'),
    Path('output_shared/research/shadow_replay'),
    Path('output_accounts'),
]
for root in roots:
    files = [p for p in root.rglob('*') if p.is_file()]
    print(root, len(files), sum(p.stat().st_size for p in files))
sqlites = [p for root in (Path('output_accounts'), Path('output_shared'))
           for p in root.rglob('*')
           if p.is_file() and p.suffix in {'.sqlite', '.sqlite3'}]
print('runtime_sqlite', len(sqlites), sum(p.stat().st_size for p in sqlites))
top1 = [p for root in (Path('output_runs'), Path('output_accounts'), Path('output_shared'))
        for p in root.rglob('*') if p.is_file()
        and any(token in str(p).lower()
                for token in ('sell-put-top1', 'sell_put_top1', 'recommendation_point.sell_put'))]
print('top1_runtime_artifacts', len(top1), sum(p.stat().st_size for p in top1))
PY

rg -n 'get_history_kl_quota|history_kl_quota' src domain tests
rg -n 'expiry_contract_terms_receipt|terms_capture' src domain tests
rg -n 'OPEND_RATE_LIMIT_ENDPOINT_ALIASES|history_kline|request_history_kline|get_trading_days_with_receipt|get_option_chain|get_snapshot' src/application/opend_fetch_config.py src/infrastructure/futu_gateway.py src/application/short_vol_metrics.py
nl -ba domain/domain/portfolio_assignment_scenario.py | sed -n '152,250p;540,613p;983,1066p'
nl -ba domain/domain/fee_calc.py | sed -n '229,262p'
nl -ba tests/test_portfolio_assignment_scenario.py | sed -n '145,180p'
nl -ba src/application/scan_scheduler.py | sed -n '323,357p;499,524p'
nl -ba domain/domain/option_lifecycle.py | sed -n '48,64p'
nl -ba src/interfaces/cli/run_ops.py | sed -n '37,42p'
jq '.schedule' config.hk.json
rg -n -C 4 '127.0.0.1|11111|opend_rate_limits' config.hk.json

nc -z -w 2 127.0.0.1 11111
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_portfolio_assignment_scenario.py tests/test_futu_gateway_minimal.py tests/test_opend_batch_config.py
git diff --check -- docs/performance/sell-put-top1-capability-preflight-20260814.md
git status --short --branch
```

`nc` 在 sandbox 内和经批准的同一 sandbox 外只读复查均为 `exit=1`。未发起 Futu API 调用，因为现有 facade 会写 file-backed rate limiter/Futu log，不符合本轮唯一写入边界。focused pytest 两次结果均为 39 passed（`1.98s`、最终复核 `0.51s`）；这只证明当前行为受测试覆盖，不会把缺失 capability 变为 green。

## 证据来源与权威边界

| 来源 | 当前证据 |
|---|---|
| 当前源码 | 上述固定 HEAD；gateway、scheduler、fee domain 与测试逐行核读 |
| 本地 run | `output_runs/`：28 files / 43,967 bytes；不含正式 opening snapshot |
| 本地 prod 历史归档 | `remote_archive/prod`：500 runs，时间覆盖 2026-06-01..2026-07-31；inventory 为 499 verified / 301 replay-evidence / 395 deletion-verified |
| 候选权威 | 归档中 `opening_candidate_snapshot.json=0`、`candidate_snapshot_manifest.v1.json=0`；最新归档 run 的 candidate evidence 为 `supported=0`、`unsupported_snapshot_schema=2`、`strict_replay_authority=false` |
| 权威引入时间 | opening snapshot 源码在 `5a1f045d`（2026-08-07）引入，manifest-bound authority 在 `97a24479`（2026-08-12）引入，均晚于归档结束日 |
| Provider | 当前 `config.hk.json` 指向 `127.0.0.1:11111`；端口不可达，故没有本轮真实 receipt |
| 旧 OpenD artifact | `output_shared/state/opend_metrics.json` 只有 2026-05-25 的 US/NVDA 样本（286 snapshot codes、2 snapshot calls、1,081ms）；它不是当前 HK/lx 最大 cardinality fixture，不能证明 green |

旧 `strategy_scan_status.v1`/CSV 不是 `opening_candidate_snapshot.v1`，不能据此重建 accepted `U_rank`、point 身份、排序投影或真实 validation 最大 cardinality；其行数不得冒充当前权威样本。

## 历史规模与成熟窗口

归档中可严格识别的 HK/lx terminal scheduled run 必须同时满足：正式 scheduler target 存在、`should_run_scan=true`、tick 与账户均为 HK、账户 `ran_scan=true`。结果：

| 指标 | p50 | p95 | max | 补充 |
|---|---:|---:|---:|---|
| terminal scheduled runs / 账户交易日 | 7 | 7 | 7 | 59 runs / 9 日；日计数为 `6,7,7,7,7,4,7,7,7` |
| terminal run duration | 4,686ms | 11,071ms | 12,036ms | `scheduler_ms + pipeline_ms`；59 个 trigger 均为 600s timeout |
| 每日正式 target（当前 HK schedule） | 7 | 7 | 7 | 09:40、10:00、11:00、13:00、14:00、15:00、15:50 |
| 每 point accepted `U_rank` | unknown | unknown | unknown | 0 个权威 opening snapshot 样本；不是 0 candidates |

- 完整且成熟 40 日窗口：`0`。
- 最早可用日期：`none`；W2/W4 producer seam/corpus 尚不存在，无法给出未来精确日期。
- 状态：`research_corpus_warming`；它阻止真实 research，但不单独阻止平台代码。

## Validation cardinality 与存储基线

真实 accepted `U_rank` max 不可得，因此真实最大 cardinality 均为 unknown。只记录不用于 green 判定的结构上界：当前 7 points/day、2 arms、4 个 HK underliers，在全部新合约且无 fill 时，日末 active monitors `<=14`、observation receipts/day `<=56`、20 日 outcome jobs `<=280`、单一 expiration 的 unique `(stock_owner, expiration)` terms shards `<=4`。这些是合同算术，不是历史实测 fixture。

| 路径/对象 | files | bytes |
|---|---:|---:|
| `output_runs/` | 28 | 43,967 |
| `remote_archive/prod/output_runs/` | 65,012 | 2,958,326,364 |
| `remote_archive/prod/manifests/` | 20 | 19,864,457 |
| `remote_archive/prod/` 全部 | 69,644 | 9,732,753,600 |
| `output_shared/research/shadow_replay/` | 267 | 221,912,690 |
| `output_accounts/` | 43 | 6,559,435 |
| 运行数据根内 SQLite 数据库文件 | 6 | 3,837,952 |
| Top1 corpus/runtime artifacts/Strategy Lab DB | 0 | 0 |

## Capability matrix

| Capability | 状态 | 当前证据 |
|---|---|---|
| terminal/daily inventory | green | 59 个严格识别的 HK/lx terminal formal runs；schedule 与 artifact 一致支持 7 targets/day |
| accepted `U_rank` / 40 日 corpus | `research_corpus_warming` | 权威样本 0，窗口 0，最早日期 none；不作为平台代码的独立 no-go 项 |
| validation active observation max | **unknown** | 只有 `<=14` 的结构上界；无真实 accepted max 与 HK fixture duration |
| validation terms shard max | **unknown** | 只有每 expiration `<=4` 的结构上界；无 exact-expiration receipt/duration |
| validation outcome-job max | **unknown** | 只有 20 日 `<=280` 的结构上界；无真实 accepted/fill 历史 |
| corpus/artifact/SQLite bytes | green（基线） | 上表为当前只读字节盘点；Top1 专属对象尚不存在 |
| HK assignment/exercise/expiry 净费用 | **provider/domain green，account evidence red** | 官方公式已在 `calc_futu_hk_terminal_fee()` 版本化实现，HK stock 七项算术仅保留在 `fee_calc.py` 一处；实际 broker fee provenance 仍优先。缺 `lx` 当前 `commission_free/platform_fee/fee_plan_ref` receipt 时返回 `basis=missing, amount=None`，只保留明确标记的标准固定式审计估算，不代替客户净费用 |
| OpenD observation | **unknown** | raw snapshot/batch path 存在；live provider 不可达，旧 US/NVDA 样本不能证明 HK 最大 cardinality 300s 上界 |
| 交易日历 | **unknown** | `get_trading_days_with_receipt()` 与单测存在；无本轮 live receipt |
| 未复权 exact-expiration K_DAY | **SDK/project source green；live unknown** | `FutuGateway.get_exact_expiration_close()` 固定同日起止、`K_DAY/NONE` 和 `time_key/close`，严格校验 DataFrame shape、分页、唯一 code/date 及正有限 close；当前 QFQ consumer 未改变，且无 live receipt |
| history K-line quota | **SDK/project source green；live unknown** | gateway 严格暴露 `get_history_kl_quota(get_detail=True)` 的紧凑事实，`OpenDFetchLimits` 有独立 `history_kline` endpoint；尚无 live receipt、额度充分性或生产共存证明 |
| expiry terms chain capacity | **unknown** | generic `get_option_chain()` 存在，HK config 为 9 calls/30s、max wait 600s；无 exact-expiration terms receipt、真实 max fixture 或 duration upper bound |
| advance cadence/timeout | **unknown** | 缺真实 observation/terms max 与 duration，不能闭合 readiness 公式 |

## Cadence/timeout 推导

可确认的非容量输入：scheduler catch-up grace 为 `cron_interval 10min + 2min = 720s`；公开 producer timeout 为 `600s`；HK 最后 target 为 15:50；`due_at` 为 expiration 次日 00:00，间隔 `29,400s`。因此必须同时满足：

```text
advance_cadence_seconds + fill_observation_duration_upper_bound_seconds <= 300
advance_cadence_seconds + fill_observation_duration_upper_bound_seconds
  + terms_capture_duration_upper_bound_seconds < 29,400 - 720 - 600 = 28,080
```

若第一式恰好取 300s，terms duration 还必须 `<27,780s`。但 cadence 尚未实现，fill/terms duration 没有真实最大 cardinality 实测，所以上述只是必要条件，不是 readiness 上界证明，结论保持 unknown。

## 首轮 W0 的最小 remediation 建议

下一个最小 runtime remediation 是补 `lx` 当前费用套餐的可审计 receipt，并建立一个验证该 receipt 后才传递 `commission_free/platform_fee/fee_plan_ref` 的 intake owner。普通 event/position 字典不是该 owner，不能解锁净费用。不新建费率引擎、registry 或默认套餐；这也不会把其余 OpenD/quota/terms unknown 自动变为 green。

未创建 W1 文件、分支、SQLite、CLI、timer 或 scaffolding；未提交、推送、发布、部署或操作远端。

## 授权后的 fee remediation 复查

- 完成时间：`2026-08-14T15:17:55Z`（UTC）
- 源码 SHA：`c1d759ae10352d2a5664739e2053bb396e698919`（未提交 working-tree diff）
- 写入范围：`domain/domain/assigned_stock.py`、`domain/domain/portfolio_assignment_scenario.py`、两个对应测试文件，以及本 preflight 产物。
- 未调用 provider facade，未写 limiter、Futu log 或 runtime artifact；未修改配置、服务、通知、交易、账本或 broker 数据。

### 实际执行命令（remediation）

```bash
rg -n "def calc_futu_hk_stock_fee|def _fee_fact|def _stock_fee_fact|zero_price_lifecycle_option_leg" domain/domain tests
sed -n '130,270p' domain/domain/portfolio_assignment_scenario.py
sed -n '340,520p' domain/domain/assigned_stock.py
sed -n '900,1030p' domain/domain/assigned_stock.py
python3 <local-skill-root>/deepseek-consult/scripts/deepseek_consult.py  # 脱敏摘要经 stdin，只读顾问
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_portfolio_assignment_scenario.py tests/test_assigned_stock_projection.py
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_portfolio_assignment_application.py tests/test_portfolio_assignment_cli.py tests/test_portfolio_agent_tool.py tests/test_agent_plugin_contract.py tests/test_strategy_lab.py tests/test_shadow_replay.py
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m ruff check domain/domain/assigned_stock.py domain/domain/portfolio_assignment_scenario.py tests/test_assigned_stock_projection.py tests/test_portfolio_assignment_scenario.py
git diff --check
git status --short --branch
```

测试结果：focused `18 passed`；扩大回归 `148 passed`；Ruff 与 `git diff --check` 通过。

### 费用事实与复查状态

| 子项 | 状态 | 证据与决定 |
|---|---|---|
| HK expired-worthless terminal fee | green | [Futu 行权与交收说明](https://www.futuhk.com/en/support/topic2_513)明确 OTM 到期不发生行权；结合费用表的 exercise-related 收费触发条件，零 terminal option-leg fee 现在有显式 source/reason，不再是默认零。 |
| HK assignment customer net fee | **provider 公式 green / domain red** | [Futu HK 期权费用表](https://www.futuhk.com/en/support/topic2_335)将 assignment 纳入“行使或被行使费用”，明确被行使不收 HK$2/张，其余交收、印花税、征费、交易费、佣金和平台费也全部列明。同页 HK$5 只出现在实体股票存入/加急登记服务，不属于期权交收公式。当前 domain 的 transfer-deed missing reason 需改为套餐未绑定。 |
| HK exercise customer net fee | **provider 公式 green / domain red** | Futu 官方明确 exercise 比 assignment 多 HK$2/contract；当前 `calc_futu_hk_stock_fee()` 没有 contracts/event-kind 输入，无法产出完整 exercise fee。 |
| domain fail-closed | green | HK assignment 仅保留 stock-only estimate 供审计，正式 fee 为 `missing`；任一 terminal fee component 缺失时，不输出净收益和年化效率。该 fail-closed 行为仍正确，但 transfer-deed reason 已被后续 Futu 官方费用复查取代。 |

结论不变：**no-go**。Futu 官方公式已消除 Transfer Deed Stamp Duty 的客户公式歧义，但当前 domain 尚未实现 exercise HK$2/张，也没有 `lx` 实际佣金减免/平台收费套餐证据；未进入 W1。

## 本地历史费用证据复查

- 复查时间：`2026-08-14T15:41:27Z`（UTC）。
- 只读范围：本地 `output_accounts/`、`output_runs/`、`output_shared/` 中的 SQLite 与 JSON/JSONL 历史 artifact；未调用 provider、未操作远端、未写运行时数据。

### 复查结果

| 证据面 | 结果 | 判定 |
|---|---|---|
| 当前 ledger SQLite | `trade_events=3`、`assigned_stock_events=0`；只有 0700.HK 的手工 open/close 与 PLTR 的手工 open，三条事件均无 fee/commission/stamp/settlement key | 无 assignment/exercise 费用事实 |
| 历史 assignment/exercise artifact | 374 个文件、6,728 个重复投影对象，按事件身份去重后 16 条 assignment、0 条 exercise | 不是费用回单 |
| HKD assignment | 去重后 7 条，均为 0700.HK；`event_source_type=manual_trade_event`、`event_source_name=cli_manual_open`，直接费用字段为空，无 order/deal/trade broker identity | **red / unknown** |
| `fee_provenance` | 374 个文件去重后仅 1 条：FUTU/USD stock fee `basis=estimated`；HKD 记录为 0 | 无 HK 客户实际费用证据 |
| 实费关键字 | `basis=actual`、`broker_reported_fee`、`transfer_deed`、`stamp_duty`、`settlement_fee` 均 0 命中 | 本地历史不能证明 `lx` 当前佣金减免和平台收费套餐 |

374 个 artifact 命中是同一批 ledger/Advice 投影在多个 run 与 source snapshot 中的重复，不能按文件数视为 374 份独立券商证据。去重后的 HKD assignment 记录可证明历史生命周期投影存在，但它们没有券商费用明细或可关联的 broker identity，不能把缺失费用解释为零。

### 实际执行命令（本地历史复查）

```bash
find output_accounts output_shared output_runs -type f \( -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db' \) -print | sort
sqlite3 -readonly output_shared/state/option_positions.sqlite3 ".schema trade_events" ".schema assigned_stock_events" "SELECT 'trade_events', COUNT(*) FROM trade_events UNION ALL SELECT 'assigned_stock_events', COUNT(*) FROM assigned_stock_events;"
sqlite3 -readonly -header -column output_shared/state/option_positions.sqlite3 "SELECT event_id, json_extract(event_json,'$.account') AS account, json_extract(event_json,'$.symbol') AS symbol, json_extract(event_json,'$.currency') AS currency, json_extract(event_json,'$.fees') AS fees, json_extract(event_json,'$.fee_provenance') AS fee_provenance FROM trade_events ORDER BY trade_time_ms;" "SELECT DISTINCT key FROM trade_events, json_tree(trade_events.event_json) WHERE lower(key) LIKE '%fee%' OR lower(key) LIKE '%commission%' OR lower(key) LIKE '%stamp%' OR lower(key) LIKE '%settle%' ORDER BY key;"
rg --no-ignore -l -i --glob '*.json' --glob '*.jsonl' '"close_type"[[:space:]]*:[[:space:]]*"assignment"|"close_type"[[:space:]]*:[[:space:]]*"exercise"|"event_type"[[:space:]]*:[[:space:]]*"assignment"|"event_type"[[:space:]]*:[[:space:]]*"exercise"' output_accounts output_runs output_shared
jq -c '.. | objects | select(.close_type? == "assignment" and .currency? == "HKD")' output_shared/research/remote_archive/prod/output_runs/20260731T023025Z-181bad/accounts/lx/state/position_advice_input.v2.json
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -  # stdin: 递归读取上述 rg 命中，按账户/标的/到期日/行权价/close-event identity 去重，统计费用字段与 broker identity
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -  # stdin: 读取所有 fee_provenance 命中，按账户/标的/币种/basis/reason/source 去重
rg --no-ignore -n -i --glob '*.json' --glob '*.jsonl' '"basis"[[:space:]]*:[[:space:]]*"actual"|"broker_reported_fee"|"transfer_deed"|"stamp_duty"|"settlement_fee"' output_accounts output_runs output_shared
```

唯一结论不变：**no-go**。本地历史证据可用于核验未来的实费回执，但不能代替 `lx` 当前费用套餐绑定。未获得前停止，不猜测，不进入 W1。

## Futu 官方终态费用公式复查

- 复查时间：`2026-08-14T15:52:34Z`（UTC）。
- 权威来源：[Futu HK 官方《港股收费（股票、ETF、窩轮牛熊、期货、期权）》](https://www.futuhk.com/support/topic2_335)中的“股票期权行使或被行使费用”，以及 [Futu HK 官方《行权与交收》](https://www.futuhk.com/en/support/topic2_513)。
- 下列只是对官方表的确定性重写，未使用第三方费率。

设 `V = strike × multiplier × contracts`，`C = contracts`，则单个行使/被行使交收的官方客户费用公式为：

```text
settlement_fee = 0.0042% × V
exercise_fee   = 2 × C                 # exercise
exercise_fee   = 0                         # assignment
stamp_duty     = ceil_to_HKD(0.1% × V)
sfc_levy       = max(0.0027% × V, 0.01)
frc_levy       = 0.00015% × V
trading_fee    = max(0.00565% × V, 0.01)
commission     = 0                         # 若账户对行权/被行权正股交易免佣
commission     = max(0.03% × V, 3)     # 否则
platform_fee   = current_account_hk_stock_fee_package(V, monthly_order_index)
total_fee      = 上述项之和
```

固定式平台费为 HK$15/订单；阶梯式依当月订单序号为 HK$30/15/10/9/8/7/6/5/4/3/2/1。Futu 明确“行权/被行权交易佣金”和“平台使用费”与账户当前港股收费套餐一致，且港股免佣适用于行权/被行权产生的正股交易。

当前 `calc_futu_hk_stock_fee()` 已与官方表的“非免佣+固定式”分支一致：佣金、HK$15 平台费、交收费、向上取整印花税、交易费、SFC 和 FRC 征费均已实现。缺口只是：

1. 没有 `exercise/assignment` event kind 与 `contracts`，所以不能对 exercise 增加 HK$2/张；
2. 佣金固定视为非免佣，平台费固定视为 HK$15，没有绑定 `lx` 当前套餐；
3. 当前 working-tree reason 仍把 Transfer Deed Stamp Duty 当作缺口，与新核验的 Futu 客户收费表不一致。Futu 同页 HK$5/纸只属于“实体股票存入/加急实体股票登记”服务，不应加到期权实物交收公式。

对 `strike=450`、`multiplier=100`、`contracts=1` 的固定式非免佣样例，`V=45,000`：当前 calculator 输出 assignment 费用 HK$79.215，与上述官方分项手算一致；exercise 应再加 HK$2，为 HK$81.215。实际免佣账户应再去掉本例 HK$13.50 佣金。本地核验命令：

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python - <<'PY'
from domain.domain.fee_calc import calc_futu_hk_stock_fee
print(calc_futu_hk_stock_fee(450, shares=100, is_sell=False))
PY
```

该公式在当次复查时使 provider schedule 证据转为 **green**，当时 domain fee capability 仍为 **red**。其后的统一费用真源实施已完成这一 domain 缺口，见下节；未进入 W1。

## 统一费用真源实施复查

- 完成时间：`2026-08-14T16:34:15Z`（UTC）。
- 源码 SHA：`c1d759ae10352d2a5664739e2053bb396e698919`（未提交 working-tree diff）。
- DeepSeek 只读检查调用链与最小边界；后续 Gateflow hardening 冻结了 exact-key v1 合同、严格数值校验和 consumer trust boundary，并移除了普通 payload 对 fee plan 的非权威透传。
- 未修改配置、服务、通知、交易、账本、broker 或 runtime artifact；未调用 provider/OpenD；未创建 W1 文件。

### 实际执行命令（统一真源实施）

```bash
git diff -- domain/domain/fee_calc.py domain/domain/assigned_stock.py domain/domain/portfolio_assignment_scenario.py tests/test_fee_calc.py tests/test_assigned_stock_projection.py tests/test_portfolio_assignment_scenario.py
rg -n "transaction_amount \\* 0\\.0003|transaction_amount \\* 0\\.000042|FUTU_HK_EXERCISE_FEE_PER_CONTRACT|platform_fee.?[:=].?15\\.0" domain src scripts --glob '*.py'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fee_calc.py tests/test_assigned_stock_projection.py tests/test_portfolio_assignment_scenario.py
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_performance_assignment.py tests/test_performance_engine.py tests/test_option_positions_cli.py tests/test_assigned_stock_sale_intake.py tests/test_ledger_economics.py tests/test_close_advice_runner.py tests/test_close_advice_runner_gateway_reuse.py tests/test_candidate_engine_parity.py tests/test_candidate_engine_contract.py tests/test_candidate_engine_phase2_contract.py tests/test_portfolio_assignment_application.py tests/test_portfolio_assignment_cli.py tests/test_portfolio_agent_tool.py tests/test_agent_plugin_contract.py tests/test_strategy_lab.py tests/test_shadow_replay.py tests/test_shadow_replay_candidate_impact.py
./.venv/bin/ruff check domain/domain/fee_calc.py domain/domain/assigned_stock.py domain/domain/portfolio_assignment_scenario.py tests/test_fee_calc.py tests/test_assigned_stock_projection.py tests/test_portfolio_assignment_scenario.py
./.venv/bin/ruff format --check --diff domain/domain/fee_calc.py domain/domain/assigned_stock.py domain/domain/portfolio_assignment_scenario.py tests/test_fee_calc.py tests/test_assigned_stock_projection.py tests/test_portfolio_assignment_scenario.py
git diff --check
date -u +%Y-%m-%dT%H:%M:%SZ
git rev-parse HEAD
shasum -a 256 docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md docs/plans/sell-put-top1-modular-implementation-control-20260814.md
git status --short --branch
```

最终 Gateflow 实现复核：聚焦测试 `45 passed`；费用消费者与邻接回归 `329 passed`；全仓 `4754 passed, 10 skipped`，仅 sandbox 禁止 loopback bind 的 HTTP 测试失败，同一测试在 sandbox 外 `1 passed`。Ruff lint、dependency graph（`production_modules=577, cycles=0`）与 `git diff --check` 通过。Ruff whole-file format check 仍显示 5 个历史大文件会产生大量超出本 work unit 的纯格式改写，因此未执行 formatter。

### 实施后费用 capability

| 子项 | 状态 | 证据与决定 |
|---|---|---|
| 公式唯一真源 | green | HK stock 七项算术只在 `fee_calc.py::_standard_fixed_hk_stock_fee_components()` 一处；旧 `calc_futu_hk_stock_fee()` 与新终态入口均复用它。未增加 registry/factory/第二引擎。 |
| assignment | domain green | 统一入口使用股票交收公式，option-leg 行使费为 0；`450×100×1` 标准固定式非免佣测例为 HK$79.215。 |
| exercise | domain green | 在同一入口上增加 HK$2/contract；同一测例为 HK$81.215。 |
| expired-worthless | green | 无行权/交收，不依赖账户套餐，返回完整零费用事实。 |
| 实际 broker 费用 | green boundary | `fee_provenance.basis=actual` / `extract_actual_fees()` 在估算之前优先，统一估算入口不覆盖实际回执。 |
| `lx` 账户套餐 | **red / unknown** | 无可审计 `commission_free/platform_fee/fee_plan_ref` receipt。缺失时正式金额为 `None`，净收益/年化效率受抑制；HK$79.215 只作已命名的标准固定式审计估算。普通 event/position mapping 注入同名字段也不会解锁完整性。 |

费用源码合同已锁定，可作为 W1B 纯计算前置；唯一运行结论仍为 **W0R runtime_no_go**。账户套餐证据、history K-line quota live receipt、其余 OpenD live receipts 和 terms capacity 未闭合前，不运行 provider-dependent research/validation 或真实试点。

## W0 继续复查：真实 provider 证据入口

- 复查时间：`2026-08-14T16:46:53Z`（UTC）。
- 范围：只读检查当前源码、已安装 Futu SDK、OpenD 本机端口/进程状态和 Futu 官方 API 文档；未调用 provider、未启动服务、未查询真实账户、未写 SDK log/runtime artifact。
- 已安装 SDK：`futu-api==10.9.6908`。

### 新增证据

| 项目 | 状态 | 证据与决定 |
|---|---|---|
| 本机 OpenD 可用性 | **red / unavailable** | `127.0.0.1:11111` TCP 检查为 `exit=1`；进程与 launchd 只读检查未发现运行中的 OpenD。没有 live provider receipt。 |
| history K-line quota 查询能力 | SDK green / project red / live unknown | 已安装 SDK 提供 `OpenQuoteContext.get_history_kl_quota(get_detail=True)`，官方返回 `used_quota`、`remain_quota` 和明细；项目 gateway/facade、receipt 及 rate-limit 配置均未暴露该能力。 |
| 实际订单费用查询能力 | SDK green / project red / account unknown | 已安装 SDK 提供 `OpenSecTradeContext.order_fee_query()`，官方支持一次最多 400 个真实订单、单账户 30 秒最多 10 次，并返回总费用与费用明细；项目没有 intake/facade，且本地 HK assignment artifact 没有可关联的 broker order identity。不能据此虚构 `lx` 套餐。 |
| 未复权到期日历史线 | provider contract green / production red / live unknown | 官方 `request_history_kline` 支持 `K_DAY` 与可指定复权类型；gateway 可归一化 `NONE`，但当前唯一生产 consumer `_fetch_qfq_history_rows()` 显式使用 QFQ。没有 `NONE` exact-expiration receipt。 |
| exact-expiration option chain | provider contract green / live unknown | 官方 `get_option_chain` 的 `start`/`end` 都是到期日，并示例以相同日期获取单一到期链；当前无真实最大 fixture、耗时或分页/完整性 receipt。 |

Futu SDK 初始化会在 `$HOME/.com.futunn.FutuOpenD/Log` 创建日志目录和文件；启动 OpenD 也属于服务/runtime 状态变更。因此，当前授权边界下不能为了“试一下”启动服务或运行 SDK probe。官方文档证明接口存在，不证明当前账户、quota、数据完整性或最大 cardinality 为 green。

### 本轮实际命令

```bash
nc -z -w 2 127.0.0.1 11111
ps aux | rg '[F]utuOpenD|[O]penD'
launchctl list | rg -i 'futu|opend'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import importlib.metadata as m; print(m.version("futu-api"))'
rg -n "def get_history_kl_quota|def order_fee_query|def get_trading_days_with_receipt|def request_history_kline|def get_option_chain|AuType.QFQ|history_kline" .venv/lib/python3.12/site-packages/futu src/infrastructure/futu_gateway.py src/application/short_vol_metrics.py src/application/opend_fetch_config.py domain/domain/fee_calc.py
sed -n '1,180p' .venv/lib/python3.12/site-packages/futu/common/ft_logger.py
sed -n '1870,1915p' .venv/lib/python3.12/site-packages/futu/quote/open_quote_context.py
sed -n '805,855p' .venv/lib/python3.12/site-packages/futu/trade/open_trade_context.py
sed -n '380,420p' src/application/short_vol_metrics.py
sed -n '80,112p' src/application/opend_fetch_config.py
rg -n "get_history_kl_quota|order_fee_query|account_fee_plan|fee_plan_ref" src domain configs config.yaml config.us.json config.hk.json tests --glob '*.py' --glob '*.yaml' --glob '*.json'
git status --short --branch
```

官方依据：

- [Futu：获取历史 K 线额度使用明细](https://openapi.futunn.com/futu-api-doc/quote/get-history-kl-quota.html)
- [Futu：查询订单费用](https://openapi.futunn.com/futu-api-doc/trade/order-fee-query.html)
- [Futu：获取历史 K 线](https://openapi.futunn.com/futu-api-doc/quote/request-history-kline.html)
- [Futu：获取期权链](https://openapi.futunn.com/futu-api-doc/quote/get-option-chain.html)

### 最小下一步

不新增 gateway、registry、配置或 W1 scaffolding。先让本机 OpenD 处于可用状态，并另行明确授权一次真实账户只读 probe 及其临时 SDK 日志写入；随后只查询 quota、交易日历、`K_DAY/NONE` 到期日收盘、exact-expiration option chain，以及在存在可关联真实订单时查询费用明细，把脱敏汇总写回本文件。任一调用失败、证据不完整或没有可关联订单，仍保持 **no-go** 并停止。

唯一结论不变：**no-go**。未进入 W1。

## 授权后的本机 OpenD 启动检查

- 检查时间：`2026-08-14T16:59:26Z`（UTC）。
- 用户已授权启动本机 OpenD、执行一次真实账户只读 W0 probe，并允许临时 SDK 日志；未授权远端、交易、通知、配置修改或 W1。
- 执行 `open -a Futu_OpenD` 后，`/Applications/Futu_OpenD.app/Contents/MacOS/Futu_OpenD` 进程持续运行，但约两分钟后 `127.0.0.1:11111` 仍未监听。
- 新生成的 OpenD GUI 日志仅能证明界面和本地组件完成初始化，不能证明登录或 API gateway ready。Orca Computer Use 因缺少 macOS Accessibility 权限无法读取该窗口；没有请求扩大系统权限，也没有输入账户凭据。
- 因端口不可达，本轮没有导入 Futu SDK、没有创建临时 SDK 日志、没有发出 provider/account API 请求。OpenD 应用自身按既有行为写入了 GUI/CrashReporter 日志。

### 实际命令

```bash
open -a Futu_OpenD
nc -z -w 2 127.0.0.1 11111
lsof -nP -iTCP:11111 -sTCP:LISTEN
ps -axo pid,etime,state,command | rg '[F]utu_OpenD|[F]utuOpenD'
find "$HOME/.com.futunn.FutuOpenD" -type f -mmin -10 -exec stat -f '%Sm %z %N' -t '%Y-%m-%dT%H:%M:%S%z' {} \;
sed -n '1,220p' "$HOME/.com.futunn.FutuOpenD/Log/FTGatewayGui_2026-08-15_00-56-52.log"
jq 'keys' "$HOME/.com.futunn.FutuOpenD/UI/user.json"
jq '{user_count:(.users|length), user_value_types:(.users|map(type)|unique)}' "$HOME/.com.futunn.FutuOpenD/UI/user.json"
```

Capability 状态不变：OpenD live receipt、quota、交易日历、`K_DAY/NONE`、exact-expiration terms capacity 和可关联实际费用仍为 **unknown/red**。唯一结论仍为 **no-go**；等待操作员在已打开的 OpenD 窗口完成登录并使 `127.0.0.1:11111` 开始监听后，再继续同一个一次性只读 probe。未进入 W1。

## W0R history K-line quota 源码边界实施

- 完成时间：`2026-08-15T09:31:30Z`（UTC）。
- 范围：只补项目内只读 gateway 与独立 endpoint 配置；未启动或调用 OpenD，未查询账户，未写生产配置、limiter state、SDK log 或 runtime artifact。

### 实施后 capability

| 子项 | 状态 | 证据与决定 |
|---|---|---|
| SDK quota contract | green | 已安装/官方 v10.9 合同提供 `get_history_kl_quota(get_detail=True)`，返回已用、剩余和逐标的请求明细。 |
| project gateway | green | `FutuGateway.get_history_kl_quota()` 强制 `get_detail=True`，严格拒绝错误 ret、畸形/负数/布尔额度、缺失或重复标的、明细数量冲突及非法时间；只返回 `used_quota`、`remain_quota` 和排序后的 `code/request_time`。 |
| history endpoint config | green | `runtime.opend_rate_limits.history_kline` 由既有 config owner 解析，默认遵循官方首页请求上限 `60 calls / 30s`，`max_wait_sec=30s`；没有进入现有 candidate fetch/discovery kwargs。 |
| live quota receipt | **unknown** | 本 work unit 没有 provider 调用；当前账户剩余额度、七日明细与响应耗时仍未证明。 |
| W0R overall | **runtime_no_go** | 账户 fee-plan、calendar、live exact-expiration close、observation/terms capacity 与 live quota 仍未闭合。 |

### 本地验证

- fake-provider/config/config-validator focused tests：`58 passed`；
- history/expiration/prefetch/research 邻接回归：`114 passed`；
- Ruff：通过；
- dependency graph：`production_modules=590, cycles=0`；
- `git diff --check`：通过。

该实施只把 **project source capability** 从 red 变为 green，不把 SDK 合同当成 live account receipt，也不授权 W5 provider runner 或真实实验。下一步仍需独立 W0R work unit 和明确的 live probe 授权。

## W0R exact-expiration close 源码边界实施

- 完成时间：`2026-08-15T11:26:47Z`（UTC）。
- 范围：只补项目内只读 gateway 的精确到期日未复权收盘价边界；未启动或调用 OpenD，未查询账户，未写生产配置、limiter state、SDK log 或 runtime artifact。

### 实施后 capability

| 子项 | 状态 | 证据与决定 |
|---|---|---|
| SDK request contract | green | 已安装 SDK 支持同日起止、`K_DAY`、`AuType.NONE`、选择 `time_key/close`、`max_count` 及 continuation key。 |
| project gateway | green | `FutuGateway.get_exact_expiration_close()` 只接受带 `code/time_key/close` 列的 SDK DataFrame shape；严格拒绝错误 ret、分页、畸形/多行数据、code/date 冲突及非正或非有限 close，只返回紧凑的 `code/expiration/close`，合法空表返回 `None`。 |
| generic QFQ path | unchanged | 现有 `request_history_kline()` 与 `_fetch_qfq_history_rows()` 未修改，本 work unit 没有改变生产波动率采集语义。 |
| live close receipt | **unknown** | 本 work unit 没有 provider 调用；真实 `time_key`、到期日可用性、响应耗时和容量仍未证明。 |
| W0R overall | **runtime_no_go** | 账户 fee-plan、calendar、live exact-expiration close、observation/terms capacity 与 live quota 仍未闭合。 |

### 本地验证

- strict fake-provider focused tests：`25 passed`；
- QFQ history 与 Top1 research 邻接回归：`26 passed`；
- Ruff：通过；
- dependency graph：`production_modules=590, cycles=0`；
- `git diff --check`：通过。

该实施只把 exact-expiration close 的 **project source capability** 从 red 变为 green；`None` 仅表示 provider 成功返回合法空表，未来 W5 runner 仍负责调用时机、缺失原因、quota/rate-limit、重试、回执绑定/存储和发布。它不把源码合同当成 live evidence，也不授权真实研究或试点。
