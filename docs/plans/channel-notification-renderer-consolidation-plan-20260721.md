# P1 通知 Renderer 收敛实施计划

> 日期：2026-07-21
> 状态：Revised after PlanReview
> 独立 work unit：`channel-notification-renderer-consolidation`
> 实施基线：`feat/channel-aware-notification-rendering@cd3d6c3d38d7250017d152822d664950d42a578b`
> P0 依赖：`docs/gateflow/channel-aware-notification-rendering-mobile-flat-fix-20260721-175409.md`
> 当前 P0 Draft PR：`#108`；P1 不修改该 PR
> 第一轮评审：`docs/reviews/plan-review-20260721-185232.md`；本版闭环其 4 个 findings

## 1. 目标、非目标与成功信号

### 1.1 目标

在不改变 scheduled notification 的 transport、发送确认、业务事实和 provider 合同的前提下，收敛当前重复的通知 Markdown renderer：

1. `Daily Decision Brief` 成为 **scheduled automatic ordinary notification** 的唯一主 renderer；scheduled 普通消息不再通过 Compact/Legacy 路由选择。
2. `trigger_kind != scheduled` 的 manual/force 扫描继续生成既有 run artifacts，但本 P1 明确禁止自动发送普通 Tick 通知；未来手工发送完整报告必须使用独立、需确认的 operator command。
3. `Compact Tick` 退出发送权威，进入显式兼容期，仅保留只读预览、对照测试、兼容 artifact 和代码/版本回滚用途。
4. `Legacy Tick` 立即退出普通发送链并进入废弃态；经过一个有证据的兼容发布后物理删除。
5. 无候选不再由独立 heartbeat renderer 生成；它是 scheduled Daily Brief 用户视图中的一种状态。
6. OpenD 故障/恢复与多账户投递失败共用一个窄 `System Notice` shell。
7. 成交回执与持仓维护回执共用一个窄 `Receipt` shell。
8. P0 的移动端扁平 Markdown、飞书 `post + 单 md node`、微信 Markdown identity、飞书容量预检及受控 text 回滚合同保持不变。

### 1.2 非目标

本 work unit 不做：

- 不实施 `docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md` 中的新 scheduler、固定报告/半点候选、successful current、delivery envelope、alerted state 或查询语义。
- manual/force 的“只扫描、不自动发普通通知”只是本 P1 的 trigger safety boundary；本 P1 不新增 current/revision、不实现手工发送命令，也不提前实现上位计划的 delivery state。
- 不改变当前 scheduled Daily Brief 的 `full / delta / none` lifecycle；“无候选成为状态”不等于本 P1 改为每个计划点都强制发送。
- 不新增第二套扫描器、候选过滤、排名、Close Advice、资金风控或 broker fetch。
- 不重写 provider adapter、retry、delivery confirmation、receipt 去重或 OpenD rate limit。
- 不新增 renderer registry、模板 DSL、继承体系、通用消息 DTO、provider-specific 业务 renderer 或运行时 renderer 开关。
- 不自动修改生产 runtime config，不发送真实通知，不 merge、release、远端升级或执行 Legacy 删除 gate。
- 不把 `close_advice.render_style=legacy` 等独立报告 renderer 仅因同名而一并删除；只有真实属于 Legacy Tick 链的代码才在本计划删除。

### 1.3 成功信号

1. `run_tick_notification_flow()` 的 scheduled 分支不再用 `notifications.render_style` / `notifications.daily_brief.enabled` 选择 renderer，不再调用 `prepare_multi_account_notification()`，也不再 import `build_account_message()` / `build_account_message_compact()`。
2. `trigger_kind != scheduled` 在 Daily Brief preparation 和 ordinary provider dispatch 之前结束普通通知流程；manual/force 仍完成扫描与 run artifacts，但不会生成普通 delivery identity、revision/current/delivered pointer 或 provider attempt；tick idempotency identity 包含 `trigger_kind`，同一分钟的 manual/force 不得吞掉 scheduled run。
3. scheduled Tick 普通消息只由：

   ```text
   structured run artifacts
     -> assemble_daily_decision_briefs()
     -> prepare_daily_decision_brief()
     -> render_daily_brief_lifecycle()
     -> existing delivery adapter
   ```

   生成；`AccountResult.notification_text` 为空、Compact 内容或恶意旧 Markdown 都不得改变最终 scheduled 消息。
4. 旧配置 key 有明确的两阶段迁移：Phase A/B 接受已知旧值、发稳定 deprecation warning、但绝不拥有 renderer 选择权；scheduled validation cache 在 Phase A 和 Phase C 分别升级 validator version，确保 unchanged config 也重新校验；配置清理和 version-skew matrix 通过后，Phase C 才 fail-closed。
5. 无候选、blocked、degraded、full、delta、none 均由 scheduled Daily Brief renderer 的同一用户投影处理；scheduled 路径没有 standalone no-candidate heartbeat。
6. Compact Tick 没有生产自动 fallback、没有配置选择权、没有第二个 scheduled authority；Legacy Tick 有可观测废弃提示和明确删除门槛。
7. `runtime_status` 等公共 read surfaces 对旧通知 bundle 返回 machine-readable `artifact_kind=compatibility_notification_bundle`、`primary_renderer=compact`、`authority=compatibility_only`、`delivery_evidence=false`；不得把可能追加的拒绝摘要/Close Advice 误标为纯 Compact，也不得再把 bundle 缺失视为 canonical notification 健康失败。
8. OpenD、投递失败、成交、维护四类 caller 仍拥有各自业务事实和发送状态机，但外壳分别只剩一个 owner。
9. 所有受影响消息继续通过 `assert_mobile_flat_markdown()`，并证明 Feishu/WeChat 收到的 canonical Markdown identity 未改变。
10. multi-market scheduled 请求不再被标记为成功跳过：shared/account last-run、tick metrics、run-end 和 idempotency record 都使用 `daily_brief_multi_market_delivery_unsupported` 失败语义；terminal tick record 写入 `ok=false,status=unsupported_failed,error_code=...`，CLI 返回非零且不调用 Daily Brief repository/provider。

## 2. 当前事实与根因

### 2.1 当前 scheduled Tick 有两个 renderer 权威

当前主链：

```text
pipeline_runtime.py
  -> notifications.render_style (default compact)
  -> notify_symbols.build_notification()
  -> symbols_notification.txt
  -> account_run.py / AccountResult.notification_text
  -> tick_notification_flow.py
       if notifications.daily_brief.enabled is true:
           assemble structured Daily Brief
           render_daily_brief_lifecycle()
       else:
           if render_style == compact:
               build_account_message_compact()
           else:
               build_account_message()   # 任意非 compact 值都会落入 Legacy
```

问题：

- `notifications.daily_brief.enabled=false` 仍是 committed default，P1 若只改 renderer 文件不会切断旧 scheduled 路由。
- `notifications.render_style` 当前没有严格 enum validation；拼写错误或未知值会静默进入 Legacy。
- `symbols_notification.txt` 和 `AccountResult.notification_text` 仍会让旧 Markdown 看起来像通知权威，即使 Daily Brief 已启用。

### 2.2 当前无候选有独立 scheduled heartbeat

```text
prepare_per_account_messages()
  -> build_no_candidate_account_messages()
  -> build_no_candidate_notification_text(include_account_header=True)
```

P0 已把 `include_account_header=False` 修成 body fragment，但 standalone heartbeat 仍是旧 scheduled 路径的一部分。Daily Brief 自身已经能在 `candidates=[]` 时渲染：

