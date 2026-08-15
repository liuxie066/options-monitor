# Code Review

## Scope

- Mode: current changes
- Branch or PR: `feat/sell-put-top1-w0r-history-quota`
- Base: `origin/main@0da901b30cd26242636b9ec967b8aa281f61937c`
- Output file: `docs/reviews/aggregate-review-20260815-174231.md`
- Included scope: 完整 work unit（`0fc8bd6d` accepted plan + `e107db50` 实现提交）——goal-confirmation、plan/plan-fix、implementation-s1、`FutuGateway.get_history_kl_quota()` + `_normalize_history_kline_quota_response()`、`OpenDFetchLimits.history_kline` 与 alias/validator 联动、focused tests、capability preflight 更新、dependency graph 机械再生
- Excluded scope: live OpenD probe、W5 runner、quota sufficiency policy、receipt persistence（plan §8 已分配给后续 work unit，不在本 slice）
- Parallel review coverage: 无；aggregate 范围窄，由主 reviewer 沿 `get_history_kl_quota -> _normalize_history_kline_quota_response -> _raise_mapped/_map_error -> FutuGatewayError` 与 `runtime.opend_rate_limits.history_kline -> _raw_opend_endpoint_rate_limit -> resolve_opend_fetch_limits -> as_config/config_validator` 两条真实链路完整走读，并对照已安装 SDK（futu `open_quote_context.py:1883`、`quote_query.py:2184-2222`、`Qot_RequestHistoryKLQuota.proto`）核对协议形状；focused tests（58 passed）在本 worktree 复跑确认

## Findings

未发现实质性问题。

走读记录（支撑无 finding 结论的直接证据）：

- Goal/plan 符合性：plan §4-§6 列出的允许改动与实际 diff 一一对应；无 caller、receipt schema、CLI、Agent tool、timer、生产 config 或 OpenD 调用，符合 §2 non-goals。`git status` 干净，HEAD 为 `e107db50`，范围与 base 一致。
- SDK 协议边界：已安装 SDK 成功路径恒返回 `(RET_OK, (used_quota, remain_quota, detail_list))`（`open_quote_context.py:1883-1901`），`unpack_rsp` 保证 `detail_list` 为 list 且每项含 `code/name/request_time`（`quote_query.py:2202-2222`）；`usedQuota/remainQuota` proto 为 `required int32`（`Qot_RequestHistoryKLQuota.proto:25-26`），与实现的非 bool `Integral` 校验一致。SDK 正常返回无 exception 路径；异常路径统一经 `_raise_mapped(action="get_history_kl_quota")` -> `_map_error`（`futu_gateway.py:612-616, 583-602`），与既有 16 个 gateway 方法的 error boundary 模式相同。
- Strict normalization：ret bool/非整数、`ret != 0`（`RuntimeError(payload)` 携带 provider 错误串）、payload 非三元组、count bool/非整数/负数、detail 非 list/非 dict、code 空或规范化后重复、`request_time` 非 canonical `%Y-%m-%d %H:%M:%S`（strptime+strftime round-trip 拒绝 `2026-02-30` 与非零填充）、detail count != used_quota，全部 raise 并映射为 `FutuGatewayError`，无 partial/default fact 出口。`requestTime` proto 为 `required string`（proto:13），SDK 已 `str()` 化，字符串假设与协议一致。
- 不拒绝合法事实 / 不接受畸形事实：failure 矩阵 14 个 case 覆盖上述全部拒绝分支（`test_futu_gateway_minimal.py:496-537`）；success case 证明 `get_detail=True` 硬编码、name 剥离、code trim+upper、按 `(code, request_time)` 排序（`test_futu_gateway_minimal.py:418-475`）。plan §5 rule 5 授权的 duplicate-code 拒绝与 count/detail 一致性检查均在 fail-closed 方向。
- Rate-limit 配置有效性链：`history_kline` canonical key 经 `_raw_opend_endpoint_rate_limit`（`opend_fetch_config.py:265-273`）解析，默认 `60/30s/30s wait`；override round-trip 有测试（`test_opend_batch_config.py:92-110`）；`OPEND_RATE_LIMIT_ENDPOINT_KEYS` 自动纳入新 key，`config_validator.py:986-992` 无需改动即接受，acceptance fixture 已扩展并复跑通过。`fetch_kwargs()`/`discovery_kwargs()`/`OPEND_FETCH_KWARG_KEYS` 未变；`from_flat_kwargs()` 的四个既有 caller（`opend_symbol_chain_fetching.py:126`、`opend_symbol_fetching.py:381`、`opend_market_snapshot_fetching.py:106,175`）不传 history 参数走 defaults，candidate fetch/discovery 行为零变化。
- 文档真实性：preflight 只把该窄缝标为 `SDK/project source green；live unknown`，W0R overall 保持 `runtime_no_go`（`sell-put-top1-capability-preflight-20260814.md:177, 420-439`），无 live readiness 夸大；implementation-s1 明确记录全量 9 个失败均为环境原因及复验路径。
- Dependency graph：diff 为既有 generator 对 origin/main stale 状态的机械再生（plan §4 已声明该义务），数值自述自洽（939 files / 590 production modules / 0 cycles），与本 slice 新增的零 import 一致。
- 过度设计检查：未新增 provider registry、factory、schema、state machine 或通用抽象；`history_kline` alias 仅含 canonical key（plan §6 明确不加投机 alias），符合 PONYTAIL 最小实现与 W0R source-only 边界。

## Open Questions

- 无。

## Residual Risk

- Live provider 的 `request_time` 实际格式（是否恒为 canonical `%Y-%m-%d %H:%M:%S`、时区语义）未由真实 receipt 证明；若非 canonical，严格校验 fail closed——这是 plan 明确选择的行为，实际形状归属已授权的 W0R live probe。
- Duplicate-code 拒绝假设 live 不会返回同一标的多行明细；若真实返回同 code 多行，gateway fail closed。该语义由 plan §5 rule 5 授权，live 证据待 probe。
- Test gap（非阻断）：failure 矩阵未单独断言 `code` 含首尾空白（当前 trim 后接受）与 SDK 升级后 detail 形状漂移的兼容行为；前者符合 plan 的 trim-normalize 规则，后者由 fail-closed 兜底。
- CI gap：验证均为本地执行，PR CI 未观察。
