# OM Runtime and Data Quality

OM 自己维护运行状态、账本、成交摄取、持仓对账和生命周期质量检查。
检查结果写入本地 artifact，CLI、Tool Gateway 和业务门禁读取同一份证据。

- [检查实现与回归证据](om-check-implementation.md)
- [操作契约](om-operator.md)
- [本地质量文件契约](../../contracts/quality-monitoring/README.md)

HTTP 质量接口与外部质量 Hub 接入已退役。本地质量刷新、到期复查、日终对账和
`OM_QUALITY_ONBOARDED` 门禁继续保留。
