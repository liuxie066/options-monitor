# 当前汇率事实 Devflow scope
# 前一已完成任务的记录可从基线提交 3e757295 回读。
goal: "统一 OM 当前汇率事实；CNY 为决策简报主要展示；外汇市场假期沿用各币种对最后有效报价"
acceptance:
  - "A1 同一正式批次共用一份封存快照，当前汇率只保留一套取数、校验、缓存和选择代码"
  - "A2 腾讯和新浪真实字段可解析，逐币种对验证来源、价格和报价时间"
  - "A3 CNY 优先并保留原币种及负余额；有外汇休市证据才按最后有效价沿用"
  - "A4 假期沿用不放宽开仓容量，历史账本汇率事实不变"
confirmed_by_user:
  - "想要 cny 是主要展示口径"
  - "假期汇率失效时按最后有效汇率计算"
  - "确认，另外要求只保留一套代码"
  - "进入 save design"
  - "确认（批准 Save Design 版本 6f1c8163，进入 Improve Design）"
  - "修订后运行planreview（确认四份意见的合批修订与 Planreview）"
  - "impl（确认 Improve Design 版本 48baca1f，进入 Impl）"
  - "确认（批准切片 1，进入切片 2）"
  - "确认（批准切片 2，进入切片 3）"
  - "确认（批准切片 3 的 Impl 结果，进入 Review）"
  - "修复问题，再跑一遍deepreview（授权按 Review 发现修复、验证并重审）"
  - "确认（批准修复后的 Review 结果，Devflow 完成）"
non_goals: "不修成交归属，不修改生产账本、配置或服务；不提交、发布或升级"
design_doc: docs/EXCHANGE_RATE_FACT_DESIGN.md
design_sha256: 48baca1fd3368b79b45bd2b80f577653eeca1ffdcbe21c145fb9f068ca8b3d0a
workspace: "Codex managed worktree fx-unified-design/options-monitor"
initial_base: 3e757295e35b433ed2693ac4c301dc73fb2d0144
current_node: Review
status: completed
next_action: "研发流程已完成；提交、PR、发布与运行环境升级分别等待独立授权"
panel:
  design_sha256: 6f1c8163f5bb014a10d77dd41ca458b2dc6b738af4cfcbc510d3e03cc8c0dc44
  result_count: 4
  agents: [fx_design_review_1, fx_design_review_2, fx_design_review_3_local, fx_design_review_4]
  independence: "四个独立原生子代理审阅同一快照；继承模型的精确型号未暴露；跨模型入口不支持当前账户"
  cross_family_attempt: "deepseek-v4-pro 返回 model unsupported，未计入四份结果"
  proposed_changes: "核实市场休市日和互斥汇率状态；run 重试与时效绑定；逐币种历史证据与唯一当前换算；简报可靠性/负币种/证据穿透；指派情景估值和资金能力隔离"
planreview_attempts: 3
planreview_result: pass-with-risks
planreview_artifact: docs/reviews/plan-review-20261002-124243.md
slice_1:
  result: "逐对真实报文解析、来源择新、2026 FX 休市状态、容量/展示用途和并发缓存合并；旧接口返回容量安全视图"
  checks: "129 个相关 pytest 通过；ruff、doc guardrails、git diff --check 通过"
  pending: "正式 tick 共用快照和资金/简报展示属于切片 2/3，尚未实现"
slice_2:
  result: "正式 tick 在 worker 前封存一次带 hash 的当前 FX 快照；账户/期权 prepared context 与扫描按同一 hash 读取，晚到消费重判容量资格；直接扫描与独立资金查询复用请求内报价；逐对原报价进入历史候选，Wheel 跨币种容量保持新鲜门槛"
  checks: "209 个相关 pytest 通过；ruff、doc guardrails、git diff --check 通过；另 8 个无关读模型用例因 worktree tests 目录权限在建临时目录时失败，未运行到业务断言"
  pending: "决策简报 CNY 优先、假期沿用证据和情景估值/资金能力隔离在切片 3；09:40 生产输入在隔离 worktree 中不可得，尚未只读重放"
slice_3:
  result: "简报 CNY 优先且保留原币种负余额；逐对假期沿用证据穿透 normalize/render；缺汇率或来源不可靠时完整 CNY 值不可用；指派情景估值可沿用假期价而资金覆盖继续按容量资格 fail closed；通知 PRD 同步"
  checks: "329 个核心相关 pytest 与 158 个扩展回归 pytest 通过；全改动 Python Ruff、doc guardrails、git diff --check 通过"
  pending: "09:40 原始生产输入不在隔离 worktree，未做只读实盘回放；切片 2 的 8 个读模型用例因临时目录权限未到业务断言，未据此宣称通过"
in_flight: []

review_attempts: 3
review_result: pass
review_artifact: docs/reviews/code-review-20261002-163654.md
review_history: "首次 Devflow Review 与用户显式独立 DeepReview 均确认只读归属情景写缓存；本次修复后重审无未关闭实质问题"
repair_1: "portfolio_assignment_scenario 显式 write_cache=False；新增有/无缓存两种入口级无写入回归"
repair_checks: "33 个相关 pytest 通过；两份改动 Python Ruff、git diff --check 通过"
review_residual: "09:40 原始批次未回放；切片 2 的 8 个读模型用例仍未到业务断言"
