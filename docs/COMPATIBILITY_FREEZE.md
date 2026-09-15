# 兼容面冻结反馈

- **状态**：当前约束
- **更新时间**：2026-08-29

本文冻结仍在使用、但不再承接新功能的兼容入口。冻结不是立即删除：bug、数据正确性、安全、
幂等和 readback 可以修；新字段、新命令、新策略和新 schema 必须进入 canonical owner。只有退出
证据完整时，才能在独立改动中删除冻结面。

## 反馈结论

这套“只修不增”边界仅适用于下表仍在使用的兼容面。

## 冻结矩阵

| 冻结面 | 允许 | 新功能 owner | 退出证据 |
|---|---|---|---|
| `notify_symbols.py`、`preview_notification`、legacy renderer、`alert_engine.py` 用户正文投影 | 修渲染崩溃、错字、漏字段和 alert 误报；保持 compact 默认与 legacy 警告 | scheduled 用户正文进入 Daily Brief service / repository / renderer | tick 不再写 `symbols_notification.txt` / `symbols_alerts.txt`，preview 已改为 Daily Brief 投影，legacy enum 与断言无 caller |
| lifecycle / position projection / current decision / cash conversion / order fee migration 与 `ledger.api` migration re-export | 修 inventory、verify、apply、幂等、replay、readback 和旧事实对账 | 日常能力进入 `ledger.commands`、`ledger.queries`、projection；cash / fee 新语义在写入时形成 canonical provenance | 生产 inventory / pending 为零，CLI 已直连 migration owner，`ledger.api` 不再有 caller 后移除 re-export |
| ignored OI / volume flags、`OM_SECRET_BACKEND=env`、旧 credential env、`--config-path` alias、service credential migration | 保持旧调用可解析；修显式兼容路径 | 策略参数进入 Candidate Engine + YAML；secret 进入 Keychain / systemd credentials；参数使用现行 canonical 名称 | 运行诊断、CI、生产 unit、文档和 caller 均不再使用后删除；不得借兼容入口增加新 secret 或参数 |
| `python -m src.application.*` 旧入口、option-position repository wrapper、Futu combined client | 修现有参数解析或旧 backend 崩溃；wrapper 只转调 | 人工入口使用 `./om`，结构化入口使用 `./om-agent`；能力进入现有 application facade 或 capability client | caller 和测试完成迁移后删除 wrapper / `__main__`；已退休 candidate CSV 参数只保持拒绝，不得复活写出 |
| `compatibility_amount`、旧 fees / fee、旧 market 列、非 Futu `fetch_source` 名字 | 只读旧事实并保持审计可解释；冲突继续 fail closed | 新费用进入 fee provenance，新持仓字段进入当前 portfolio schema；实际行情读取保持 Futu / OpenD owner | 历史事实和兼容 reader 无引用后，按独立数据迁移与 readback 证据删除 |

## 不冻结的 canonical owner

以下生产核继续演进，但不得再建平行实现：

- Candidate Engine、Cash-Secured Put (CSP) / Covered Call (CC) / Combo Yield steps；
- Daily Brief service、repository、renderer；
- ledger commands、queries、projection；
- trade intake / lifecycle（migration 与 backfill 除外）；
- required-data 与 tick spine；
- `./om research collect ...` 与 `./om research archive ...`。

`account_config_compatibility_path` 仍是 account 子进程配置权威链的一部分，不属于冻结兼容面，也不得
因此新增第二条 account config 文件通道。

## 变更检查

改动触及上表左列时，必须先回答：

1. 这是修复现有行为，还是新增行为？
2. 新增行为的 canonical owner 是谁？
3. 若要删除，退出证据是否同时覆盖 caller、公开合同、数据和 owning tests？

冻结面上的新增行为应停止并移到 canonical owner。冻结面上的回归修复应在变更说明中写明
“只修不增”。Architecture guard 只为可静态断言的高价值边界增加；不把整张表复制成脆弱的源码文本测试。


## 无调用者代码退役（2026-09-15）

### 范围与成功标准

删除审计列出的六项：旧 short-vol 评估分支、旧配置初始化模块、两套 Python 模型 HTTP 请求实现、两个无调用者调度包装函数、四个 Bitable 写入/字段接口、SciPy 直接依赖。保留现行扫描、账户管理、模型执行、诊断、调度和 Feishu 读取/消息行为。不得扩展为重命名模块、替代算法、配置迁移或生产操作。已有 Tick 正文退役 worktree 独立保留；本批以固定基线 `395cfe3403693d173d94e154a45ee673b7c94140` 隔离实现，不合并上批未提交改动。

用户已明确要求实施，沿用本会话已选 Devflow full；研发包括四路设计评议、实现、验证和 Deepreview，不包含提交、推送、发布、升级。原 checkout 有无关改动，进入 Impl 后使用专用 worktree。

