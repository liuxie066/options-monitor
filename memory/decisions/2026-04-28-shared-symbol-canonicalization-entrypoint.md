## Context

最近几轮排障确认，`close_advice` 的 OpenD 补拉失败并不是行情源无价，而是不同入口对 symbol / underlier 的归一化口径不一致。

典型现象是：

- 手工按合约代码或正确 underlying 查询 OpenD 有价
- 业务链路里却因为 `POP` 这类别名没有及时 canonicalize，继续沿用 alias 或错误 market 口径，最终落成 `opend_fetch_no_usable_quote`

## Decision

1. 仓库内需要处理用户输入、broker 原始 payload、或 OpenD/Futu underlier 的入口，统一复用 `scripts/opend_utils.py:resolve_underlier_alias`。
2. canonical symbol 以业务 symbol 为准，例如 `NVDA`、`0700.HK`、`9992.HK`。
3. alias（例如 `POP`）允许作为输入，但不能作为 watchlist、持仓、multiplier cache、或后续业务链路中的持久 symbol 继续传播。
4. 纯下游消费层如果只做展示、聚合、比较，可以继续假设上游已提供 canonical symbol；不要求机械替换所有 `upper()`。

## Rationale

- 高风险错误都发生在“入口未 canonicalize”，而不是“下游消费层大小写标准化不够”。
- 统一入口后，watchlist、持仓、trade normalize、Futu portfolio、OpenD 补拉和 multiplier refresh 都能共享同一 alias 解析规则。
- 不对所有 `upper()` 做大扫除，可以避免低 ROI 的大面积回归风险。

## Operational Guidance

- 新入口先判断自己是否属于“输入边界”：
  - 用户手输 symbol
  - broker/Futu/OpenD 回传 underlying
  - 配置写入
  - 需要据 symbol 决定 market / underlier 查询
- 如果属于以上边界，必须先 canonicalize，再进入业务逻辑。
- 如果只是消费 canonical symbol 做比较或显示，可以继续保持轻量实现。
