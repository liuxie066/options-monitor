# Gateflow Implementation Plan — Feishu Reaction ACK Latency

- **Work unit**: `feishu-reaction-ack-latency`
- **Created at**: 2026-07-17 23:00:43 CST（本机系统时钟）
- **Last revised at**: 2026-07-17 23:42:46 CST（本机系统时钟）
- **Finding source**: `docs/reviews/plan-review-20260717-224453.md`
- **Plan status**: implemented / verified
- **Implementation status**: completed and verified 2026-07-17 23:42:46 CST

## 1. Objective and Success Boundary

把飞书 `ack_reaction` 从“Assistant/Copilot 完成后才出现的处理完成标记”改成低延迟、best-effort 的接收 ACK，同时保持 sender 静默规则、业务串行顺序和现有回复语义。

目标链路：

```text
Feishu SDK callback
  -> structural preflight + Assistant-owned allowlist decision
  -> business queue accepted
  -> bounded ACK lane sends Reaction
  -> existing single business worker handles outbox / Control / Copilot / reply
```

必须证明：消息 A 的 Copilot 仍被阻塞时，消息 B 的 Reaction 调用可以在 A release 前发生。

### Non-goals

本 work unit 不：

- 改 Assistant command parser、Control state machine 或 Copilot engine；
- 改 business queue 的单 worker / FIFO 语义；
- 为 Reaction 新增数据库、outbox、durable retry 或 idempotency state；
- 新增 runtime config key、外部依赖或通用 channel executor/preflight abstraction；
- 保证飞书客户端 UI 的显示时延；
- 在未经批准时调用真实飞书写接口或重启生产服务。

## 2. Fixed Contracts

### 2.1 ACK semantics

Reaction 表示：

1. payload 是当前适配器支持的 `im.message.receive_v1` 文本消息；
2. sender 和 message ID 可提取；
3. sender 通过 `src.application.assistant.policy.check_sender_allowed()`；
4. 事件已成功进入 business queue。

Reaction 不表示命令、工具、Copilot、write approval 或最终文字回复成功。

### 2.2 Security, duplicate and failure policy

- 未授权 sender 保持完全静默：无 Reaction、无 reply；现有 denial/audit 路径继续处理。
- missing sender、missing message ID、非文本和 unsupported event 不加 Reaction。
- 每次 Feishu delivery 最多提交一个**逻辑 Reaction dispatch**；该 dispatch 遇 auth failure 时允许一次 refresh + POST retry，因此底层最多两次 POST。duplicate delivery 可以再次 best-effort dispatch，不增加 durable dedupe。
- Reaction failure、timeout、rate-limit、ACK queue full、stale drop 或 shutdown drop 均不阻断 business/reply。
- business queue full 时不发送 ACK，因为“已进入处理队列”的语义不成立。

### 2.3 Concurrency and bounded delivery

- 保留现有单 business worker，新增一个私有、单线程 `_FeishuAckWorker`。
- ACK queue 容量为 `min(4, max(1, settings.queue_size))`，不新增配置；容量不含当前 in-flight job。
- 该容量只吸收健康 API 下的小 burst；外部依赖退化时依靠 queue-full 和 stale drop 限制积压，而不是增加线程。
- `submit()` 只用 `put_nowait()`，queue full 返回结构化 dropped status，不能把 `queue.Full` 抛回 SDK callback。
- ACK job 开始执行时若 `monotonic_now - received_monotonic > 3s`，记录 `stale_dropped` 并不调用网络。

### 2.4 ACK-specific network budget

Reaction 路径不沿用通用 20 秒 × 3 次 + backoff：

| Step | Timeout | Attempts | Lock wait |
|---|---:|---:|---:|
| valid cached token | 无网络 | — | 不等待 refresh lock |
| cold/refresh token fetch | 2s | 1 | non-blocking acquire |
| Reaction POST | 2s | 1 | — |
| auth failure refresh + POST retry | 各 2s | 各 1 | non-blocking acquire |

规则：

- 无指数退避；rate-limit/transient failure 单次失败即 drop。
- 仅 ACK 调用（`lock_timeout is not None`）在 token cache 有效时走 optimistic lock-free fast return，不能被另一个长 token refresh持锁阻塞；普通 caller仍走原 blocking lock语义。
- cache miss/force refresh 时，ACK 路径对 token lock 使用 `lock_timeout=0.0`；锁被占用则抛 typed transient error并 drop。
- 普通 reply/send/Bitable callers 继续使用 blocking lock 和原 20/3 defaults。
- warm healthy path只包含一次 Reaction POST；cold path最多一次 token fetch + POST；stale-token path最多失败 POST + refresh + POST。
- 这些是 request/lock budget，不宣称约束 DNS、OS scheduling 或第三方 UI 的绝对 wall-clock。