```text
## 候选
当前没有通过筛选的候选。
```

因此 P1 不应再创建第二个 no-candidate renderer。

### 2.3 当前 System Notice 重复外壳

| 场景 | 当前 owner | 当前标题 |
|---|---|---|
| OpenD failure | `src/application/multi_tick/opend_guard.py` | `OM · 系统告警 · OpenD` |
| OpenD recovery | `src/application/multi_tick/opend_guard.py` | `OM · 系统通知 · OpenD` |
| delivery failure summary | `src/application/scheduled_notification.py` | `OM · 系统告警 · 通知投递` |

三者都手写 H1、状态和扁平字段，但 rate limit、recovery gate、send retry 和 failure aggregation 必须继续由 caller 拥有。

### 2.4 当前 Receipt 重复外壳

| 场景 | 当前 owner | 当前标题 |
|---|---|---|
| trade intake | `src/application/trades/receipt.py` | `OM · 成交回执 · <account>` |
| position maintenance | `src/application/positions/maintenance_receipt.py` | `OM · 持仓维护 · <account>` |

两者共享 H1、状态、字段和 section 排版，但成交的 assigned-stock 确认、Combo Yield relation-pending、ledger/projection diagnostics，以及维护的 idempotency、partial/noop/dry-run 都不能进入通用 shell。

### 2.5 多市场静默跳过是切换 blocker

当前 `_prepare_daily_brief_notification()` 在 `len(markets) != 1` 时清空消息；上层随后记录 `daily_brief_multi_market_delivery_skipped` 并把 tick 完成为 skipped/success。P1 一旦移除旧 renderer fallback，这会把多市场请求变成无告警的通知丢失。

本 P1 不设计多市场聚合或一账户多消息状态机；先把该场景改为 fail-closed。后续 broader notification experience work unit 再实现 `account + market` 独立 delivery。

## 3. 目标所有权

### 3.1 Decision Brief

**唯一 scheduled renderer owner**：`src/application/daily_decision_brief_renderer.py`

职责：

- 输入 canonical structured brief/lifecycle；
- 渲染 full、delta、blocked、degraded、planning、no-candidate 和 current/query user view；
- 继续执行现有 item/character bounds；
- 不解析 `symbols_notification.txt` 或 Compact/Legacy Markdown。

**协调 owner**：`src/application/tick_notification_flow.py`

职责：

- 仅对 `trigger_kind == scheduled` 的自动普通通知进入 Daily Brief preparation；
- `trigger_kind != scheduled` 记录 `non_scheduled_ordinary_notification_disabled`，完成 scan run，但不准备/发送普通通知；
- scheduled 分支保留 existing delivery、confirmation、audit、DND、no-send 和 finalization；
- single-market 前提不满足时写 terminal failed idempotency record 并返回非零；
- 在既有 `tick_metrics.daily_brief` 中记录 `renderer="daily_brief"`，不新增 telemetry framework。

**兼容代码**：

- `src/application/notify_symbols.py`
- `src/application/multi_tick/notify_format.py::build_account_message_compact()`

仅可生成 Compact compatibility artifact/preview；不得出现在 scheduled 普通发送路由选择中。

**Non-scheduled 合同**：

```text
manual / force scan
  -> existing account pipeline + run artifacts
  -> ordinary notification disabled audit
  -> successful tick finalization
  -> no Daily Brief prepare/revision/delivery/provider
```

System Notice、恢复通知和业务 Receipt 继续按各自独立合同运行，不受此普通通知 guard 影响。

### 3.2 System Notice

新增一个窄 application helper：

```text
src/application/notification_shells.py
```

公开函数只允许：

```python
render_system_notice(
    *,
    component: str,
    status: str,
    fields: Sequence[tuple[str, object]] = (),
    sections: Sequence[tuple[str, Sequence[object]]] = (),
) -> str
```

目标格式：

```text
# OM · 系统通知 · <component>

状态｜<status>
<field>｜<flat value>

## <section>
<flat row>
```

约束：

- shell 只负责 H1、字段分隔、空 section 省略和单行 flatten；
- OpenD detail 的 1200 字符限制、错误码、rate limit、recovery gate 继续由 `opend_guard.py` 决定；
- delivery failure 的账户聚合、attempt/message/provider code 继续由 `scheduled_notification.py` 决定；
- shell 不发送、不重试、不持久化、不分类 severity。

### 3.3 Receipt

同一模块公开第二个窄函数：

```python
render_receipt(
    *,
    account: str,
    receipt_type: str,
    status: str,
    fields: Sequence[tuple[str, object]] = (),
    sections: Sequence[tuple[str, Sequence[object]]] = (),
) -> str
```

目标格式：

```text
# OM · 回执 · <account>

类型｜成交
状态｜✅ 已完成
...
```

或：

```text
# OM · 回执 · <account>

类型｜持仓维护
状态｜⚠️ 部分完成
...
```

约束：

- trade/maintenance caller 继续决定状态文字、字段顺序、section 内容和业务警告；
- shell 不知道 deal、lot、Combo Yield、ledger、projection、auto-close、dry-run 或 dedupe；
- 不创建 `ReceiptDTO`、基类或 renderer registry；两个 public helper 最多共享一个 private flat-message helper。

### 3.4 Transport

保持不变：

```text
canonical Markdown
  -> Feishu adapter: post / exactly one md node / 28 KiB preflight
  -> WeChat adapter: original Markdown identity
```

禁止：

- renderer 根据 provider 分支；
- Compact/Legacy 作为 Post 失败后的自动 fallback；
- timeout、ambiguous 或 confirmed send 后补发另一 renderer；
- shell 读取 Feishu byte budget。

## 4. Compact / Legacy / Config 兼容与废弃状态机

| 阶段 | Scheduled Daily Brief | Manual/force ordinary | Compact Tick | Legacy Tick | 旧 config keys | 进入条件 | 退出条件 |
|---|---|---|---|---|---|---|---|
| Baseline | config-gated | 当前可产生普通通知 | 默认 scheduled | 非 compact fallback | 具有选择权 | P0 `cd3d6c3d` | P1 Slice 1 完成 |
| Phase A：canonical cutover | 唯一 scheduled sender renderer | 禁止自动普通通知 | `compat-active`：只读 preview、对照测试、artifact、版本回滚 | `deprecated`：不在 scheduled route；显式调用 warning | 接受已知值、明确 warning、无选择权 | focused + broad tests pass | 发布至少一个兼容版本 |
| Phase B：observation/migration | 保持唯一 | 保持禁发 | 公共 read surface 明确 compatibility metadata | 仅保留废弃入口 | 清理 committed/generated/production candidates | 兼容版本上线 | version-skew、配置清理、上位计划修订全部满足 |
| Phase C：strict cleanup | 保持唯一 | 保持禁发 | 继续作为唯一兼容 renderer，未来删除另立决策 | 物理删除；legacy 请求 fail-closed | 任意出现均 migration error | CEO/owner 确认 exit evidence | P1 final cleanup pass |

### 4.1 Compact compatibility 的精确定义

允许：

