# Gateflow Implementation Artifact — S1 PM opt-in config and strict wire adapter

- Gate: `implementation`
- Work unit: `ai-decision-advice-drift-remediation`
- Slice: `S1`
- Plan: `docs/gateflow/ai-decision-advice-drift-remediation/plan-20260809.md`
- Status: `accepted after fix and re-review; ready for checkpoint commit`

## Outcome

AI Decision Advice 现在有明确且默认关闭的 PM 组合分布 provider 配置边界，PM client
能够构造固定的单账户 distribution 请求并对回包做第一层 wire validation。
本切片未引入 PM 网络调用，也未改变 Candidate Engine 的筛选、排序或资金口径。

## Changed files and ownership

- `src/application/ai_decision_advice/config.py`
  - 新增 `none|portfolio_management` provider 常量和 fail-closed resolver。
  - 缺失、非 object 或非法 provider 均解析为 `none`。
- `src/application/config_validator.py`
  - 收紧顶层 `ai_decision_advice` 和嵌套 `portfolio_distribution` 的 object 类型校验。
  - 只允许 `provider` 字段和两个固定值；falsey 非 object 不再被吞掉。
- `src/application/config_yaml.py`
  - 明确 YAML 窄配置由操作者 opt in，保持原有 root/market passthrough。
- `configs/examples/config.yaml.example`
  - 增加默认 `provider: none` 示例。
- `src/infrastructure/portfolio_management_client.py`
  - 新增单账户 `read_distribution()`，固定
    `by_asset=true&include_value=true&group_cash=false`。
  - 请求前拒绝空账户、`all` 和多账户 scope；回包校验 API version、
    success、唯一账户、freshness 时间和基础结构。
  - 记录当前 v1 consumer valuation currency 为 CNY，不把宽松 OpenAPI schema
    误当成行级单位证明。
- `tests/test_ai_decision_advice_config.py`
- `tests/test_portfolio_management_client.py`
- `tests/test_portfolio_management_contract_vendor.py`
  - 覆盖 provider、YAML round-trip、严格 query、账户绑定、freshness 和 vendored
    schema 残留边界。

## Decisions and invariants

1. Provider 缺失时固定为 `none`，不做环境或服务自动探测。
2. OM account 是单账户请求的权威 scope；这一层不允许 `all` 或逗号集合。
3. Loopback-only URL 和 API version 验证继续复用已有 client 边界。
4. S1 只校验 wire envelope；行级资产字段、有限数值、账户隔离和派生权重属于 S2。
5. PM 是 Advice soft dependency；本切片没有把 PM 加入 Candidate Engine gate。

## Validation

- `python3 -m pytest -q tests/test_ai_decision_advice_config.py tests/test_config_yaml.py tests/test_portfolio_management_client.py tests/test_portfolio_management_contract_vendor.py`
  - `116 passed`
- `python3 -m py_compile` on all changed Python source files: passed.
- `./om config validate --source yaml --market us ...`: passed.
- `./om config validate --source yaml --market hk ...`: passed.
- `./om config build --source yaml --market us ... --dry-run`: passed; no write applied.
- `./om config build --source yaml --market hk ... --dry-run`: passed; no write applied.
- `git diff --check`: passed.

The accepted plan names `tests/test_layered_config.py`, which does not exist in the current tree;
the actual YAML adapter coverage lives in `tests/test_config_yaml.py` and was included. The repo
`.venv` currently has no `pytest` module, so the focused suite ran with the available system
Python 3 pytest environment. No dependency was installed or production state changed.

## Residual risks / next gate

- Vendored PM OpenAPI still does not formally declare the row-level CNY unit; this remains the
  accepted cross-repository contract risk and is not widened in S1.
- Row normalization and prepared-envelope publication are intentionally deferred to S2.
- Next gate: DeepReview of the complete S1 diff, followed by fix/re-review before checkpoint commit.