### 2.5 Latency SLO and evidence type

起点统一为 SDK callback 进入本地 coordinator 时捕获的 `time.monotonic()`。

| Signal | Target | Evidence |
|---|---:|---|
| callback → business/ACK dispatch完成 | P95 ≤ 50ms | 本地 fake-adapter timing run + dispatch log |
| callback → Reaction function invocation start | P95 ≤ 250ms | 本地 fake-adapter timing run + ACK log |
| callback → successful Reaction completion（warm token、healthy API） | P95 ≤ 2s | 生产/受控环境日志；不做真实 API 单元测试 |
| A business blocked时 B Reaction invocation | 必须在 A release 前 | deterministic Event-based test |

数值 P95 不作为易受 CI 调度影响的单次 wall-clock 单测断言。CI 必须证明非阻塞结构和 happens-before 关系；本地 fake-adapter timing run 计算至少 40 个样本的 P95 并记录结果。真实飞书 P95 只在 CEO 批准的 rollout 中验证。

## 3. Architecture and Ownership

### 3.1 Ownership retained

- `src/application/assistant/policy.py`：sender allowlist normalization 和 authoritative decision。
- `src/application/inbound/feishu.py`：Feishu payload extraction 和纯 ACK target preparation。
- `src/application/inbound/feishu_ws.py`：SDK callback coordinator、business queue、ACK queue、lifecycle 和 timing logs。
- `src/infrastructure/feishu_bot.py`：Reaction HTTP 调用及 ACK-specific budget。
- `src/infrastructure/feishu_bitable.py`：token cache、refresh lock 和 HTTP primitive。

`inbound/feishu.py -> assistant/policy.py` 符合当前 application-layer dependency；transport 不复制 allowlist parser，也不直接读取 allowlist env。

### 3.2 Pure ACK target preflight

在 `src/application/inbound/feishu.py` 增加：

```python
def prepare_feishu_ack_target(
    payload: dict[str, Any],
    *,
    allowed_senders: str | None,
) -> dict[str, Any]:
    ...
```

成功 contract：

```json
{
  "ready": true,
  "reason": "accepted_sender",
  "message_id": "om_xxx",
  "sender_decision": {"allowed": true, "reason": "matched_allowlist"}
}
```

失败 contract：

```json
{
  "ready": false,
  "reason": "permission_denied | unsupported_event | invalid_message"
}
```

实现约束：

1. 复用 `_extract_event_type()` 与 `feishu_payload_to_inbound_request()`；不新增第二套 payload parser。
2. 显式传入 `settings.allowed_senders` 并调用 `check_sender_allowed(channel="feishu", ...)`；不触发 env fallback。
3. 捕获 payload extraction 的 `AgentToolError` 并映射为 not-ready；不把 malformed event 异常抛回 SDK callback。
4. 不查 audit DB、不做 idempotency、不创建 operation、不调用工具/模型、不发送 Reaction。
5. 不在日志记录 `sender_decision.matched_entry`、sender ID 或正文。

### 3.3 SDK callback coordinator and submit ordering

`serve_feishu_ws()` 使用一个本地 closure，不新增通用 coordinator class。

若 `settings.ack_reaction` 为空：不启动 ACK worker、不运行 ACK preflight，保持 `reaction_disabled`。

启用 ACK 时固定顺序：

```text
1. capture received_monotonic
2. prepare_feishu_ack_target(payload)
   - unexpected preflight error => log + not-ready；仍尝试 business submit
3. business_worker.submit(payload, received_monotonic)
4. only if business submit accepted and target ready:
     ack_worker.submit(message_id, emoji_type, received_monotonic, event_ref)
5. emit sanitized dispatch timing/status and return
```

原因：

- 先确认 business queue accepted，避免 false ACK；
- 两次 submit 均非阻塞；
- ACK queue full 不影响 business；
- business worker 即使立即 dequeue，也不会阻塞 ACK worker；
- coordinator 必须吞掉 queue-full/preflight errors并返回，不能污染 SDK callback thread。

### 3.4 Worker lifecycle and shutdown

两个 worker 均有显式 `accepting` 状态：