- `build_notification(..., render_style="compact")` 生成 compatibility body；
- `build_account_message_compact()` 生成完整 Compact Tick 供 deterministic comparison；
- `preview_notification` 的显式 Compact read-only preview；
- scheduled pipeline 在 Phase A/B 继续写 Compact `symbols_notification.txt`，但该 artifact 只供对照、旧 reader 兼容和版本回滚证据使用；
- `symbols_notification.txt` 可能在 Compact body 后追加 rejection summary / Close Advice，因此 public read surface 暂时保留 legacy `notification` 字段时，该 alias payload 必须带 `artifact_kind=compatibility_notification_bundle`、`primary_renderer=compact`、`authority=compatibility_only`、`delivery_evidence=false`、`deprecated_field=true`，不能宣称整份文件由单一 Compact renderer 生成。

禁止：

- `notifications.render_style=compact` 重新控制 scheduled sender；
- Daily Brief 返回空、报错或 Post 发送失败时自动调用 Compact；
- 通过隐藏 feature flag、环境变量或 unknown style 恢复 Compact；
- 用 Compact artifact 反推 Daily Brief facts、候选、delivery key 或实际 sent message；
- Compact artifact 缺失让 `runtime_status.summary.ok=false`，或产生 canonical notification missing warning。

### 4.2 Legacy deprecated 的精确定义

Phase A/B：

- `build_account_message()` 和 `build_notification(render_style="legacy")` 不得被 scheduled runtime import/call；
- direct Python legacy 调用发 `DeprecationWarning`；
- `preview_notification(render_style="legacy")` 仅在 read-only compatibility surface 暂时接受，并返回显式 warning/metadata；
- unknown render style 直接报错，不能再 `else -> legacy`；
- docs 写明移除版本门槛和 Compact 是唯一 renderer rollback artifact。

Phase C：

- 删除 `build_account_message()`；
- 删除 `notify_symbols.py` 的 legacy/plain 分支及只为其存在的 tests，包括 `tests/test_close_advice_runner.py` 中仅验证 Legacy Tick 包装的桥接 case；
- `preview_notification(render_style="legacy")` 返回 actionable unsupported/deprecated error；
- 保留与 Legacy Tick 无关的 independent close-advice legacy report，除非当时的 import/call graph 证明其只服务 Legacy Tick。

### 4.3 旧 config key 的 version-skew 状态机

Phase A/B canonical config 不再发布旧 key，但 validator 暂时接受：

- `notifications.daily_brief.enabled=true|false`：必须是 boolean；发 `[CONFIG_WARN] NOTIFICATIONS_DAILY_BRIEF_ENABLED_DEPRECATED`；值不参与 scheduled renderer 或 manual/force send 决策。
- `notifications.render_style=compact|legacy`：必须是已知 string；发 `[CONFIG_WARN] NOTIFICATIONS_RENDER_STYLE_DEPRECATED`；值只可用于显式 compatibility preview，不参与 runtime sender。
- unknown `render_style`、错误类型和嵌套结构继续 fail-closed。
- `config_loader.py` 的 scheduled validation cache 必须同时比较 config hash 与 `validator_version`：Phase A bump 到 `notification-renderer-v2`，Phase C bump 到 `notification-renderer-v3`；不能让 unchanged old config 复用旧 `v1` cache 绕过 warning 或 strict rejection。

兼容矩阵：

| Code | Config | Expected |
|---|---|---|
| old | old keys | baseline；旧 key 仍控制 Compact/Daily Brief，仅用于升级前事实 |
| Phase A/B new | old keys | 启动成功；稳定 warning；scheduled 始终 Daily Brief；manual/force 不自动发普通通知 |
| Phase A/B new | clean | canonical 成功，无 deprecation warning |
| old | clean | 缺少 `enabled` 时回到 Compact；只允许在停止 scheduler 后做整版本 rollback，禁止 mixed fleet |
| Phase C new | old keys | 启动 fail-closed，给出删除 key 的 migration error |
| Phase C new | clean | canonical 成功 |

Rollout 顺序固定为：

1. 部署 Phase A compatibility code，**不先修改生产 config**；
2. 证明旧 key 仅产生 warning、不影响 scheduled renderer；
3. 分别获批清理 committed/generated/production candidate config；
4. 证明没有 old/new mixed fleet，并通过完整 version-skew tests；
5. 在 `option-notification-experience` 实施前修订其 `daily_brief.enabled` 默认/rollback 合同并重新 PlanReview；
6. 完成 observation 后才允许 Phase C strict rejection。

### 4.4 Legacy/strict-config 物理删除 exit gate

必须全部满足：

1. P0 已合并，P1 Phase A/B 已作为至少一个独立 release 部署；
2. 所有 committed defaults/examples、generated configs 和生产候选配置均不含 `notifications.daily_brief.enabled`、`notifications.render_style`；
3. read-only `config_validate` / `runtime_status` 证据显示启用账户配置可解析，且 scheduled audit 的 `tick_metrics.daily_brief.renderer` 为 `daily_brief`；
4. 每个启用 account + market 至少有一轮 single-market scheduled run 生成 Daily Brief prepare evidence，且没有 Compact/Legacy delivery attempt；
5. public read surfaces 对 Compact 均返回 compatibility-only metadata，Compact 缺失不再触发 canonical notification health failure；`runtime_status` 与 `analysis.runtime_tick_status` 的 canonical compatibility names 已上线，旧 aliases 的已知消费者已盘点；
6. version-skew matrix 全部通过，Phase A/Phase C validator cache-version bump 均证明 unchanged config 会重新校验，生产无 mixed old/new nodes；
7. `option-notification-experience` 的 config/rollback 文本已修订并重新 PlanReview，不再依赖 `daily_brief.enabled=false`；
8. repo import/call graph 证明 Legacy Tick 只剩 deprecation surface 和 tests；
9. Compact full-message compatibility fixture 仍通过，rollback owner 明确确认 Compact 足够承担停止 scheduler 后的整版本回滚；
10. 没有 unresolved high/critical review finding；
11. CEO/owner 明确批准 final cleanup。真实 canary/send、release、部署仍需各自独立授权。

任一条件不满足时，旧 config keys 继续 accepted-with-warning，Legacy 保持 deprecated，不允许“为了清理代码”提前进入 Phase C。

## 5. Config 与 public API 迁移

### 5.1 Runtime config

P1 canonical contract：

```json
{
  "notifications": {
    "daily_brief": {
      "max_actions_per_priority": 5,
      "max_candidates_per_strategy": 3,
      "max_rejection_reasons": 5
    }
  }
}
```

Phase A/B：

1. 从 `src/application/config_defaults.py`、`configs/system.json` 和 examples 移除 `notifications.daily_brief.enabled`；Daily Brief 对 scheduled 普通通知不再是 feature flag。
2. `notifications.render_style` 不再由 defaults/examples 发布，runtime sender 不读取其值。
3. `config_validator.py` 复用现有 `warn()`，`config_loader.py` 把 `validator_version` 纳入 scheduled cache hit 条件：
   - 已知 `daily_brief.enabled` boolean -> stable deprecation warning；
   - 已知 `render_style=compact|legacy` -> stable deprecation warning；
   - unknown style / 错误类型 -> fail-closed；
   - Phase A 使用 `notification-renderer-v2`，即使 config hash 未变也必须相对旧 `v1` cache 重新校验并输出 warning。
4. warning 必须明确“值已忽略、删除 key”；不能把 `enabled=false` 解释成 Compact，也不能静默接受。
5. 生产 `config.yaml` / generated runtime config 的真实修改属于 rollout gate，不能在 implementation slice 未经授权完成。

Phase C：

- 两个旧 key 任意出现均 fail-closed，并给出删除 key 的 migration error；
- scheduled validator cache bump 到 `notification-renderer-v3`，确保 Phase A/B 已缓存的旧 config 在 Phase C 第一次运行时重新校验；
- strict rejection 与 Legacy cleanup 同属 hard-pause 后的单独 cleanup PR/commit，不进入 Phase A compatibility release。

