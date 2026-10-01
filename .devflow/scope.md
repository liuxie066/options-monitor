# Portfolio Assignment Scenario Devflow scope.
# The previous completed task scope at this path remains recoverable from the worktree base commit.
goal: "全部指派后分布以富途股票与现金/MMF及 OM 短期权账本为基线，按现有开关选择性补充 PM 非富途资产"
non_goals:
  - "不执行真实指派、交易、账本/PM/飞书写入或生产服务变更"
  - "不把 PM Holdings 作为富途账户资产的替代来源"
  - "不提交、不发布或升级"
scope: "Portfolio Exposure 全部指派后分布的来源、报价、FX、质量、资金覆盖与配置预检实现"
success_signals:
  - "S1: 开关关闭不访问 PM，仅用富途仓位/现金/MMF和 OM 短期权账本"
  - "S2: 开关开启只补充同账户明确非富途的 PM 资产，零合格行有效"
  - "S3: 富途标的用同次 OpenD 报价，缺价或 FX 不回退 PM"
  - "S4: 非富途现金进入资产分布但不增加富途指派资金覆盖"
  - "S5: 只对纳入资产判断质量，保留来源、时间和不完整原因"
  - "S6: 保持现有只读入口与配置预览/确认/回读边界"
authorized_slices: [A, B, C]
slice_checkpoints:
  - "A: 富途基线、全部券商期权隔离投影、同次报价与 FX、来源文案已实现；173 个相关测试、ruff、py_compile、guardrails 与 git diff --check 通过；用户已确认"
  - "B: PM non_futu 已按第五轮设计返工：完整原始 Holdings 切片先判券商再严格转换，回传 broker 清单与数量；CNY 现金/MMF 恒等价、报价缓存/可核实市场时刻及独立 FX 证据按纳入行判质量；PM 全套 1571 项及项目门禁通过；用户已确认"
  - "C: OM 已按现有开关请求 PM non_futu 并核对完整 broker 清单、计数、逐行来源与按账户批准集合；PM 失败/旧版/新 broker 保留富途基线且 partial，预览零合格行与 apply stale 摘要、配置回读、输出/文档已完成；OM 394 个相关测试、smoke、ruff、编译、guardrails 与 diff 检查通过；用户已确认"
user_confirmation:
  - "开启时只补充 PM Holdings 中非富途来源的资产，排除 PM 的富途股票、现金和 MMF 副本，避免重复计入。"
  - "先用 devflow 设计方案，别着急开发"
  - "进入下一节（确认 Brainstorm 结果后进入 Save Design）"
  - "好的（确认 Save Design 版本 48fddb15ffebe00e833279eab9d9d1a56dfd65be379e4772cb4c7432982f30e6，进入 Improve Design）"
  - "确认（批准四路 Panel 裁决；仅修订同一设计稿，不进入开发）"
  - "优化方案，再跑 planreview（授权修订现有设计并对新快照执行第二轮只读评审，不进入开发）"
  - "impl（授权按已确认设计进入实现节点；各切片仍分别展示结果并确认）"
  - "确认（批准 Slice A 验收，进入 Slice B）"
  - "先修订，然后跑planreview（批准本轮 Improve Design 修订与第三轮只读评审；不授权继续 Slice B 代码）"
  - "得确认确实有数据源（核对 PM 上游是否有真实市场价格时刻，不把 fetched_at 当作市场时刻）"
  - "先修订，然后跑planreview（授权第四轮设计修订与只读评审；不授权继续 Slice B 代码）"
  - "先修订，然后跑planreview（授权第五轮设计修订与只读评审；不授权继续 Slice B 代码）"
  - "确认（批准第五轮 pass-with-risks 的 Improve Design 结果；按此前 impl 授权继续 Slice B 返工）"
  - "确认（批准 PM Slice B 验收，进入 OM Slice C）"
  - "确认（批准 OM Slice C 与 Impl 最终验证结果；Impl 节点完成）"
