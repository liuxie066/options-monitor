# 渠道感知通知渲染实施计划

> 日期：2026-07-21
> 状态：Revised after `docs/reviews/plan-review-20260721-142232.md`
> 基线：`main@9b1e200ed313407c3f20708a33549ba0d5e46cf0` / `VERSION=1.4.0`

## 1. 目标

在不拆分业务 renderer、不改变微信 ClawBot Markdown、不改变通知幂等与确认语义的前提下：

1. 飞书 App 主动通知改用 `msg_type=post`；
2. 每条飞书 post 的 `zh_cn.content` 只包含一个 `md` node；
3. 微信 ClawBot 继续收到原始 canonical Markdown；
4. 飞书 post 不符合视觉或协议预期时，通过受控代码/版本回滚恢复 `text`；
5. 禁止 post 已发起、超时或结果不明确后，在同一业务事件内自动补发 text；HTTP 前确定失败按第 2.3 节恢复。

## 2. 已锁定决策

### 2.1 渠道投影

```text
Business facts / structured brief
        │
        ▼
Existing business renderer
        │
        ▼
Canonical Markdown string
        │
        ├── Feishu adapter
        │      └── msg_type=post
        │          content.zh_cn.content =
        │          [[{"tag":"md","text": markdown}]]
        │
        └── WeChat adapter
               └── existing text_item.text = markdown
```

- 不创建 Feishu/WeChat 两套业务 renderer。
- 不新增 renderer registry、message DTO、content enum、middleware 或运行时 format config。
- 微信 projection 是 identity；不新增 `render_wechat_markdown()`。
- `post.title` 是可选字段，但本 slice **有意省略**，继续使用 canonical Markdown 内的 H1，避免双标题。
- Feishu inbound reply/outbox 继续使用现有 `reply_text_message()`；本计划只改变主动通知链路。

### 2.2 超限策略：HTTP 前 fail-closed

飞书官方限制 text 最大 150 KB、post 最大 30 KB。为避免静默丢字段，并保持“发现方案 B 不符合预期后受控回滚”的产品要求，本计划选择：

- **不截断** canonical Markdown；
- **不拆成多条消息**；
- **不自动 fallback 到 text**；
- 在任何 token 获取或消息 HTTP 请求前，计算最终请求体的 UTF-8 byte size；若超过安全预算，立即 fail-closed。

安全预算固定为：

```python
FEISHU_POST_REQUEST_BUDGET_BYTES = 28 * 1024
```

理由：28 KiB 同时低于 30,000 bytes 和 30 KiB，为平台口径、字段增长和序列化差异保留余量。

byte size 必须基于**最终完整 payload**，并使用与 `http_json()` 相同的序列化方式：

```python
len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
```

计算必须包含：

- `receive_id`；
- `msg_type`；
- 已二次 JSON 序列化的 `content` string；
- 可选 `uuid`。

超限时使用现有 `FeishuPermanentError`，不得伪造上游 HTTP 状态或飞书业务 code：

```text
local_error_code = FEISHU_POST_TOO_LARGE
http_status = None
feishu_code = None
http_attempts = []
```

错误结果至少包含：

- `request_body_bytes`；
- `request_body_budget_bytes`；
- `normalized_markdown_chars`；
- `normalized_markdown_sha256`；
- `local_error_code=FEISHU_POST_TOO_LARGE`。

不记录原始通知正文。

`scheduled_notification` 必须保留该 local error 和大小诊断：

- Feishu normalizer 同时输出 `local_error_code` 和通用 `error_code=FEISHU_POST_TOO_LARGE`；`_notify_error_code()` 优先返回 normalized `error_code`；
- attempt/audit record 保存上述大小字段；
- 因 `FEISHU_POST_TOO_LARGE` 不在 retryable error code 集合中，同一执行不得重复尝试；
- 既有 failure summary 可汇总该失败，failure summary 本身仍使用同一 Feishu post sender；若 summary 也超限，则同样 fail-closed，不做 text fallback。