### 5.2 `preview_notification`

Phase A/B：

- 将实现已接受但 schema 未声明的 `render_style` 变成显式 read-only input；只允许 `compact` / `legacy`；默认 `compact`。
- output 增加实际 `render_style`、`authority=compatibility_only`、`delivery_evidence=false`；warnings 中说明 Compact compatibility-only、Legacy deprecated。
- 不让该 tool 发送消息、修改 config 或承诺与 Daily Brief 等价。

Phase C：

- input 只允许 `compact`；legacy 明确报错。

### 5.3 `symbols_notification.txt` 与 public read surfaces

Phase A/B 保留路径，避免同时打破 research/materialization 和旧 artifact readers，但语义固定为：

> Compatibility notification bundle：以 Compact Tick 为主，可能追加 rejection summary / Close Advice；不是 scheduled delivery authority，也不是 Daily Brief source/sent evidence。

所有公共 reader 必须在本 P1 盘点并改造，至少包括 `runtime_status_impl.py` 的 shared/account/latest-run/account-summary fields，以及 `analysis.py` 的 `runtime_tick_status` materialization/diagnostics：

```json
{
  "artifact_kind": "compatibility_notification_bundle",
  "primary_renderer": "compact",
  "may_include": ["candidate_reject_summary", "close_advice"],
  "authority": "compatibility_only",
  "delivery_evidence": false,
  "text": "..."
}
```

合同：

- 新 canonical field 使用 `compatibility_notification`；Phase A/B 的 legacy `notification` alias 仅作为 compatibility window 的临时输出层，至少保留一个 compatibility release 并在 Phase C 删除，必须返回同一 metadata payload 并带 `deprecated_field=true`；
- `runtime_status_impl.py` 的 internal summary/read helpers 必须先读 `compatibility_notification`，仅为读取 pre-P1/mixed-version payload 才 fallback 到 legacy `notification`；fallback 不得制造 delivery evidence；
- `account_summary` 的 canonical derived fields 改为 `compatibility_notification_exists`、`compatibility_notification_mtime_utc`、`accounts_with_compatibility_notification`；旧 `notification_exists`、`notification_mtime_utc`、`accounts_with_notification` 仅在 Phase A/B 保留 deprecated aliases；
- `analysis.py` 的 `runtime_tick_status` 新 canonical column 为 `compatibility_notification_exists`；Phase A/B 可保留 deprecated `notification_exists` query alias，并在 `VIEW_SPECS["runtime_tick_status"].deprecated_fields` 映射到 canonical column；`_runtime_diagnostic_records()` 不得再用任一 compatibility artifact existence 字段判断实际通知缺失，delivery 诊断只读 `notification_diagnosis`、scheduler 和 send counters；
- 删除或改名 `_run_payload_account_notification_exists()`；它不得继续参与 canonical health warning，因为该文件不是 scheduled delivery evidence；
- top-level 增加 `notification_authority.ordinary_scheduled_renderer=daily_brief`，并在 Phase A/B 通过 `notification_authority.legacy_aliases` machine-readable map 标明旧 field -> canonical field 与 `removal_phase=phase_c`；canonical `compatibility_notification` payload 本身不带 `deprecated_field`，只有 legacy alias 带；
- Compact 文件缺失不再追加 canonical notification warning，也不令 `summary.ok=false`；只在 compatibility-local status 中显示 `exists=false`；
- Phase C 在 `runtime_status_impl.py`、`analysis.py` 及其 contract tests 中删除 legacy `notification` output/input fallback、旧 derived fields 和旧 query column；canonical `compatibility_notification*` 字段继续保留，直至后续 Compact/artifact retirement work unit；
- `AccountResult.notification_text` 可暂时承载该 mixed compatibility bundle，但只作为 internal compatibility payload，poison tests 证明 sender 不读取；
- 未来物理删除/重命名 artifact 仍另立 work unit，本 P1 先消除其无标记 authority。

## 6. Implementation Slices

### Slice 0 — Baseline 与 worktree gate

前置条件：

- 继续在独立 worktree/branch 实施；不得触碰 dirty 主工作区。
- P1 implementation 必须以 `cd3d6c3d` 为基线；若 PR #108 已 merge，则从 merge 后 main 新建分支并核对等价 ancestry；否则使用 stacked branch。
- 记录当时所有 `notifications.daily_brief.enabled` / `notifications.render_style` committed 和 production-candidate 配置位置，只读检查优先。
- 盘点所有生产 scheduled launcher（cron/systemd/launchd/operator wrapper）；必须证明其使用 `./om run tick-cron` 或显式设置受支持的 `OM_TRIGGER_SOURCE=cron|scheduler`，且没有把 `--force` 固化在正常 scheduled command。未知/缺失 source 继续按 manual no-send fail-safe；若存在未分类 launcher，停止 cutover。

产物：Gateflow preflight artifact；不改代码。

### Slice 1 — 切断旧 scheduled authority，锁定 trigger 与 failure state

主要文件：

```text
src/application/multi_account_tick.py
src/application/tick_notification_flow.py
src/application/tick_run_context.py
src/application/scheduled_notification.py
src/application/multi_tick_finalization.py
domain/domain/multi_tick_result.py
domain/domain/__init__.py
src/application/config_validator.py
src/application/config_loader.py
src/application/config_defaults.py
configs/system.json
configs/examples/user.common.example.json
src/application/pipeline_runtime.py
tests/test_daily_decision_brief_notification_flow.py
tests/test_daily_decision_brief_scenarios.py
tests/test_scheduled_notification_application.py
tests/test_multi_tick_domain_step2.py
tests/test_validate_config_notifications.py
tests/test_config_loader_validation_cache.py
tests/test_tick_run_context.py
tests/test_multi_account_tick.py
tests/test_runtime_trigger_context.py
tests/test_tick_cron.py
tests/test_phase3_audit_idempotency_hooks.py
tests/test_multi_tick_contract_batch2.py
tests/test_domain_engine_batch5.py
tests/test_cli_operator_commands.py
tests/test_pipeline_runtime_paths.py
```

改动：

1. `run_tick_notification_flow()` 只对 `trigger_kind == scheduled` 无条件执行 `_prepare_daily_brief_notification()`；删除 `_daily_brief_enabled()` 和 scheduled Compact/Legacy branch。
2. `trigger_kind != scheduled` 在 Daily Brief preparation 前记录 `non_scheduled_ordinary_notification_disabled`，复用 existing finalization path 以 caller-provided reason 正常完成 tick；shared/account last-run、tick metrics、audit/run-end 均保留该 reason，不得伪装成 `no_account_notification`。不创建普通 revision/delivery identity、不调用 provider。同步更新 `run tick` command help、`--force` help 与 operator docs，明确直接手工调用和 force 都不会自动发送普通通知。
3. 删除旧 scheduled preparation 所独占的：
   - `PreparedMultiAccountNotification`；
   - `prepare_per_account_messages()`；
   - `query_multi_account_cash_footer_lines()`；
   - `prepare_multi_account_notification()`；
   - `build_account_messages()`；
   - `build_no_candidate_account_messages()`；
   - heartbeat metrics branch / `mark_no_candidate_notification_metrics()`（若最终 call graph 证明无其他 caller）。