- 每个 worker用自己的私有 lifecycle lock，把 `accepting` 检查与 `put_nowait()`、以及 stop transition原子化，避免 submit/stop race把 job放到 sentinel之后；
- `submit()` 在 stop 后返回 `stopped`，不抛异常；
- `serve_feishu_ws()` 的 `finally` 先关闭 coordinator intake，再停止 ACK worker，再按现有 bounded policy停止 business worker；
- ACK stop 立即 drop 并记录所有尚未开始的 queued jobs，然后插入 sentinel；不在 shutdown 继续发送旧 ACK；
- ACK worker 最多 join 5 秒；已进入网络调用的 in-flight job无法被 Python thread安全取消，超时后记录 `inflight_unfinished`，daemon thread不阻塞进程退出；
- shutdown contract 不声称 in-flight HTTP 一定被取消或一定在 5 秒内完成。

本 slice 不扩大为通用 worker lifecycle framework。

## 4. Send-once and Handler Compatibility

`handle_feishu_ws_event()` 新增内部兼容参数：

```python
handle_feishu_ws_event(..., react_in_handler: bool = True)
```

### Direct mode（默认）

- 保持当前同步 `_maybe_react()` 行为和现有 response envelope。
- 继续返回 `sent | reaction_failed | permission_denied | reaction_disabled | ...`。

### Long-running service mode

business worker调用 `react_in_handler=False`：

- `_maybe_react()` 仍先做无网络分类，保留 `not_message`、`permission_denied`、`reaction_disabled`、`missing_*` 等原因；
- 对本可发送的 allowed message不调用 Reaction，返回：

```json
{"attempted": false, "ok": true, "reason": "transport_managed"}
```

- `transport_managed` 只表示发送 ownership 在 coordinator/ACK worker，不表示 job 已发送成功；真实 scheduling/result 只在 dispatch/ACK log 中体现。
- 不跨线程等待 future，不把异步 ACK 状态写入 inbound audit、reply receipt 或 handler response。

因此 service mode 每个 delivery最多一次 attempt，同时 disabled/unauthorized/invalid 事件不会被错误标成已排队 ACK。

## 5. Observability

只使用 `time.monotonic()` 计算 duration。日志使用 `sha256(message_id or event_id)[:12]` 的 `event_ref`；不记录完整 message ID、sender、正文、credential、token 或 Feishu response body。

### Dispatch log

```text
feishu_ws_dispatch event_ref=<hash> business=accepted|queue_full|stopped \
ack=disabled|not_ready|accepted|queue_full|stopped preflight_ms=<n> callback_ms=<n>
```

### ACK final log

accepted ACK job最终产生：

```text
feishu_ws_ack event_ref=<hash> status=sent|failed|stale_dropped|shutdown_dropped \
queue_wait_ms=<n> reaction_ms=<n> total_ms=<n> error_type=<optional>
```

queue-full/stopped 在 dispatch log 已终结，不再伪造 ACK worker result。

### Business timing logs

- worker log：`queue_wait_ms/process_ms/total_ms`；
- handler stage log：`outbox_retry_ms/inbound_ms/reply_ms/total_ms`；
- service mode Reaction 不计入 handler stage；direct mode可包含 `reaction_ms`。

不修改 `build_response()` 公共 envelope，不新增 metrics backend、日志 DB 或 telemetry dependency。

## 6. Infrastructure Change Contract

### 6.1 `src/infrastructure/feishu_bot.py`

`add_message_reaction()` public inputs保持不变，但内部：

- Reaction POST调用 `http_json_fn(..., timeout=2, retry_max_attempts=1)`；
- token helper显式传 `token_timeout=2`、`token_retry_max_attempts=1`、`token_lock_timeout=0.0`；
- auth error最多 refresh一次；无 backoff。

`reply_text_message()`、`send_text_message()` 不传这些 override，现有行为不变。

### 6.2 `src/infrastructure/feishu_bitable.py`

最小向后兼容扩展：

```python
def get_tenant_access_token(
    app_id: str,
    app_secret: str,
    *,
    force_refresh: bool = False,
    timeout: int = 20,
    retry_max_attempts: int = 3,
    lock_timeout: float | None = None,
) -> str:
    ...
```

```python
def with_tenant_token_retry(
    app_id: str,
    app_secret: str,
    fn: Callable[[str], Any],
    *,
    token_timeout: int | None = None,
    token_retry_max_attempts: int | None = None,
    token_lock_timeout: float | None = None,
) -> Any:
    ...
```

