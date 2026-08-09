# Gateflow Slice Implementation — S2 观察集合与身份快照

- Gate: `implementation`（slice S2）
- Work unit: `ai-decision-advice`
- Plan: `docs/gateflow/ai-decision-advice/plan-20260809.md` S2

## Changed files

- `src/application/ai_decision_advice/identity.py`（新增）：
  - `build_observation_set`：配置扫描标的 + 普通股票持仓 + 开放期权底层 +
    最近候选的 canonical 去重并集，来源优先级（open_option <
    recent_candidate < stock_holding < scan_config）与多来源合并；
  - 提取器：`open_option_underlyings_from_lots`（ledger lot 字段）、
    `stock_symbols_from_portfolio_context`（冻结组合上下文）、
    `candidate_symbols_from_snapshot`（封存候选快照）；
  - `build_symbol_identity_snapshot`：优先注入的 market-snapshot 名称、缺失时
    注入的 basicinfo（分批 200、只接受请求集合内的行），否则
    `identity_unavailable`；payload schema
    `ai_decision_advice.symbol_identity_snapshot.v1` + 内容 hash；
  - `publish/load_symbol_identity_snapshot`：原子写整份快照到
    `output_shared/state/ai_decision_advice/symbol_identity_snapshot.json`，
    同内容重写确定性；
  - `RefreshQueue`：优先级 + 最久未尝试排序 + 未完成标的回队首（饥饿保护）。
- `tests/test_ai_decision_advice_identity.py`（新增，14 例）。

## 设计说明

- OpenD 交互以注入 provider（`market_snapshot_provider` /
  `basic_info_provider`）隔离；本 slice 不直接依赖 futu gateway，真实接线在
  S3 collector 运行入口完成；
- 身份解析只接受 canonical 后仍属于观察集合的行，防止 OpenD 代码形态差异
  （`US.NVDA` vs `NVDA`）或多余返回行污染身份事实。

## Validation

- `python3.12 -m pytest tests/test_ai_decision_advice_identity.py -q`
  → 14 passed。

## Residual risks

- 真实 OpenD market snapshot 行格式（`name`/`exchange_type` 键名）在 S3 接线时
  以 adapter 负责映射——covered by S3；
- 快照与 evidence 记录的 hash 绑定——covered by S3。

## Completion status

Complete；进入 code review gate。