4. 保留 `PreparedPerAccountMessages`，供 Daily Brief preparation/delivery 使用；不新建替代 DTO。
5. scheduled 分支解析 markets 后、调用 assembler/repository/provider 前验证 single-market；multi-market 时：
   - audit/runlog error code 固定 `daily_brief_multi_market_delivery_unsupported`；
   - shared/account last-run 与 `tick_metrics` 写 `sent=false` 和同一 reason/error code，`run_end` 为 error 而非 success/skip；
   - 写 tick idempotency terminal record：`ok=false,status=unsupported_failed,error_code=<code>,finished_at_utc=<ts>`；
   - CLI 返回 `2`；不生成 Daily Brief revision，不推进 current/delivered pointer，不调用 provider。
6. 最小扩展 `build_no_account_notification_payloads()` / `finalize_no_account_notification()` 接受 caller-provided `reason` 和 run-end outcome：默认值保持当前 `no_account_notification` byte-compatible；manual/force 使用正常完成语义，multi-market 使用 error 语义；不新增第二个 finalizer。
7. `build_tick_idempotency_context()` 把 normalized `trigger_kind=scheduled|manual|force` 纳入 key 和 record；同 config/accounts/market/minute 内，同 trigger retry 保持同 key，不同 trigger 必须不同 key。
8. `tick_run_context.complete_tick_idempotency()` 和 nested closure 窄增 `ok: bool=True`、`error_code: str|None=None`；现有 success/skipped caller 输出保持 byte-compatible。
9. duplicate claim 遇到同 key 的 `unsupported_failed` record 时不返回成功 skip：复用记录的 error code、记录 error run-end、返回 `2`、不重跑 pipeline；改为 single-market 的纠正请求因 `market_config` 不同使用新 key，可立即执行。
10. `tick_metrics.daily_brief` 增加固定 `renderer="daily_brief"`。
11. Phase A/B config validator 对已知旧 key 发 stable `[CONFIG_WARN]` 且值不参与路由；unknown style/type 继续 fail-closed；scheduled validation cache bump 到 `notification-renderer-v2`，unchanged config 不得沿用旧 `v1` cache。
12. pipeline compatibility artifact 固定使用 Compact；`pipeline_runtime.py` / `pipeline_alert_steps.py` 不再按 config 生成 Legacy，CLI/log 将 `symbols_notification.txt` 标为 compatibility bundle 而非 “prepared for sending”；移除 runtime sender 对 `notifications.render_style` 的读取，不再存在 unknown -> Legacy 行为。
13. 删除/重写只验证旧路径存在的 source-string contract tests。
14. 加入 poison test：`AccountResult.notification_text` 分别为空、Compact、Legacy/恶意 H1 时，最终 scheduled prepared message 和 delivery key 不变。

Acceptance：

- scheduled source/call graph 中无 Compact/Legacy import；
- manual/force scan 成功但无 ordinary prepare/revision/provider attempt，last-run/tick metrics reason 为 `non_scheduled_ordinary_notification_disabled`；同分钟 scheduled/manual/force idempotency keys 互不冲突；
- no-candidate 从 structured scheduled brief 渲染；
- old known config keys accepted-with-warning 且无选择权；Phase A unchanged config 会因 validator-version bump 重新校验；
- multi-market 有 terminal failed record、非零返回和 deterministic duplicate behavior，不再成功跳过或遗留 `in_progress`。

### Slice 2 — Compact compatibility、public read authority 与 Legacy deprecation

主要文件：

```text
src/application/notify_symbols.py
src/application/multi_tick/notify_format.py
src/application/pipeline_reporting.py
src/application/pipeline_alert_steps.py
src/application/agent_tools/notifications.py
src/application/agent_tools/notifications_impl.py
src/application/agent_tools/runtime_status_impl.py
src/application/agent_tools/analysis.py
tests/test_notify_symbols_markdown.py
tests/test_notification_compact.py
tests/test_multi_tick_notify_format.py
tests/test_agent_plugin_contract.py
tests/test_agent_plugin_smoke.py
tests/test_analysis_tools.py
docs/AGENT_WIKI.md
```

改动：

1. `build_notification()` 默认改为 `compact`，并严格校验 `compact|legacy`；unknown style raise。
2. `build_account_message_compact()` 标注 compatibility-only；保留完整 P0 flat contract。
3. `build_account_message()` 和 Legacy body branch 发 deprecation signal，但不参与 scheduled path。
4. `preview_notification` 仍是纯 renderer preview，schema/output/warnings 显式呈现 `renderer=compact|legacy`、`authority=compatibility_only`、`delivery_evidence=false`。
5. 盘点所有 `symbols_notification.txt` readers；`runtime_status` 的 shared/account/latest-run 输出增加 canonical `compatibility_notification` 和 top-level `notification_authority`，bundle metadata 使用 `artifact_kind=compatibility_notification_bundle`、`primary_renderer=compact`、`may_include=[candidate_reject_summary,close_advice]`；canonical payload 不带 `deprecated_field`，legacy `notification` alias 才带 `deprecated_field=true`，top-level alias map 给出 Phase C removal。
6. `_account_summary()`、latest-run helper 和 `analysis.runtime_tick_status` 全部改为 canonical compatibility 命名；Phase A/B 只在 public compatibility boundary 保留旧 derived/query aliases，`notification_authority.legacy_aliases` 与 `VIEW_SPECS["runtime_tick_status"].deprecated_fields` 提供 machine-readable migration，internal reads 仅为 pre-P1 payload fallback 到 `notification`。
7. `_runtime_diagnostic_records()` 不再把 `notification_exists=false` 或 `compatibility_notification_exists=false` 当作实际投递缺失；实际投递健康只由 `notification_diagnosis`、scheduler decision 和 send counters 决定。
8. 移除 “No symbols_notification.txt...” 作为 canonical health warning，并删除/改名只为该 warning 存在的 `_run_payload_account_notification_exists()`；Compact 缺失只在 compatibility-local metadata 中显示，不影响 `summary.ok`。
9. 文档说明 `symbols_notification.txt` 是 compatibility artifact，不是 prepared/sent Daily Brief evidence。
10. 不新增 `render_compact_tick()` wrapper、registry 或 runtime flag；复用现有函数完成兼容组合。

Acceptance：

- 显式 Compact preview 可运行且纯读；
- 显式 Legacy preview 可运行但有 warning；
- unknown style fail-closed；
- scheduled tests 证明这些函数没有被调用；
- runtime status 中每份旧 notification bundle 都有准确的 mixed-bundle compatibility metadata，且文件缺失不再造成 canonical notification warning；
- account summary 与 `runtime_tick_status` 使用 compatibility-specific canonical names，legacy aliases 仅存在于 Phase A/B compatibility boundary；analysis diagnostics 不把 artifact absence 解释为 delivery failure。

### Slice 3 — 合并 System Notice shell

主要文件：

```text
src/application/notification_shells.py
src/application/multi_tick/opend_guard.py
src/application/scheduled_notification.py
tests/test_notification_shells.py
tests/test_opend_watchdog_alerts.py
tests/test_scheduled_notification_application.py
```

改动：

1. 实现 `render_system_notice()` 和最小 private flat helper。
2. OpenD failure/recovery 改为向 shell 提交 caller-owned fields/sections。
3. delivery failure summary 改为向同一 shell 提交批次字段和逐账户 rows。
4. 标题统一为 `# OM · 系统通知 · <component>`；故障/恢复语义保留在 `状态｜...`、`影响/结果/诊断` 字段。
5. multiline reason/detail/error row 单行化；保留 OpenD detail limit 和 delivery failure diagnostics。
6. 不移动 `_send_notification()`、rate limit、consecutive threshold、send retry 或 failure-summary delivery。