约束：

1. 仅当 `lock_timeout is not None`、非 force-refresh且 cache有效时，允许在获取 `_token_lock` 前 optimistic返回；默认 `lock_timeout=None` 的普通 caller仍先获取锁。
2. `lock_timeout is None` 保持当前 blocking lock；指定值时使用 timed acquire，失败抛 `FeishuTransientError`。
3. 获取锁后继续 double-check cache，保留并发只刷新一次的语义。
4. `with_tenant_token_retry()` 的 optional budget全为 `None` 时，继续以原调用形态调用 token helper；避免改变普通 caller和现有 test double契约。
5. 只在 Reaction path传 fail-fast override；不修改 cache key、refresh threshold 或 error classification。

## 7. Files and Boundaries

| File | Planned change | Boundary |
|---|---|---|
| `src/application/inbound/feishu.py` | pure ACK target preflight | 无 I/O、无 send、无 DB |
| `src/application/inbound/feishu_ws.py` | coordinator、ACK worker、send-once、lifecycle、logs | 不改 Control/Copilot |
| `src/infrastructure/feishu_bot.py` | Reaction-specific budget | reply/send defaults不变 |
| `src/infrastructure/feishu_bitable.py` | token timeout/lock budget | existing defaults不变 |
| `tests/test_inbound_feishu_ws.py` | concurrency、queue、shutdown、compat、logs | fake adapters only |
| `tests/test_feishu_bot.py` | Reaction POST/token override | no network |
| `tests/test_feishu_bitable.py` | cache fast path、lock contention、defaults | 保留 concurrency tests |
| `tests/test_architecture_guards.py` | forbid transport-owned allowlist parser/env fallback | policy single owner |
| `docs/AGENT_INTEGRATION.md` | 说明 early best-effort ACK语义 | public behavior doc |
| `CHANGELOG.md` | user-visible latency/semantics | implementation完成时更新 |

如需要跨出此列表，implementation必须暂停并说明新的 owning boundary。

## 8. Implementation Slices

### Slice 1 — Pure preflight and policy equivalence

**Files**: `inbound/feishu.py`、`test_inbound_feishu_ws.py`、`test_architecture_guards.py`

1. 增加 `prepare_feishu_ack_target()`。
2. 测 ready、unauthorized、missing sender/message ID、non-text、unsupported、malformed JSON。
3. 测 channel-specific、wildcard allowlist结果与 `check_sender_allowed()`一致。
4. architecture guard确保 `feishu_ws.py` 不读取 allowlist env、不包含 parser、不导入 policy internals；`feishu.py` 只调用 public `check_sender_allowed()`。

**Gate**:

```bash
python3 -m pytest -q tests/test_inbound_feishu_ws.py tests/test_architecture_guards.py
```

### Slice 2 — Fail-fast Reaction HTTP and token contention

**Files**: `feishu_bot.py`、`feishu_bitable.py`、`test_feishu_bot.py`、`test_feishu_bitable.py`

1. 扩展 optional token/request/lock budget。
2. Reaction使用 POST 2/1、token 2/1、lock 0.0。
3. ACK budget调用在有效 cache且另一个线程持 token lock时仍立即返回；默认 caller仍保持 blocking语义。
4. cold/force-refresh lock contention typed-fail，不调用 HTTP。
5. 默认 caller仍使用原调用形态、20/3、blocking lock；cache concurrency仍只刷新一次。
6. auth refresh最多一次；rate-limit/transient不 sleep。
7. 更新注入的 test doubles使其捕获并断言 kwargs，不削弱 reply/send assertions。

**Gate**:

```bash
python3 -m pytest -q tests/test_feishu_bot.py tests/test_feishu_bitable.py
```

### Slice 3 — Independent ACK lane, lifecycle and send-once

**Files**: `feishu_ws.py`、`test_inbound_feishu_ws.py`

1. 增加单 `_FeishuAckWorker` 和 local coordinator。
2. ACK disabled时不启动 worker、不做 preflight。
3. business/ACK submit均返回 status、不把 queue.Full抛给 callback。
4. service handler使用 `react_in_handler=False`；direct handler默认同步兼容。
5. 实现 stale、queue-full、stop-after-submit、shutdown queued-drop、in-flight timeout状态。
6. Reaction exception不影响 business/reply。

**Required deterministic tests**:

