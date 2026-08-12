# Gateflow Goal Confirmation — Candidate Compatibility CSV Retirement

- Work unit: `candidate-csv-retirement`
- Gate: `goal confirmation`
- Date: 2026-08-12
- Status: confirmed by user
- Branch: `refactor/candidate-csv-retirement`
- Base: `origin/main@ded8f882`

## User problem

候选链路已经建立版本化 JSON 快照，但 Combo Yield、CC+LP、研究归档和 Shadow Replay 等路径仍保留
候选 CSV 的写入、读取或兜底。新运行因此同时产生两套候选表达，文件数量膨胀，也让旧 CSV 有机会绕过
封存快照重新取得事实权威。用户要求完整移除这层候选兼容 CSV，而不是继续维护 JSON/CSV 双轨。

## Confirmed goal

新产生的 US/HK 运行中，候选事实只通过封存的版本化 JSON 快照和已有的 append-only JSONL 审计链路
发布与消费；候选兼容 CSV 不再生成、不再读取，也不再作为缺失 JSON 时的降级来源。

本 work unit 退役的候选 CSV 范围包括：

- Sell Put / Covered Call 的 candidates、labeled candidates、reject log 等候选兼容导出；
- Combo Yield 的 candidates、put universe、labeled/cash-filtered/underwritten universe、reject log、
  pair diagnostics、rank shadow 等候选兼容导出；
- CC+LP 的 candidate CSV；
- research/archive/shadow replay 对上述候选 CSV 的采集、读取和 fallback；
- 仅为候选 CSV 双轨存在的 `inline|separate|both` 输出模式和文件适配器。

旧的 CSV-only 历史运行不做推断式迁移，不根据 CSV 伪造 sealed snapshot。需要候选事实的自动研究或回放遇到
这类运行时，必须返回明确、可审计的 `unsupported` 分类；原始历史运行文件继续作为冷存档保留，未来仅由
独立授权的保留策略按整次运行清理。

## Evidence and ownership

- Sell Put / Covered Call 的正式候选权威是 `state/opening_candidate_snapshot.json`
  (`opening_candidate_snapshot.v1`)；`candidate_filter_trace.jsonl` 是 append-only 审计证据。
- Combo Yield 的正式候选权威是 `state/combo_yield_candidate_snapshot.json`，但当前快照只保存最终 ranked
  pairs；停止 CSV 前，快照必须补足 Funding Put 决策、pair rejection/diagnostics 和 rank shadow 中仍被消费的
  唯一证据。
- CC+LP 的正式候选权威是 `state/cc_lp_candidate_snapshot.json`；当前 candidate CSV 只是已接受 pair 的重复
  表达，除非实现期发现唯一证据缺口，否则不为退役 CSV 任意扩张 schema。
- `src/application/strategy_scan_status.py` 仍把 Combo candidate CSV 当作 canonical artifact 并参与 hash，
  必须改为校验封存快照。
- `src/application/research/` 和 `src/application/shadow_replay/` 仍存在候选 CSV glob/read/fallback，必须统一
  收敛到 sealed snapshot + trace。

## Success signals

1. 一次新的 US/HK 候选扫描不会写出上述任何候选兼容 CSV；空候选路径也不会物化空 CSV。
2. Scheduled、manual renderer、strategy status、research/archive 和 shadow replay 都不读取候选 CSV，也不存在
   JSON 缺失时静默回退 CSV 的分支。
3. Combo Yield 封存 JSON 在停写 CSV 前承接仍有业务或诊断价值的 Funding Put 决策、pair 评估和排序证据；
   快照身份、hash、scope/status 仍可严格校验。
4. 对同一组冻结输入，Sell Put、Covered Call、Combo Yield、CC+LP 的候选资格、拒绝理由、排序和数量不因
   序列化媒介退役而改变。
5. 旧 CSV-only 运行在自动 research/archive/shadow replay 中得到明确 `unsupported` 结果，不被误报为
   `no_candidate`、`data_unavailable` 或成功空结果。
6. 源码与测试中没有候选兼容 CSV 的生产依赖；针对禁止文件名/模式有确定性回归保护，防止重新引入。
7. 聚焦 analyze、相关测试、完整测试基线（资源允许时）和 `git diff --check` 通过。

## Non-goals and safety boundary

- 不移除 `required_data/parsed/*_required_data.csv`；它仍是冻结 required-data 正式载荷。
- 不移除 `close_advice.csv` 或 `symbols_summary.csv`；它们仍有独立正式契约。
- 不改变候选过滤阈值、RV、Delta、财报窗口、收益率、权利金/费用、容量、排序或通知结论。
- 不重写或删除任何历史运行，不修改生产输出、配置、secret、ledger、Feishu 或 broker 数据。
- 不把旧 CSV 转换为 sealed snapshot，不为兼容旧运行保留隐藏 fallback。
- 不统一三个策略快照为一个泛化 schema，不顺带迁移所有 CSV 或建立新的数据库/对象存储层。
- 不合并 main、不发布、不部署、不升级远端；这些均需要独立授权。

## First-principles constraint

先把仍然只存在于候选 CSV 的有效证据搬到其自然所有者的封存 JSON，再删除 CSV 生产和消费。任何一步都不能
以“文件已不再推荐”为由先丢失可解释性，也不能保留双轨来回避契约迁移。

## Blocking open questions

无。用户已确认完整退役候选兼容 CSV，并确认旧 CSV-only 历史运行采用“显式不支持、不伪造回填、原始运行
作为冷存档保留”的策略。