Acceptance：

- OpenD failure/recovery、单/多账户 delivery failure 均通过 shared shell 和 mobile-flat assertion；
- rate-limit/recovery/send tests 行为不变。

### Slice 4 — 合并 Receipt shell

主要文件：

```text
src/application/notification_shells.py
src/application/trades/receipt.py
src/application/positions/maintenance_receipt.py
tests/test_notification_shells.py
tests/test_trades_receipt.py
tests/test_positions_maintenance_receipt.py
```

改动：

1. 实现/接入 `render_receipt()`。
2. 成交 title 改为 shared Receipt title，增加 `类型｜成交`。
3. 维护 title 改为 shared Receipt title，增加 `类型｜持仓维护`。
4. caller 继续组装：
   - assigned-stock confirmation-before-write；
   - Combo Yield relation pending；
   - projection/ledger diagnostics；
   - dry-run/applied/partial/failed/noop；
   - completed/error sections 和截断计数。
5. send decision、normalized error propagation、receipt identity/dedupe/persistence 不改。

Acceptance：

- 两类回执共享同一 H1 shell；
- 所有业务高风险提示原文语义保留；
- provider-local `FEISHU_POST_TOO_LARGE` 等 error 仍进入既有 receipt result，而不是 shell。

### Slice 5 — Aggregate validation、Phase A compatibility release 与迁移观察

初始 implementation PR 结束于 Phase A code；不提前执行 strict config rejection 或 Legacy 物理删除。

要求：

1. focused renderer/config/trigger/idempotency/delivery tests；
2. broad Daily Brief/multi-tick/notification/receipt/agent-tool tests；
3. Ruff/compileall/diff check；
4. DeepReview 检查 scheduled authority、manual/force no-send、hidden fallback、version-skew、terminal failure、runtime-status/analysis read-surface metadata 与 diagnostic semantics、shell ownership 和 P0 transport identity；
5. release/生产 config/canary/部署分别请求授权；
6. 首次部署 Phase A code 时保持生产 config 不变；部署前保存 scheduled launcher/trigger-source 只读证据，部署后确认实际 scheduled run 的 `trigger_kind=scheduled`，再确认旧 key 只发 warning、不控制 renderer；
7. 单独获批后清理 committed/generated/production candidate config，并证明无 old/new mixed fleet；
8. 在 `option-notification-experience` 实施前修订其 `daily_brief.enabled` 默认/rollback 合同并重新 PlanReview；
9. 部署至少一个 compatibility release 后收集第 4.4 节 read-only evidence。

### Slice 6 — Legacy Tick + strict config final cleanup（hard pause 后）

只有第 4.4 节全部通过并获得批准后执行；使用单独 cleanup PR/commit，仍归属本 P1 work unit。

主要文件：

```text
src/application/notify_symbols.py
src/application/multi_tick/notify_format.py
src/application/agent_tools/notifications.py
src/application/agent_tools/notifications_impl.py
src/application/agent_tools/runtime_status_impl.py
src/application/agent_tools/analysis.py
src/application/config_validator.py
src/application/config_loader.py
tests/test_notify_symbols_markdown.py
tests/test_notification_compact.py
tests/test_multi_tick_notify_format.py
tests/test_agent_plugin_contract.py
tests/test_agent_plugin_smoke.py
tests/test_analysis_tools.py
tests/test_validate_config_notifications.py
tests/test_config_loader_validation_cache.py
tests/test_close_advice_runner.py
docs/AGENT_WIKI.md
```

改动：

- 删除 Legacy Tick functions/branches/imports/tests；
- legacy preview 变为 actionable error；
- validator 对 `notifications.daily_brief.enabled` / `notifications.render_style` 任意出现严格 fail-closed；scheduled cache version bump 到 `notification-renderer-v3`；
- Compact compatibility metadata 和 artifact 保留，不恢复 runtime switch；
- 从 runtime status shared/account/latest-run、account summary 和 `analysis.runtime_tick_status` 删除 legacy `notification` aliases/fallbacks 与旧 derived/query names；只保留 `compatibility_notification*` canonical contract；
- 重新运行完整 gates 和 DeepReview；
- 更新 deprecation 文档为 removal/migration record。

Acceptance：

- clean config 正常；任一旧 key 给出稳定 migration error；即使 Phase A 已缓存相同 config hash，Phase C 也必须因 `v3` version mismatch 重新校验；
- Legacy import/call graph 为零，独立 close-advice renderer 不受影响；
- scheduled/manual/force、multi-market idempotency、runtime-status/analysis canonical compatibility metadata 和 P0 transport contracts 不回归；Phase C public payload/schema 中不存在旧 notification artifact aliases。

## 7. Test Matrix

### 7.1 Decision Brief authority / trigger / failure state

| Case | Expected |
|---|---|
| canonical tick-cron / scheduled source | `OM_TRIGGER_SOURCE=cron|scheduler` -> `trigger_kind=scheduled`；正常 scheduled command 不带 `--force` |
| missing/unknown source | `trigger_kind=manual` fail-safe；scan artifacts 可生成但 ordinary notification no-send |
| scheduled + candidates | Daily Brief full/delta output；无 Compact/Legacy call |
| scheduled + no candidates | Daily Brief `## 候选` 空状态；可用持仓/资金仍保留 |
| blocked pipeline | 明确 blocked/system data unavailable；不能描述成无候选 |
| degraded data | renderer 保留 data gap/当前可用事实 |
| `delivery_kind=none` | 不发送；不 fallback Compact |
| empty `notification_text` | structured scheduled brief 正常渲染 |
| poisoned Legacy/Compact `notification_text` | scheduled 输出和 delivery identity 不变 |
| explicit `should_notify=false` | 保留现有 scheduled 通知窗口否决语义 |
| multi-account | account isolation；各自 message/delivery key |
| manual/force | scan/run artifacts 成功；shared/account last-run、tick metrics、audit 明确 `non_scheduled_ordinary_notification_disabled`；无 Daily Brief prepare/revision/delivery/provider；tick idempotency 正常完成 |
| scheduled/manual/force same minute | config/accounts/market/bucket 相同但 `trigger_kind` 不同，三个 idempotency keys 不同；同 trigger retry key 稳定 |
| multi-market first attempt | assembler/repository/provider 前 fail-closed；last-run/tick metrics/run-end 与 terminal record 均为同一 unsupported failure；`ok=false,status=unsupported_failed,error_code=...`；返回 2；无 revision/current/delivered pointer |
| multi-market same-key retry | 不重跑 pipeline；复用 terminal error 并记录 error run-end；返回 2，不伪装 duplicate success |
| corrected single-market retry | 新 market_config/idempotency key；正常进入 scheduled Daily Brief |

### 7.2 Config / version skew / compatibility