### 2.3 幂等、重试和回滚

- notification idempotency key 继续基于调用方传入的**原始 canonical Markdown**构造；算法不变。
- 合法 post payload 在所有 HTTP retry 中复用相同 `uuid` 和完全相同的 payload。
- 没有 UUID 时维持单次 HTTP attempt；有 UUID 时维持现有最多三次 transport retry。
- 本地 size failure 发生在 HTTP 前：没有 ambiguous send、没有 duplicate risk、没有 transport retry。
- timeout 或 transient failure 后禁止自动改发 text，因为 post 可能已被飞书接受。
- 回滚是代码/版本回滚：停止 rollout，保留 evidence，把 adapter 恢复为 `send_text_message()`，跑测试并发布修复版本；使用新的 canary/run ID 验证 text。
- 已确认送达或存在 ambiguous send 的 post，不为同一业务事件补发 text。
- `FEISHU_POST_TOO_LARGE` 在 token/HTTP 前确定失败，已证明没有上游发送；完成 text 代码回滚后，operator 可显式决定是否以新的 transport UUID 重放该业务通知，并把重放记录关联到原 size-failure audit。不得由原执行自动 fallback。

## 3. Payload Contract

目标短消息 payload：

```python
payload = {
    "receive_id": open_id,
    "msg_type": "post",
    "content": json.dumps(
        {
            "zh_cn": {
                "content": [
                    [
                        {
                            "tag": "md",
                            "text": markdown,
                        }
                    ]
                ]
            }
        },
        ensure_ascii=False,
    ),
}
if uuid:
    payload["uuid"] = str(uuid)
```

不添加 `title`，不解析第一行，不把 Markdown 拆为多个 post node。

输入 normalization 与现有 text sender 对齐：

- `open_id = str(open_id or "").strip()`；
- `markdown = str(markdown or "").strip()`；
- size metadata 中的 `normalized_markdown_*` 均基于上述 strip 后、实际准备发送的 Markdown；调用方原文仍用于既有 idempotency key；
- 空或纯空白 open ID：`ValueError("open_id is required")`；
- 空或纯空白 Markdown：`ValueError("markdown is required")`。

## 4. Implementation Boundary

### 4.1 `src/infrastructure/feishu_bot.py`

1. 保留 `send_text_message()` 和 `reply_text_message()` 行为不变。
2. 新增：

```python
def send_post_message(
    *,
    app_id: str,
    app_secret: str,
    open_id: str,
    markdown: str,
    uuid: str | None = None,
    log_fn: Callable[[dict[str, Any]], Any] | None = None,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    ...
```

3. `send_post_message()` 负责：
   - 输入校验；
   - 构造唯一 `md` node；
   - 构造最终 payload；
   - 在 token 获取和 HTTP 调用前执行 28 KiB preflight；
   - 合法 payload 沿用现有 token/UUID/retry/logging 语义；
   - 超限抛出带结构化 response metadata 的 `FeishuPermanentError`。
4. 可添加小型 private helper 构造 post payload和计算 bytes；不引入类、通用 builder 或 provider-neutral abstraction。
5. 是否抽取 text/post 公用 send helper 由实现时最小 diff 决定；不得改变现有 text tests 的行为。

### 4.2 `src/application/notification_delivery_adapter.py`

1. 将 Feishu 主动通知从：

```python
send_text_message(text=message, ...)
```

切换为：

```python
send_post_message(markdown=message, ...)
```

2. 保留凭据解析、bot open ID、request path、UUID、HTTP attempt logging、message ID 提取和 delivery confirmation。
3. 捕获 size preflight 的 `FeishuPermanentError` 时，把 exception response 中的以下字段复制到 send result：
   - `local_error_code`；
   - `request_body_bytes`；
   - `request_body_budget_bytes`；
   - `normalized_markdown_chars`；
   - `normalized_markdown_sha256`。
