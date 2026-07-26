# 运行与数据质量监控 — API 契约

- **状态**：已确认设计，待实施
- **日期**：2026-07-26
- **状态 Schema**：[quality_status.v1.schema.json](quality_status.v1.schema.json)
- **总设计**：[architecture.md](architecture.md)
- **检查矩阵**：[check-matrix.md](check-matrix.md)
- **实施计划**：[implementation-plan.md](implementation-plan.md)

## 1. 契约边界

本契约定义：

- OM/PM producer 向 Quality Hub 暴露的只读接口；
- Quality Hub 的统一只读接口；
- Hub incident acknowledge 和 maintenance window 控制接口；
- HTTP、鉴权、错误、幂等和脱敏语义。

本契约不提供：

- 业务数据写入；
- OpenD 查询代理；
- PM 同步触发；
- OM ledger/trade/lifecycle 修复；
- 服务重启或配置修改；
- 完整 evidence 文件下载。

## 2. 版本

状态 payload 的规范名：

```text
investment.quality_status.v1
```

版本存在于响应 body：

```json
{
  "schema_version": "investment.quality_status.v1"
}
```

规则：

- V1 只新增可选字段；
- 修改枚举、删除字段或改变语义时发布 V2；
- Hub 支持当前版本和前一个兼容版本；
- 未知版本不得猜测解析；
- endpoint 路径在 V1 内保持稳定。

## 3. 通用 HTTP 规则

### 3.1 传输

- producer 接口仅绑定 loopback 或受控内网；
- Hub 对未来控制台的接口由控制台身份层保护；
- 生产强制 HTTPS 或可信 loopback；
- 响应使用 UTF-8 JSON；
- 响应头包含：

```text
Content-Type: application/json; charset=utf-8
Cache-Control: no-store
X-Request-Id: <opaque-id>
X-Quality-Schema-Version: investment.quality_status.v1
```

### 3.2 鉴权

Producer：

```text
Authorization: Bearer <service-specific-read-token>
```

- OM、PM token 不得相同；
- token 只有 `quality:read` 权限；
- token 不进入 URL、日志、状态文件或 evidence。

Hub operator：

- `quality:read`：统一状态和 incident；
- `quality:acknowledge`：ack incident；
- `quality:maintenance`：维护窗口。

actor 从认证身份取得，不能信任请求 body 自报 actor。

### 3.3 时间

- 所有时间使用 UTC RFC 3339；
- 字段名以 `_utc` 结尾；
- producer 的 `observed_at_utc` 表示质量事实的截止时间，不是 HTTP 返回时间；
- Hub 必须独立检查 freshness；
- producer/server clock 明显漂移时输出运行检查，不由 Hub静默修正。

### 3.4 HTTP 状态与质量状态分离

`GET /quality/status` 成功读取质量事实时返回 HTTP 200，即使业务状态为：

```text
unhealthy
untrusted
unavailable
```

HTTP 503 只表示接口自身无法形成有效响应，不能用来表达“某个业务数据集不可信”。

### 3.5 通用错误

```json
{
  "error": {
    "code": "QUALITY_AUTH_FAILED",
    "message": "quality endpoint authentication failed",
    "request_id": "req-opaque"
  }
}
```

错误 envelope：

- 不包含 token、完整账户 ID、金额、持仓或内部文件路径；
- `code` 是稳定机器码；
- `message` 是安全的人类摘要；
- 详细堆栈只进入受限本地日志。

通用状态码：

| HTTP | 含义 |
|---|---|
| 200 | 请求成功，质量结论在 body |
| 304 | ETag 未变化；仅允许有未过期缓存的 Hub 使用 |
| 400 | 请求格式无效 |
| 401 | 缺失或无效 token |
| 403 | token scope 不足 |
| 404 | 资源不存在 |
| 409 | incident/window 状态冲突 |
| 422 | payload 语义验证失败 |
| 429 | 受控接口频率限制 |
| 500 | 未分类内部错误 |
| 503 | 接口自身无法读取/构建状态 |