- 无 `daily_brief` object：使用 renderer defaults，scheduled 仍为 canonical Daily Brief。
- Phase A/B `daily_brief.enabled=true|false`：类型合法则成功、发稳定 deprecation warning、两值渲染/发送 identity 相同。
- Phase A/B `notifications.render_style=compact|legacy`：成功、发稳定 deprecation warning、均不影响 runtime sender。
- Phase A/B `render_style=typo` 或错误类型：fail-closed。
- Phase A 从旧 `validator_version=v1` cache 启动且 config hash 不变：必须重新校验、输出 deprecation warning，并把 cache 写为 `notification-renderer-v2`。
- Phase C 任一旧 key：migration error；Phase A/B `v2` cache 不得绕过，首次运行写/检查 `notification-renderer-v3`。
- old/new code × old/clean config 六格矩阵与第 4.3 节一致；old+clean 只作为停止 scheduler 后的整版本 rollback 验证。
- preview `compact`：成功并返回 compatibility metadata/warning。
- preview `legacy`：Phase A/B 成功并返回 deprecation warning；Phase C 报错。
- preview unknown：始终报错，不进入 Legacy。
- Compact full fixture 继续验证候选、持仓、资金、无候选和 P0 flat contract。

### 7.3 Public read surfaces

- shared/account/latest-run bundle 均带 `artifact_kind=compatibility_notification_bundle`、`primary_renderer=compact`、`may_include`、`authority=compatibility_only`、`delivery_evidence=false`。
- Phase A/B canonical `compatibility_notification` 与 legacy `notification` alias 内容相同，alias 带 `deprecated_field=true`；Phase C 删除 alias 和 legacy fallback。
- `account_summary` canonical fields 为 `compatibility_notification_exists` / `compatibility_notification_mtime_utc` / `accounts_with_compatibility_notification`；Phase A/B 旧名称仅为 deprecated aliases，Phase C 删除。
- `analysis.runtime_tick_status` canonical column 为 `compatibility_notification_exists`；Phase A/B 旧 `notification_exists` 仅为 deprecated query alias，Phase C 删除。
- top-level `notification_authority.ordinary_scheduled_renderer=daily_brief`；Phase A/B `legacy_aliases` 列出 runtime-status output/summary aliases 与 `removal_phase=phase_c`，canonical payload 不被误标 deprecated。
- Compact file missing：compatibility metadata `exists=false`，不追加 canonical warning，不令 `summary.ok=false`，也不让 runtime analysis 产生 `observed_notification_missing`；实际投递失败仍由 `notification_diagnosis`/send counters 正常报告。
- runtime status/analysis 不把 bundle 描述为纯 Compact renderer 输出，也不把它描述为 prepared/sent scheduled message。

### 7.4 System Notice

- OpenD single-line failure。
- OpenD multiline reason/detail：压平为单行，无第二层缩进。
- OpenD recovery。
- no-send 不消耗 rate limit；failed send 不消耗 confirmed alert quota。
- delivery failure：无成功账户、部分成功、多个失败账户。
- provider response code、message ID、attempt count、confirmed/unconfirmed 完整保留。
- failure summary 本身发送失败仍沿用既有 failure semantics，不递归创建新 renderer/fallback。

### 7.5 Receipt

- trade applied、failed、projection verification failed。
- assigned-stock ambiguity：必须显示候选批次、确认前不写入、下一步。
- Combo Yield relation pending：必须说明当前按单腿记录且未自动归组。
- missing optional fields：显式 `-` 或省略，不能伪造值。
- maintenance dry-run、applied、partial failure、full failure、noop、empty applied/errors。
- maintenance multiline errors flat。
- trade/maintenance send confirmed、unconfirmed、timeout、exception、Feishu size error。
- idempotency/dedupe/receipt state tests结果不变。

### 7.6 P0 transport 与视觉合同

每个 family 至少一个真实 renderer fixture：

1. Daily Brief；
2. Compact compatibility；
3. System Notice；
4. Trade Receipt；
5. Maintenance Receipt。

共同断言：

- 一个 standalone H1；
- 无 `###`、blockquote、Markdown table、nested list；
- Feishu payload 仍为 exactly one `md` node；
- WeChat message 等于 canonical Markdown；
- canonical Markdown 不被 adapter 改写；
- Feishu 最终 request-body byte preflight、UUID/retry/confirmation tests 不变。

## 8. Validation Commands

实现时按 slice 先 focused、后 broad：

```bash
./.venv/bin/python -m pytest -q \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_daily_decision_brief_scenarios.py \
  tests/test_validate_config_notifications.py \
  tests/test_config_loader_validation_cache.py \
  tests/test_tick_run_context.py \
  tests/test_phase3_audit_idempotency_hooks.py \
  tests/test_cli_operator_commands.py

./.venv/bin/python -m pytest -q \
  tests/test_notification_shells.py \
  tests/test_opend_watchdog_alerts.py \
  tests/test_scheduled_notification_application.py \
  tests/test_trades_receipt.py \
  tests/test_positions_maintenance_receipt.py

./.venv/bin/python -m pytest -q \
  tests/test_notify_symbols_markdown.py \
  tests/test_notification_compact.py \
  tests/test_multi_tick_notify_format.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py

./.venv/bin/python -m pytest -q \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_feishu_bot.py

./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run

./.venv/bin/python -m ruff check \
  src/application/multi_account_tick.py \
  src/application/tick_notification_flow.py \
  src/application/tick_run_context.py \
  src/application/config_loader.py \
  src/application/scheduled_notification.py \
  src/application/agent_tools/runtime_status_impl.py \
  src/application/notification_shells.py \
  src/application/multi_tick/opend_guard.py \
  src/application/trades/receipt.py \
  src/application/positions/maintenance_receipt.py

./.venv/bin/python -m compileall -q domain src tests
git diff --check
```

Phase A release 前必须把第 4.3 节 version-skew matrix 转为 deterministic tests；Phase C 前再次运行同一 matrix，确认 strict rejection 只发生在 clean-config gate 之后。

若仓库实际测试文件名/命令已变化，实现者先读当前 source/test authority 后调整，不得跳过等价覆盖。

## 9. Rollback 与故障恢复

### 9.1 Renderer / config rollback

- Phase A/B 中旧 config keys 仅 accepted-with-warning，不拥有控制权；不能用 `enabled=false` 或 `render_style=compact` 做新代码的行为回退。
- renderer 回滚只能通过停止 scheduler 后的 code/version rollback；禁止 old/new mixed fleet。
- old code + clean config 会因缺少 `enabled=true` 回到 Compact；这是整版本 rollback 的已知结果，必须在 maintenance window 内验证，不能作为在线 feature flag。
- `option-notification-experience` 当前关于 `daily_brief.enabled=false` 的行为回退合同被本 P1 supersede；该计划在实施前必须修订为停止 scheduler + code/version/state-compatible rollback，并重新 PlanReview。
- Daily Brief renderer preparation 失败时 fail-closed；不自动发送 Compact。
- Compact compatibility artifact 不得被 delivery flow 自动读取。
- Legacy 删除后若必须恢复，只能回滚 release，不临时复制旧分支回生产。

### 9.2 Provider rollback

完全继承 P0：

- Feishu Post 视觉/协议不符合预期时，回滚 adapter code/version 到 text；
- Post timeout、ambiguous 或 confirmed 后不得为同一业务事件自动补发 text；
- `FEISHU_POST_TOO_LARGE` 是 HTTP 前确定失败，仍按现有 operator-controlled replay 规则处理；
- renderer rollback 和 Post->text rollback 是两个独立版本动作，不能互相触发隐藏 fallback。

### 9.3 Shell rollback

System/Receipt shell 只是 presentation owner；若发现字段丢失：

1. 停止 rollout；
2. 保留 caller facts 和发送状态证据；
3. 回滚 shell integration commit；
4. 不修改 rate limit、receipt state、idempotency 或 delivery confirmation 来掩盖渲染问题。

## 10. 文档、telemetry 与删除证据

需要更新：