4. `normalize_feishu_app_send_output()` 把这些字段原样放入 normalized extra；size failure 同时设置通用 `error_code=FEISHU_POST_TOO_LARGE`，使 scheduled notification、trade receipt 和 maintenance receipt 的既有结果组装自动保留该错误；不得把本地错误伪装成 Feishu `230025`。
5. `request_path`、failure stage 和 tool name 保持不变。

### 4.3 `src/application/scheduled_notification.py`

1. `_notify_error_code()` 优先采用 normalized `error_code`，否则维持 `SEND_UNCONFIRMED` / `SEND_FAILED`。
2. send attempt/audit record 增加 size/local-error 字段透传。
3. 不把 `FEISHU_POST_TOO_LARGE` 加入 `NOTIFY_SEND_RETRYABLE_ERROR_CODES`。
4. 不修改通用发送状态机、confirmation 规则、idempotency key 或 failure-summary 生成逻辑。

### 4.4 不修改的业务 renderer

```text
src/application/multi_tick/notify_format.py
src/application/daily_decision_brief_renderer.py
src/application/trades/receipt.py
src/application/positions/maintenance_receipt.py
src/application/tick_notification_flow.py
```

原因：业务 renderer 继续维护 canonical Markdown；飞书容量是 provider contract，不下沉到业务层，也不缩短微信内容。

## 5. Covered Notification Paths

共享 `NotificationDeliveryAdapter` 的变化覆盖：

- legacy/compact tick notifications；
- Daily Decision Brief；
- multi-account delivery failure summary；
- OpenD failure/recovery notices；
- trade-intake receipts；
- option-position auto-close/maintenance receipts。

当前 committed runtime route 使用 `wechat_clawbot`，且 Daily Brief 默认关闭。本 work unit 不修改任何运行时 config。

## 6. Test Plan

### 6.1 `tests/test_feishu_bot.py`

保留所有现有 text/reply/reaction tests，并新增：

1. 短消息 payload：
   - `msg_type == "post"`；
   - `json.loads(payload["content"]) == {"zh_cn":{"content":[[{"tag":"md","text": markdown}]]}}`；
   - 没有 `title`；
   - Chinese、Unicode、引号、换行保持正确。
2. UUID/retry：
   - 有 UUID 时 payload 包含 UUID、HTTP retry attempts 为 3；
   - 无 UUID 时不包含 UUID、HTTP retry attempts 为 1。
3. 校验：空 open ID、空 Markdown、纯空白 Markdown。
4. byte preflight：
   - ASCII、中文、emoji、引号和大量换行均按最终 outer request body 计算；
   - 低于 28 KiB 时原文不变并发生一次 HTTP 调用；
   - 高于 28 KiB 时抛出 `FeishuPermanentError`，`http_json_fn` 和 token 获取均未调用；
   - error metadata 包含 bytes/budget/chars/hash，不包含原文；
   - 有/无 UUID 的边界分别覆盖。

### 6.2 `tests/test_feishu_notification_sender.py`

1. mock/capture `send_post_message(markdown=...)`；验证 credentials、open ID、UUID 和 message normalization 不变。
2. 验证 Feishu API 成功仍需 `code=0 + message_id` 才 delivery confirmed。
3. 验证 size preflight error 被归一化为：
   - `command_ok=False`；
   - `delivery_confirmed=False`；
   - `local_error_code=FEISHU_POST_TOO_LARGE`；
   - `error_code=FEISHU_POST_TOO_LARGE`；
   - `http_attempts=[]`；
   - `ambiguous_send=False`；
   - `duplicate_risk=False`。
4. 渠道分歧回归：
   - Feishu sender 收到原始 canonical Markdown 的 `markdown` 参数；
   - WeChat sender 收到完全相同原文的 `text` 参数。

### 6.3 `tests/test_scheduled_notification_application.py`

新增 local size failure 状态机测试：