### 基线证据与删除边界

- `short_vol_assessment.py` 当前生产消费者仅 `short_vol_risk_context` 使用 `ShortVolPortfolioContext`、`sell_put_strategy_risk` 使用 `portfolio_concentration_fields`。保留这两个接口、`ShortVolMode` 及其真实依赖的数值辅助函数，算法和输出字段保持原样。删除旧配置解析、波动率/事件/压力评分与独立 option-market-value 集中度实现及仅供其使用的常量/import/helpers；保留候选引擎现行集中度 owner。
- `agent_tool_init_local.py` 只有 `tests/run_smoke.py` 调用初始化；账户增改删包装无人调用。smoke 改为既有 YAML authoring initializer/build 和账户 mutation owner，保留临时路径、市场、账户、symbol、生成配置与策略默认值等仍适用的行为断言；旧返回字段、portfolio.runtime.json 创建/复用断言随旧接口退役，跨市场用例改为一次 YAML 初始化并验证 US/HK 共享同一 authoring owner；不得新增生产配置生成方式。迁移后删除整个旧模块。
- `openai_responses.py` / `openai_chat_completions.py` 只有 URL resolver 被 runtime/assistant diagnostics 使用。保留 resolver 的模块路径、默认 URL、斜杠规范化行为；删除请求、异常、headers、response parsing 等无调用者实现，并移除 `llm_provider_registry.provider_chat_completion_payload_options` 及其 export。Pi runtime 与 provider registry 的其他能力保持原样。
- 只删除 `scan_scheduler.scheduled_scan_targets_for_date` 和 `multi_tick_scheduler.resolve_markets_to_run`，保留底层 `_scheduled_scan_targets` / `resolve_market_run` 和其他活跃调度入口。仅清理明确变成无引用的 import。
- 只删除 `feishu_bitable.bitable_create_record/update_record/delete_record/fields`；保留 records 读取、pagination、鉴权、HTTP、缓存与消息调用者，不触碰真实 Bitable。
- 从 `requirements/runtime.txt` 与对应 `constraints/runtime.txt`、根 `constraints.txt` 去掉 SciPy。没有替代库；使用新建隔离 Python 3.12 venv 安装完整 runtime/dev 依赖，不继承 system site packages，不卸载现有环境中的 SciPy；显式绑定 `OM_PYTHON=<本 worktree>/.venv/bin/python`，记录 launcher 实际选中解释器，使 CLI 子进程和父测试使用同一环境。证明 installed metadata / import discovery 不含 SciPy且 `pip check` 通过。若其他必需依赖实际要求 SciPy则报告证据，不用 `--no-deps` 绕过。

### 选择、切片与失败语义

采用直接删除加保留接口回归，避免新 abstraction、shim 或第二配置入口。三个可验收切片：

1. 保留使用中的领域/诊断计算：short-vol 裁剪、Python HTTP 裁剪和 URL 保留。验证生产 caller、集中度的 put/call/缺失资料行为、URL default/已完整路径/尾斜杠和 diagnostics facade；不改变缺失数据表达或发真实模型请求。
2. 退役无消费者入口并迁移 smoke：旧 initializer、两个 scheduler wrappers、四个 Bitable helpers。验证 YAML smoke 初始化与增改删、原调度语义和 Feishu 模拟鉴权/读取/消息测试，禁止真实写入。
3. 移除依赖：在干净无 SciPy 环境完整安装并跑全量回归与项目必要门禁；更新生成依赖图并将 AGENT_INTEGRATION / AGENT_WIKI 的旧 initializer 引用改到现行 YAML owner。只在隔离临时 fixture 和本任务依赖目录写入。

删除前全仓搜索导入、符号调用、动态字符串与 exports；发现活跃消费者时先迁移到已存在 owner，若涉及新产品行为或安全语义则暂停说明。外部未公开 Python helper 的仓库外消费者不作兼容保证，不能把仓库无引用等同于全世界无引用。

### 验证与退出

必须有新环境安装日志、pip check、SciPy 缺席证据、完整回归（平台条件跳过如实记录）、独立 smoke、Ruff、guardrails、launcher spec、Pi smoke、生成依赖图 freshness 与 diff check。Node dependencies 使用本 worktree 内真实目录以满足既有 runtime realpath 隔离校验；旧 Pi SDK 测试复用已核验隔离目录，禁止修改生产依赖或绕过路径检查。检查内容/依赖/base 不变时复用通过证据；失败必须分类并收口，不削弱有效测试。

最终 Deepreview 覆盖全部本批 diff（含删除和 untracked）；确认保留接口语义不变、smoke 使用现行配置 owner、无死链 import、无新增替代实现、净减代码。真实 provider/服务和远端验收不在范围。未决重大取舍：无；四路评议用于挑战上述删除边界和验收缺口。
