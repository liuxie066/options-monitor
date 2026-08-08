# Slice 3 Implementation — CC+LP sealed snapshot 与 pipeline 封存

- Work unit: cc-lp-same-expiry
- Slice: 3 (CC+LP sealed snapshot 与 pipeline 封存)
- Date: 2026-08-08
- Status: 完成（待 code review）

## Changed Files

- `src/application/cc_lp_candidate_snapshot.py`（新增）：
  - schema `cc_lp_candidate_snapshot.v1`、file `cc_lp_candidate_snapshot.json`；
  - `seal_cc_lp_candidate_snapshot` / `load_cc_lp_candidate_snapshot` / `validate_cc_lp_candidate_snapshot`；
  - 复用 combo snapshot 模式（content_sha256、immutable write、status 枚举）。
- `src/application/pipeline_watchlist.py`：封存 combo snapshot 后，过滤 `captured_combo_pairs` 中 `variant=="cc_lp"` 候选并封存 CC+LP snapshot；
- `src/application/cc_lp_steps.py`：row 增加 `candidate_pair_id`（`cc_lp:{symbol}:{call}:{put}`）；
- `tests/test_cc_lp_candidate_snapshot.py`（新增）：5 个测试。

## Validation

- `pytest tests/test_cc_lp_candidate_snapshot.py tests/test_cc_lp_steps.py tests/test_cc_lp_domain.py`：21 passed
- 回归：`test_combo_yield_candidate_snapshot.py` + `test_combo_yield_steps.py` + `test_combo_yield_pairing.py` + `test_pipeline_watchlist_*.py`：41 passed
- `ruff check`：All checks passed

## Key Decisions

- CC+LP 用独立 `cc_lp_candidate_snapshot.v1`（不混 combo schema）；
- 从 `captured_combo_pairs` 按 `variant=="cc_lp"` 过滤（combo_pairs_sink 已收所有 combo 变体）；
- status 派生：有候选 → candidates_found；无候选且 combo status 含 not_applicable → not_applicable；否则 no_candidate；
- Daily Brief 消费 CC+LP snapshot 留待后续 work unit（plan 非目标：不改通知格式）。

## Findings / Residual Risks

- 无 accepted findings；Daily Brief 展示 CC+LP 是后续 work unit（已记录 deferred）。