- A business被 `threading.Event` 阻塞时，B Reaction在 release A前发生；
- ACK reaction fake被阻塞时，SDK callback和business submit仍完成；
- service mode每 delivery最多调用一次 `reaction_fn`（一个逻辑 dispatch），direct mode仍同步 `sent`；
- disabled/unauthorized/invalid分别返回正确分类且不调用 Reaction；
- business queue full不提交 ACK；ACK queue full时business继续；
- stale job和shutdown queued job不调用 Reaction；
- 并发 submit/stop不会把 job放到 sentinel之后；stop后submit返回 stopped；stop不无限等待，in-flight不可取消行为被显式测试/记录；
- unexpected preflight异常不阻断business submit。

测试用 `threading.Event`、fake clock和fake adapter，不以真实 sleep作为主要同步手段。

**Gate**:

```bash
python3 -m pytest -q tests/test_inbound_feishu_ws.py
```

### Slice 4 — Timing evidence and docs

**Files**: `feishu_ws.py`、`test_inbound_feishu_ws.py`、`docs/AGENT_INTEGRATION.md`、`CHANGELOG.md`

1. 增加 dispatch、ACK、business stage脱敏日志。
2. fake monotonic断言 duration计算和状态分类。
3. caplog断言日志不含 sender、正文、credential、完整 message ID。
4. 本地 fake-adapter timing test/harness在 ACK queue 未满的 sequential/steady-state 条件下采集至少 40 个 accepted 样本，报告 dispatch P95和invocation-start P95；该结果记录在实现 handoff，不调用真实飞书。
5. 文档明确 ACK 只表示 allowlisted event已进入business queue；changelog记录行为变化。

**Gate**:

```bash
python3 -m pytest -q \
  tests/test_inbound_feishu_ws.py \
  tests/test_inbound_control.py -k 'duplicate_message_from_other_sender or denies_unknown_remote_sender'
```

## 9. Acceptance Matrix

| Scenario | Reaction | Business | Reply | Evidence |
|---|---|---|---|---|
| allowed + idle | fast attempt | normal | normal | dispatch/ACK log + test |
| allowed + A Copilot blocked | independent attempt | B queued | later | blocked A/B test |
| ACK disabled | none | normal | normal | no-worker test |
| unauthorized | none | denial/audit | silent | equivalence + denial test |
| unsupported/non-text/malformed | none | existing path | existing semantics | preflight tests |
| business queue full | none | rejected/logged | none | queue-full test |
| ACK queue full | dropped/logged | normal | normal | ACK-full test |
| token lock contended | failed fast | normal | normal | lock-contention test |
| Reaction timeout/rate-limit | failed once/no sleep | normal | normal | failure isolation test |
| stale ACK | dropped | normal | normal | fake-clock test |
| duplicate delivery | ≤1 logical dispatch per delivery；auth refresh可有第2次POST | existing idempotency | existing semantics | send-once test |
| direct handler | synchronous existing behavior | normal | normal | compatibility test |
| shutdown queued | dropped, no late queued send | existing bounded stop | existing behavior | shutdown test |
| shutdown in-flight | best-effort join; may finish | existing bounded stop | existing behavior | lifecycle test + residual risk |

## 10. Validation Commands

Focused suite：

```bash
python3 -m pytest -q \
  tests/test_inbound_feishu_ws.py \
  tests/test_feishu_bot.py \
  tests/test_feishu_bitable.py \
  tests/test_inbound_control.py \
  tests/test_architecture_guards.py
```

Agent/channel regression：

```bash
python3 -m pytest -q \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py \
  tests/test_effective_settings.py \
  tests/test_config_yaml.py
```

Static checks：

```bash
git diff --check
! rg -n '_parse_allowed_entries' \
  src/application/inbound/feishu.py \
  src/application/inbound/feishu_ws.py
! rg -n 'OM_FEISHU_BOT_ALLOWED_OPEN_IDS|OM_FEISHU_BOT_USER_OPEN_ID' \
  src/application/inbound/feishu.py
```

预期：两个 transport文件均不复制 parser；ACK preflight所在的 `feishu.py` 不读取 allowlist env，只通过 public policy API消费显式 `allowed_senders`。`feishu_ws.py` 可保留 settings构建和配置错误提示中的既有 env名称。

## 11. Rollout, Safety and Rollback

本地 implementation/validation不得：

- 调用真实 Feishu Reaction/reply API；
- 修改生产 config、secret或 resolved runtime config；
- 安装/重启 service；
- 写 production inbound audit DB。

代码合并后的真实验证需要 CEO 另行批准：

