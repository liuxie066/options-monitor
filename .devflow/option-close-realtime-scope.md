goal: 券商期权成交确认后立即按成交数量和时间更新开平仓，平仓原因可后补
approved_brainstorm: 2026-09-30 用户对 Brainstorm 回答“确认”，随后对 Save Design 回答“启动”
approved_design: 2026-09-30 用户看到设计结果后回答“继续”；设计 SHA-256 见下
scope: 实时经济平仓、待定原因、后续原子更正、收益率与风险消费语义
non_goals: 历史生产数据修复、生产账本/服务写入、提交推送及发布升级
success_signals:
- 已确认零价 close 后对应 lot 数量立即减少，原成交时刻作为资本终点
- 原因待定不算胜率；现金和费用完整时仍可算收益率
- 原因补齐或重复成交后只有一次有效数量和现金效果，股票事件另凭结算证据
workspace: <task-worktree>/options-monitor（本任务独立工作区）
initial_head: 73a8da096f5e06b1a87ecb1592e5ca9830579071
initial_status: clean detached HEAD；原主工作区有其他任务的脏改动，未碰触
design_doc: docs/OPTION_CLOSE_REALTIME_DESIGN.md
design_sha256: d651bb57aeeabfc4579f953450b327b484903ded0a339026dcbfeb666cde6759
current_node: completed
status: completed
next_action: Devflow 研发流程已完成；交付、发布和生产处理另行授权
panel: 四份独立只读建议均返回并由主 agent 定点核证；跨模型尝试因账号不支持失败，补派一次成功；实际模型身份未验证
proposed_decisions:
- 采纳：复用已有 allocation/event ID helper，分别绑定 broker anchor 与结算 evidence
- 采纳：按有效 pending source/lot/张数先 void 再等量 replacement；保留期权成交时间、费用、汇率转换和订单身份，补测 actual 费用迟到
- 采纳：在直接读取与可信决策快照中同代校验有效事件的待定原因；缺失或冲突 fail closed，已平仓优先于到期观察前的 open 分支
- 采纳：全平仓 lot 仍派生待交收资金/股份不可用，独立券商证据满足后才解除；覆盖同合约再次开仓的 case 选择
- 采纳：成交已应用但原因待定的 Inbox/receipt 状态及重复 push/backfill 读回；A、B 必须合并后才视为可交付功能
- 暂不自动判定：零价普通买卖平仓缺独立原因依据时继续待定/待审，不凭价格猜测
approved_decisions: 2026-09-30 用户说“修改设计然后跑 planreview”；以上合批写入同一设计稿
planreview_attempts: 2 completed；首轮 fail，二轮 pass-with-risks
planreview_latest: docs/reviews/plan-review-20260930-224834.md（gitignored process artifact）
second_revision: 用户再次说“修改设计然后跑 planreview”；明确 ledger 单事务 owner，完整单 anchor 多 lot 可自动更正，部分结算先 review 不自动拆分，补强费用与快照合同
approved_impl: 2026-09-30 用户在二次设计与 planreview 结果展示后说“impl”
validation: 切片 A 核心零价成交 6 passed；收益率 27 passed；Inbox/回执 36 passed；决策快照与风险 102 passed；ruff --no-cache 与 git diff --check 通过。旧平仓全文件曾有 28 failed/37 passed，旧结算语义及原因更正待切片 B 更新验证；A 单独不可交付。
slice_B_validation: 平仓入口与原因更正 73 passed；相邻结算/费用/快照/风险/收益率合计 505 passed；额外相邻消费者 176 passed、质量检查 115 passed（共享夹具旧断言修正后）；Ruff 与 git diff --check 通过。以上均为隔离测试，不是生产验收。
impl_doc_sha256: db5b1701d411257f3740249150d7999ab5ac8fbd2adca7ebaa8e7c1dc290dad1
review_attempts: 2
review_latest: docs/reviews/code-review-20261001-085931.md（gitignored process artifact）
review_outcome: pass；首轮第 1 项已修复，第 2 项经真实券商观察入口证伪并撤回；2026-10-01 用户确认完成
repair_validation: 586 passed；Ruff 和 git diff --check 通过；隔离测试，未触及生产
review_doc_sha256: 25129558a8536d2c06210174c87de46b65054310834d61b2dd0241e89eda4af6（仅流程状态文案变更）