- attempt record/audit 保存 `FEISHU_POST_TOO_LARGE` 和 byte diagnostics；
- `will_retry=False`，即使 `max_attempts > 1`；
- 不改变其他 `SEND_FAILED` / `SEND_UNCONFIRMED` retry 行为；
- failure summary 能看到该 error code。

### 6.4 Direct receipt error propagation

在 `tests/test_trades_receipt.py` 和 `tests/test_positions_maintenance_receipt.py` 增加回归：

- normalized sender 返回 `error_code=FEISHU_POST_TOO_LARGE` 时，receipt result 保留同一 error code；
- `delivery_confirmed=False`、`status=failed`；
- 不需要修改两个 receipt 的业务 renderer 或消息正文。

### 6.5 Renderer fixture contract

使用当前真实 renderer 生成至少以下 fixture，并把**原样输出**送入 Feishu payload test：

1. Daily Brief：H1/H2、quote、ordered/nested list、bold、中文金额和合约；
2. compact tick：候选、持仓、资金、缺数提示；
3. trade receipt：普通字段行、候选 lot、状态和原因；
4. maintenance receipt：普通字段行、明细列表、错误列表；
5. failure/recovery notice：failure summary 或 OpenD recovery message。

fixture test 证明 payload 未重写 canonical Markdown；视觉表现由 canary gate 验收。

### 6.6 Focused validation

```bash
PYTHONPYCACHEPREFIX=/tmp/om-channel-render \
python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_feishu_bot.py \
  tests/test_feishu_notification_sender.py \
  tests/test_scheduled_notification_application.py
```

### 6.7 Broader regression

```bash
PYTHONPYCACHEPREFIX=/tmp/om-channel-render \
python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_scheduled_notification_multi_account_application.py \
  tests/test_trades_receipt.py \
  tests/test_positions_maintenance_receipt.py \
  tests/test_multi_tick_notify_format.py \
  tests/test_notification_compact.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_notification_flow.py
```

另运行项目支持的 Ruff/static checks、`compileall` 和 `git diff --check`。

## 7. Canary Acceptance Matrix

真实 canary 是通知发送，必须单独获得用户明确批准。实施、测试或 review 阶段不得自动发送。

每条 canary 使用独立 canary ID/UUID，不复用真实业务事件的 idempotency key；按以下顺序发送并保存 message ID、响应摘要、桌面端和移动端截图：

| 类别 | 必须使用的内容来源 | 主要验收点 |
|---|---|---|
| Daily Brief | 当前 renderer fixture | H1/H2、quote、ordered/nested list、bold、中文、金额、合约 |
| Compact Tick | 当前 renderer fixture | 候选/持仓/资金分区、列表缩进、缺数提示 |
| Trade Receipt | 当前 renderer fixture | 一字段一行仍清晰、候选 lot 不合并、状态/原因完整 |
| Maintenance Receipt | 当前 renderer fixture | 字段行、明细列表、错误列表层次清晰 |
| Failure/Recovery | 当前 renderer fixture | 错误 code、attempt、message ID 等诊断不丢失 |

每类都必须满足：

- message ID 存在且 delivery confirmed；
- 不显示原始 `#`、`##`、`**` 等未解析标记；
- 没有 JSON 转义泄漏；
- 字段未丢失、未截断；
- 中文、金额、合约数量保持；
- 桌面端和移动端可读性不低于当前 text；
- trade/maintenance receipt 保持“一字段一行”的视觉语义。

任一关键类别失败：停止 rollout，不继续后续类别；记录 evidence，进入受控 text 回滚。不得为刚才已确认送达的 canary 补发 text。

size boundary 主要由 deterministic tests 证明。除非用户另行批准，不发送接近 28 KiB 的超长真实 canary。

## 8. Rollback Procedure

触发条件：

- API 持续拒绝 post；
- 出现 `FEISHU_POST_TOO_LARGE` 的真实业务消息；
- 标题、引用、列表或字段行视觉异常；
- 字段缺失、JSON escape 泄漏或移动端明显退化；
- delivery confirmation 行为改变。