- `docs/AGENT_WIKI.md`：scheduled Decision Brief、manual/force no-send、System Notice/Receipt owner、compatibility/deprecation 边界。
- config defaults/examples：Phase A 移除旧 renderer keys；validator 保留 warning compatibility；Phase C 记录 strict removal。
- `option-notification-experience` plan：在其实施前移除 `daily_brief.enabled` 默认/行为回退依赖，并重新 PlanReview。
- Gateflow artifacts：每 slice implementation/review、version-skew matrix、compatibility release evidence、config migration、Legacy/strict removal decision。
- existing `tick_metrics.daily_brief`：添加 `renderer=daily_brief`；不新增新 telemetry service。
- `preview_notification` / `runtime_status`：Compact compatibility metadata；runtime status 的 canonical renderer authority 为 Daily Brief。
- tick idempotency/audit：multi-market 使用 `unsupported_failed` + stable error code；manual/force 使用 `non_scheduled_ordinary_notification_disabled` success evidence。

Phase C 前的 read-only evidence bundle 至少包含：

```text
git/rg import graph and symbols_notification readers
committed/generated/production-candidate config scan
./om-agent config_validate warning-free clean config evidence
old/new code x old/clean config deterministic matrix
Phase A v2 / Phase C v3 scheduled validator-cache revalidation evidence
no old/new mixed fleet evidence
./om-agent runtime_status compatibility metadata + notification authority
latest scheduled tick_metrics.daily_brief.renderer
scheduled delivery audit: no compact/legacy attempt
manual/force audit: no ordinary provider attempt
multi-market terminal idempotency failure evidence
focused compatibility fixture results
revised option-notification-experience PlanReview artifact
```

不得通过真实通知探针绕过显式 canary 授权。

## 11. 残余风险与后续 owner

| 风险 | 严重度 | 本 P1 处理 | 后续 owner |
|---|---|---|---|
| Daily Brief 当前 `full/delta/none` 不保证每个固定点发送 | 中 | 明确非目标，不伪装解决 | `option-notification-experience` work unit |
| manual/force successful current 尚未实现 | 中 | P1 仅禁自动普通通知并保留 run artifacts | `option-notification-experience` snapshot work |
| 多市场一账户多消息尚无 delivery model | 高 | P1 terminal fail-closed，非零返回，不静默 skip | `option-notification-experience` account+market delivery |
| Phase A/B 外部消费者仍读取 legacy `notification*` aliases | 中 | 至少一个 compatibility release 保留 alias；Phase C 前盘点消费者，Phase C contract tests 证明 alias 删除 | P1 Gateflow owner |
| mixed `symbols_notification.txt` compatibility bundle 仍在正常 pipeline 生成 | 中 | public readers 标注 bundle composition/compatibility-only；不影响 health/sender | 后续 Compact/artifact retirement work unit |
| `AccountResult.notification_text` 仍承载 compatibility Markdown | 中 | poison tests 证明非权威 | structured account outcome migration |
| old code + clean config 回滚会恢复 Compact | 中 | 只允许停止 scheduler 后整版本回滚；禁止 mixed fleet | release/runbook owner |
| P1 与 broader scheduler/snapshot plan 都会修改 `tick_notification_flow.py` | 高 | P1 先行；broader plan 必须 rebase、修订 config/rollback、重新 PlanReview | Gateflow owner |
| Legacy deprecation 长期不删除 | 中 | concrete release/evidence/approval gate + final cleanup slice | P1 Gateflow owner |
| shell 过度通用导致业务语义下沉 | 中 | 只允许两个 public functions + tuple fields/sections；DeepReview 检查 | P1 review gate |
| 真实 Feishu/WeChat 客户端视觉差异 | 中 | deterministic tests；真实 canary 单独授权 | P0/P1 rollout gate |

## 12. Gate Order 与停止条件

```text
preflight
  -> revised PlanReview
  -> Slice 1 implementation/review
  -> Slice 2 implementation/review
  -> Slice 3 implementation/review
  -> Slice 4 implementation/review
  -> aggregate DeepReview
  -> Phase A compatibility release gate (explicit approval)
  -> deploy Phase A code with production config unchanged
  -> production read-only observation
  -> config migration gate (separate explicit approval)
  -> version-skew matrix + no mixed-fleet evidence
  -> revise option-notification-experience config/rollback + PlanReview
  -> Phase C Legacy/strict-config deletion decision (explicit approval)
  -> Slice 6 cleanup/review/DeepReview
  -> final closeout
```

立即停止并返回 CEO 的条件：

- P0 baseline 与当前 main/PR 出现不可安全重放的冲突；
- implementation 需要 manual/force 自动发送普通通知，或无法在不新增 sender 的前提下完成 no-send boundary；
- Phase A 新代码不能接受旧 known config keys 并发出稳定 warning，或旧 validation cache 会跳过 v2 revalidation；
- 生产存在无法停止/升级的 old/new mixed fleet；
- 任一生产 scheduled launcher 未使用 canonical `tick-cron`/受支持 trigger source，或正常计划任务固化了 `--force`；
- `option-notification-experience` 仍依赖 `daily_brief.enabled=false` 且未完成修订/PlanReview；
- scheduled runtime 真实存在 multi-market 单 tick，且业务不能接受 terminal fail-closed；
- multi-market 失败无法写 `ok=false` terminal record 或 deterministic non-zero duplicate result；
- Daily Brief structured artifacts 无法覆盖当前 Compact 中必须保留的业务事实；
- public reader 无法把 Compact 明确标为 compatibility-only，或 runtime-status/analysis 仍把其缺失解释为 canonical delivery failure；
- shell integration 会迫使业务状态、dedupe、retry 或 provider contract进入通用 helper；
- focused/broad tests 或 DeepReview 出现 unresolved high/critical finding；
- Phase C 证据不完整或未获明确批准。

## 13. PlanReview 重点

下一轮 `$planreview` 应重点挑战：

1. scheduled-only authority 是否真的没有把 manual/force 重新接入 ordinary sender；生产 launcher 是否都能可靠分类为 scheduled；manual/force finalization reason 是否准确，trigger-scoped idempotency 是否避免同分钟吞掉 scheduled run；
2. Phase A/B accepted-with-warning 是否完全移除旧 key 的控制权，并覆盖 old/new code × old/clean config；Phase A v2 / Phase C v3 cache-version bump 是否阻止 unchanged config 绕过新 validator；
3. `option-notification-experience` 的 config/rollback 冲突是否由明确 sequencing 和 hard gate 收敛；
4. runtime status/analysis 是否以 `compatibility_notification*` 为 canonical contract、legacy aliases 是否只停留在有明确 exit gate 的 compatibility window、Phase C 是否有完整删除 slice，且 bundle 缺失是否不再影响 canonical health/delivery diagnostics；
5. multi-market `unsupported_failed` 是否有 `ok=false` terminal record、返回 2、same-key deterministic replay 和 corrected-request recovery；
6. Legacy/strict-config deletion exit gate 是否足够可执行且不会无限延期；
7. `notification_shells.py` 是否保持最小、没有演化为 DTO/registry/DSL；
8. assigned-stock、Combo Yield、receipt idempotency、OpenD rate limit 和 delivery failure retry 是否仍由原 caller owner；
9. P0 Post/WeChat identity、size、retry、rollback 是否被任何 renderer/shell 改动破坏；
10. 与 broader plan 的文件冲突是否要求后实现者 rebase 后重新 review，而不是静默覆盖 P1 合同。