prd_doc: not-applicable
prd_doc_ref: not-applicable
design_doc: docs/PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md
design_ref: "docs/PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md sha256:210b06a8d5d65d64cc5df360e6bfaff1c849561467c0b500de4145137347567e"
implementation_workspace: <task-worktree>/options-monitor, <task-worktree>/portfolio-management
review_base: 73a8da096f5e06b1a87ecb1592e5ca9830579071
authorization_diffs: []
workflow_version: 2
mode: node
workflow_path: null
node_sequence: [Improve Design, Impl]
current_node: Impl
internal_step: complete
status: completed
next_action: "Impl 已确认完成；Review 是后续独立节点，待明确调用"
approved_scope_ref: "本会话的非富途补充要求及已确认的 Brainstorm 结论"
path_approval_ref: null
implementation_baseline: "detached HEAD 73a8da096f5e06b1a87ecb1592e5ca9830579071; 进入 Impl 时已有方案、文档、配置与应用/测试草稿的未提交改动，保留并在任务 diff 中一并核查"
inventory:
  - "Slice A 改 OM 工作区；未接触真实券商/PM/飞书"
  - "Slice A 验证: python3.12 -m pytest -p no:cacheprovider (173 passed); ruff check --no-cache (pass); py_compile (pass); guardrails_check.py (pass); git diff --check (pass)"
  - "Slice B 改 PM 隔离工作区；仅用 fixture 模拟 Feishu/报价，无真实服务调用；get_raw_holdings 不发布 Holdings 本地缓存，pm.holdings_quantity 不用于 scoped 数量质量"
  - "Slice B 验证: PM Python 3.12 pytest tests (1571 passed); 项目 ruff、compileall、OpenAPI contract --check、git diff --check 均通过；既有模拟 Feishu 测试需在隔离 worktree 创建进程锁文件，沙箱许可后全套通过"
  - "Slice C 验证: OM Python 3.12 相关和配置消费者 pytest (394 passed); tests/run_smoke.py (OK); ruff、PYTHONPYCACHEPREFIX=/tmp 的 py_compile、guardrails 文档/敏感文件/公开接口、git diff --check 均通过；PM 工作区自 Slice B 验证后未改动"
content_revision: "sha256:210b06a8d5d65d64cc5df360e6bfaff1c849561467c0b500de4145137347567e"
panel:
  design_snapshot: "sha256:48fddb15ffebe00e833279eab9d9d1a56dfd65be379e4772cb4c7432982f30e6"
  backend: "four separate native subagents"
  model: "GPT-6 family; exact variant not exposed; cross-family dispatch unavailable"
  result_count: 4
  independence: "separate reviews verified; cross-family independence unverified"
  decisions:
    accepted: "separate non-Futu option funding; preserve signed cash by broker before gross/liability; make PM failure/empty scoped quality explicit; enforce Futu completeness flags; align broker aliases; single scenario FX; label aggregate economic coverage and per-account gaps"
    needs_evidence: "OpenD FUND/MMF row shape before changing the position classifier"
    rejected: "do not make the whole distribution unavailable solely because a non-Futu short option exists"
  revision_note: "Panel reviewed the previous fixed snapshot; the revised design awaits user confirmation and was not itself panel-reviewed"
planreview_round: 5
deepreview_round: 0
in_flight: []
evidence_paths:
  - docs/PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md
  - docs/reviews/plan-review-20260930-210746.md
  - docs/reviews/plan-review-20260930-213726.md
  - docs/reviews/plan-review-20261001-000528.md
  - docs/reviews/plan-review-20261001-003526.md
  - docs/reviews/plan-review-20261001-084009.md
blocking_findings: []
planreview_result: "第五轮 pass-with-risks；broker 新值由配置内按账户批准集合和查询时完整清单约束，旧配置缺集合安全 partial；真实 broker 值与部分市场时刻语义待实际验证"
residual_risks:
  - {item: "PM scoped freshness must be proven from included rows", classification: assigned-to-later-work-unit, owner: "PM valuation owner", destination: "Slice B source and quote/FX evidence"}
  - {item: "Closed-market quote lookback may exclude long holidays", classification: assigned-to-later-work-unit, owner: "OM quote owner", destination: "Improve Design and Slice A"}
  - {item: "Domain cash pool currently mixes distribution and funding", classification: assigned-to-later-work-unit, owner: "OM scenario domain owner", destination: "Slice A"}
  - {item: "Non-Futu option terminal values lack broker-complete starting cash and stock evidence", classification: accepted-residual-risk, owner: "OM scenario domain owner", destination: "Slice A/C partial output"}
  - {item: "Real PM broker labels are not yet verified; approved-name mislabel remains possible", classification: accepted-residual-risk, owner: "PM data source owner", destination: "Authorized enablement preview and Holdings record management"}
  - {item: "New legitimate broker labels conservatively pause PM supplement", classification: accepted-residual-risk, owner: "OM configuration owner", destination: "Slice C visible partial reason and re-preview path"}
panel_b:
  design_snapshot: "sha256:d01b280c3c2835b25a5aed2b21bf7f9a080e1247c4700b3e372c6a205e794b69"
  backend: "four separate native subagents; read-only"
  result_count: 4
  independence: "separate reviews verified; cross-family independence unverified"
  proposed_changes: "raw complete Feishu account slice before broker/quantity filtering; avoid get_holdings_fresh cache write; distinguish PM record read time from broker-current quantity; require independent scoped price/FX provenance and age; clarify non-Futu positive broker recognition"
  evidence_gap: "current draft may omit zero/negative rows and claim trusted on unavailable quote/FX time; no Slice B acceptance"
  disposition: "four-panel proposals incorporated into design snapshot d2f6d32c; Slice B draft remains unaccepted pending review and rework"