## 4. Producer API

OM 和 PM 必须实现相同的公共接口。

### 4.1 `GET /quality/status`

返回当前完整服务质量状态。

请求：

```http
GET /quality/status HTTP/1.1
Authorization: Bearer <quality-read-token>
Accept: application/json
```

成功：

```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Cache-Control: no-store
ETag: "sha256:<payload-sha256>"
X-Quality-Schema-Version: investment.quality_status.v1
```

body 必须通过 [quality_status.v1.schema.json](quality_status.v1.schema.json)。

语义：

- 只执行有界本地读取；
- 不因 HTTP 请求触发 OpenD 强制刷新；
- 不写 latest/history/evidence；
- 不触发 PM 同步；
- 不触发 OM replay repair；
- 返回最近一次已发布状态及其真实 `observed_at_utc`；
- 如果本地状态不存在或不可读，返回 503 安全错误；
- ETag 基于规范化响应内容，不包含 token/request ID。

Hub 使用 `If-None-Match` 时：

- 304 只表示内容未变化；
- Hub 仍必须使用 cached `observed_at_utc` 判断 stale；
- cached status 已过期时不得因 304 保持 available。

### 4.2 `GET /health`

Producer 现有 `/health` 可以保留，但只表达接口/进程健康：

```json
{
  "status": "ok",
  "service": "portfolio-management"
}
```

禁止从 `/health=ok` 推导业务数据可信。Hub 的业务入口始终是 `/quality/status`。

### 4.3 本地 CLI 等价接口

OM：

```text
./om quality status --json
./om-agent run --tool quality_status --input-json ...
```

PM：

```text
pm quality status --json
```

CLI 与 HTTP 必须调用同一 application service 并返回相同 payload 语义。CLI 不绕过 Schema、scope、证据或门禁策略。

## 5. Quality Hub API

### 5.1 `GET /health`

只表达 Hub 自身运行：

```json
{
  "service": "investment-quality",
  "status": "ok",
  "database": "ok",
  "scheduler": "ok",
  "outbox": {
    "status": "healthy",
    "pending_count": 0
  },
  "observed_at_utc": "2026-07-26T06:00:00Z"
}
```

如果 outbox unhealthy，可以返回 HTTP 200 + `status=degraded`；只有 Hub 无法构建 health 时返回 503。

### 5.2 `GET /quality/status`

返回 Hub 聚合后的 `investment.quality_status.v1`：

- `producer.service=investment-quality`；
- `components` 描述 OM/PM onboarded/available/incompatible；
- `datasets` 保留原服务 dataset ID 和 scope；
- Hub 追加 dependency propagation 结果；
- Hub 不修改 producer 原始 evidence/reason 语义；
- 同一账户不同服务状态不得错误合并。

支持可选过滤：

| Query | 格式 | 语义 |
|---|---|---|
| `service` | `options-monitor` / `portfolio-management` | 仅投影指定 producer |
| `account` | lowercase label | 仅投影指定账户 |
| `market` | lowercase market | 仅投影指定市场 |
| `dataset_id` | exact ID | 仅投影指定数据集 |

过滤只改变返回投影，不改变 incident、门禁或存储状态。

### 5.3 `GET /quality/incidents`

响应：

```json
{
  "items": [],
  "next_cursor": null,
  "observed_at_utc": "2026-07-26T06:00:00Z"
}
```

支持：

- `state=new|persistent|acknowledged|recovered`；
- `severity=info|warning|blocking`；
- `service`；
- `account`；
- `dataset_id`；
- opaque cursor。

默认：

- 不返回完整金额；
- evidence 只返回 opaque reference；
- recovered 记录按保留策略可查询。

### 5.4 `POST /quality/incidents/{incident_id}/acknowledge`

权限：`quality:acknowledge`。

请求：

```http
POST /quality/incidents/inc-opaque/acknowledge
Authorization: Bearer <operator-token>
Idempotency-Key: <caller-stable-key>
Content-Type: application/json
```

```json
{
  "comment": "已开始调查 PM fund_mmf 写入失败"
}
```