1. 部署但不修改现有 `ack_reaction` 配置；
2. 经批准重启 Feishu WS service；
3. 先验证一条消息的 ACK/reply和日志脱敏；
4. 收集足够健康样本后计算 dispatch/invocation/completion P95；
5. 检查 queue-full、stale、lock-contention、inflight-unfinished；
6. 只有单 ACK worker在健康依赖下仍不满足 SLO，才另开 work unit评估固定小并发。

Rollback：回退本次代码并重启 Feishu WS service；没有 schema/config migration或持久 ACK state需要清理。

## 12. Findings Resolution Matrix

| Review finding | Resolution |
|---|---|
| PR-01 同 worker队头阻塞 | 当前 slice使用独立 ACK worker；blocked A/B为硬验收 |
| PR-02 fast preflight policy ownership | payload extraction在 Feishu adapter；allowlist委托 public `check_sender_allowed()`；guard防复制/env fallback |
| PR-03 通用重试阻塞 ACK lane | POST/token 2/1、无 backoff；有效 cache lock-free；ACK token lock non-blocking；stale/queue drop |
| PR-04 send-once与同步契约 | direct默认同步；service由 ACK worker唯一发送；handler只返回 `transport_managed`，无跨线程状态汇合 |
| PR-05 SLO与失败测试缺失 | 明确 monotonic起点、数值目标、deterministic concurrency tests、fake timing run和完整退化矩阵 |

## 13. Residual Risks

| Risk | Handling / tracking |
|---|---|
| Feishu API成功但客户端 UI晚显示 | 用 API completion log区分；不在本 slice猜测 UI |
| DNS/OS scheduling超出 request timeout | ACK best-effort；记录 total/error type；不宣称硬 wall-clock |
| in-flight HTTP在 shutdown join后仍运行 | queued job先drop；记录 `inflight_unfinished`；daemon不阻塞退出；无法安全强杀 Python thread |
| 单 ACK worker在外部故障时丢 ACK | 小队列 + fail-fast + stale drop；业务不受影响 |
| cold/stale token degraded path可达约4-6s | 只约束 warm healthy completion SLO；后续 job会 stale/drop，避免长积压 |
| ACK optimistic cache read撞上并发 force-refresh | 可能先用旧 token并在 refresh lock争用时drop；这是best-effort ACK可接受退化，普通 caller不使用该fast path |
| duplicate Reaction被飞书判 already-exists | 记录 failed/permanent；不为装饰性 ACK加持久状态机 |
| allowlist policy未来变化 | 始终调用 public policy API；architecture guard跟踪 |
| timing wall-clock test受 CI噪声 | CI验证因果关系；P95用本地/生产日志验收 |

## 14. Definition of Done

仅当以下全部满足，implementation 才可声明完成：

- PR-01 至 PR-05 的 resolution evidence全部通过；
- blocked A/B证明 ACK lane与 business lane解耦；
- service mode每 delivery只调用一次 `reaction_fn`，direct handler无兼容回归；
- disabled、unauthorized、invalid、queue-full、stale、token-lock-contention、shutdown状态均符合 matrix；
- Reaction失败/超时不影响 business/reply；
- reply/send/Bitable token defaults和cache concurrency无回归；
- 日志脱敏并能区分 dispatch、ACK queue/network和business queue/stage耗时；
- 完成至少40样本的 fake-adapter P95记录；
- 未新增配置键、数据库表、依赖或通用 channel abstraction；
- focused + regression + static checks全部通过；
- docs/changelog已更新；
- 未执行真实飞书写操作或生产服务重启。


## 15. Execution Evidence

Implementation completed without production config changes, service restart, or successful real Feishu write.

### Tests

```text
Focused Feishu/inbound/architecture suite: 148 passed in 3.26s
Agent/channel regression suite: 147 passed in 3.40s
Feishu WS concurrency suite repeated 5 times: 33 passed on every run
Ruff / py_compile / git diff --check / architecture rg guards: passed
```

### Controlled fake-adapter latency

40 sequential/steady-state accepted samples, fake business handler and fake Reaction adapter:

```json
{
  "samples": 40,
  "dispatch_p95_ms": 0.089,
  "dispatch_max_ms": 0.199,
  "invocation_start_p95_ms": 0.114,
  "invocation_start_max_ms": 0.215
}
```

This validates local dispatch and ACK-lane scheduling only. Warm healthy real-Feishu completion P95 remains a separately approved rollout observation.