步骤：

1. 停止 Feishu rollout/canary；
2. 保留响应、message ID、local error、payload byte summary 和截图；
3. 把 `notification_delivery_adapter.py` 的 Feishu sender 恢复为 `send_text_message(text=...)`；
4. 保留 `send_post_message()` 与测试还是删除，由最小、清晰的 rollback diff 决定，不引入 runtime switch；
5. 运行 focused + broader tests；
6. 若生产已受影响，发布新的 fix version；
7. 使用新的 canary/run ID 验证 text；
8. 不对已确认 post 的同一业务事件补发 text。

## 9. Documentation

更新 `docs/AGENT_WIKI.md`：

- canonical Markdown ownership；
- Feishu `post` + single `md` node；
- WeChat identity Markdown；
- 28 KiB HTTP-before-send fail-closed contract；
- 禁止自动 text fallback；
- canary 与回滚需要显式批准。

不修改运行时配置示例，不新增 format key。

## 10. Implementation Slices

### Slice 1 — Infrastructure payload and size preflight

- 修改 `src/infrastructure/feishu_bot.py`；
- 新增 post payload、28 KiB preflight、permanent local error；
- 完成 `tests/test_feishu_bot.py`。

**Gate**：text sender tests 全部不变；post payload 与 byte boundary tests 通过。

### Slice 2 — Feishu application adapter and normalization

- 修改 `src/application/notification_delivery_adapter.py`；
- 切换 post sender；
- 传播 local size diagnostics；
- 完成 `tests/test_feishu_notification_sender.py`。

**Gate**：delivery confirmation、UUID、retry、WeChat identity regression 通过。

### Slice 3 — Audit/retry semantics

- 修改 `src/application/scheduled_notification.py`；
- 保留 local error/size fields；
- size failure 不重试；
- 完成 scheduled notification tests。

**Gate**：size failure fail-closed 且 failure summary/audit 可诊断，其他 retry 行为无回归。

### Slice 4 — Renderer fixtures, docs and full validation

- 添加五类真实 renderer fixture contract；
- 更新 `docs/AGENT_WIKI.md`；
- 运行 focused/broader/static checks。

**Gate**：所有本地 checks 通过；不发送 canary。

### Slice 5 — Explicitly approved canary and rollout

- 仅在用户明确批准后执行五类 canary；
- 逐类验收，任一失败立即停止并回滚。

**Gate**：五类全部通过后才可判定 Feishu post rollout 合格。

## 11. Success Criteria

实施完成必须同时满足：

1. 短消息 Feishu payload 严格为 post + single md node；
2. WeChat 获得与改动前相同的 canonical Markdown；
3. post 请求体不超过 28 KiB；超限在 HTTP 前失败、无重试、无歧义、可审计；
4. UUID、retry、message ID confirmation 和 failure stage 不变；
5. 五类 renderer fixture contract tests 通过；
6. focused、broader、static checks 全部通过；
7. 未经批准不发送真实 canary；
8. 获批后，五类 canary 在桌面端和移动端全部通过；
9. 若 canary 触发 rollback，恢复 text 后使用新 run ID 验证；已发送或发送状态不明确的事件不得重复。HTTP 前 size failure 可由 operator 在回滚后显式重放，并关联原 audit。

## 12. Explicit Non-goals

- Feishu interactive cards、模板、按钮或 callback；
- post 多 node 或多消息分片；
- 自动 text fallback；
- runtime format feature flag；
- 改写业务 renderer 以迎合飞书；
- 修改 Daily Brief lifecycle/revision/diff/confirmation pointer；
- persisted schema migration；
- 修改 idempotency key algorithm；
- inbound Assistant reply 或 Feishu WS reply outbox；
- 修改微信 sender；
- 生产 config、release、remote upgrade 或真实通知发送。