成功：

```json
{
  "incident_id": "inc-opaque",
  "state": "acknowledged",
  "acknowledged_at_utc": "2026-07-26T06:00:00Z",
  "actor": "authenticated-operator",
  "comment": "已开始调查 PM fund_mmf 写入失败"
}
```

约束：

- acknowledge 不改变 severity、dataset status、gate 或 blocked consumers；
- recovered incident 再 acknowledge 返回 409；
- 相同 Idempotency-Key + 相同 payload 返回原结果；
- 相同 key + 不同 payload 返回 409；
- actor 来自认证身份。

### 5.5 `POST /quality/maintenance-windows`

权限：`quality:maintenance`。

请求：

```json
{
  "scope": {
    "service": "portfolio-management",
    "account": "sy"
  },
  "starts_at_utc": "2026-07-26T07:00:00Z",
  "ends_at_utc": "2026-07-26T08:00:00Z",
  "reason": "approved PM service maintenance"
}
```

规则：

- 必须带 Idempotency-Key；
- actor 从认证身份取得；
- end 必须晚于 start；
- 禁止无结束时间；
- 仅抑制 scope 内重复通知；
- 状态、incident、门禁继续真实计算；
- scope 外 blocking 继续告警；
- 到期自动失效。

### 5.6 `DELETE /quality/maintenance-windows/{window_id}`

权限：`quality:maintenance`。

提前结束维护窗口。操作写 audit，不修改既有 incident 状态。

## 6. Producer 拉取契约

Hub 对每个 producer 配置：

```text
service
base_url
read_token
onboarded
bounded_timeout
expected_schema_versions
freshness_policy
```

拉取状态：

| 结果 | Component status | 数据处理 |
|---|---|---|
| 尚未接入 | `not_onboarded` | 不生成故障告警 |
| 200 + valid schema | `available` | 参与依赖和 incident |
| 304 + fresh cache | `available` | 使用缓存但保留原 as_of |
| auth failure | `unavailable` | fail closed + security incident |
| timeout/connection | `unavailable` | fail closed |
| unknown schema | `incompatible` | 不猜测解析 |
| valid body but stale | `unavailable` | 可展示旧值，正式消费者阻断 |

Hub 不因拉取失败触发 producer 重启、OpenD 查询或业务同步。

## 7. Incident 与通知契约

Incident fingerprint 的规范输入：

```text
producer.service
subject_id
normalized scope
reason_code
```

不得包含：

- 当前时间；
- request ID；
- 易变错误文本；
- 完整 broker account ID；
- 金额。

状态：

```text
new -> persistent -> acknowledged -> recovered
```

通知：

- new blocking：立即 outbox；
- persistent：按 2 小时/每日最多 3 次；
- acknowledged：暂停重复提醒；
- reason/scope/severity 变化：新 transition；
- recovered：立即；
- 被新状态取代的旧 outbox item 标记 superseded。

## 8. 脱敏与证据

公共 API 可以返回：

- account label；
- account fingerprint/mask；
- dataset/check/reason；
- 差异数量；
- as_of/expected_by；
- opaque evidence ID；
- blocked consumers。

公共 API 默认不返回：

- 完整 broker account ID；
- token/webhook；
- OpenD 原始响应；
- 完整业务数据库路径；
- 非必要持仓或金额明细；
- Python traceback。

V1 不提供 evidence download API。完整 evidence 通过服务器本地受控 CLI 和文件权限调查。

## 9. 契约测试

三个仓库必须共享以下契约用例：

1. producer status HTTP 200 + valid V1；
2. unhealthy/untrusted 仍返回 HTTP 200；
3. unknown top-level field 拒绝；
4. unknown schema 标记 incompatible；
5. 304 + stale cached status 不能保持 available；
6. token 缺失/错误；
7. producer timeout 不触发副作用；
8. acknowledge idempotency/conflict/recovered guard；
9. maintenance window 有界且不改变门禁；
10. 响应和错误不泄露 broker ID、token、金额或内部路径。
