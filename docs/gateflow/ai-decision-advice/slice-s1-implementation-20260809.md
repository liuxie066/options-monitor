# Gateflow Slice Implementation — S1 配置合同 + DeepSeek Responses adapter

- Gate: `implementation`（slice S1）
- Work unit: `ai-decision-advice`
- Plan: `docs/gateflow/ai-decision-advice/plan-20260809.md` S1

## Changed files

- `src/infrastructure/deepseek_responses.py`（新增）：
  `create_deepseek_response`（web_search 工具声明、json_schema 结构化输出、
  注入式 HTTP 层、超时、错误类型 `DeepSeekResponsesError`）、
  `resolve_deepseek_responses_url`、`extract_output_text`、`extract_usage`、
  `extract_web_search_calls`；不复用 Copilot 的 OpenAI client；
- `src/application/ai_decision_advice/__init__.py`（新增包）；
- `src/application/ai_decision_advice/config.py`（新增）：v1 固定常量
  （provider/model/API key env、4h/24h/8h/5min/批 5/并发 2/30s 预算、状态文件
  名）、`ai_decision_advice_enabled`、`resolve_api_key`；
- `src/application/config_validator.py`：
  `AI_DECISION_ADVICE_CONFIG_KEYS` / `RETIRED_AI_ADVICE_CONFIG_KEYS`、
  `_validate_ai_decision_advice_config`（unknown keys、enabled bool、
  `enabled: true` 时 `DEEPSEEK_API_KEY` 环境检查）、
  `validate_config` 接入 retired-key 拒绝与段校验；
- `src/application/config_yaml.py`：`PASSTHROUGH_KEYS` 增加
  `ai_decision_advice`（root 与 market 层均允许）；
- `configs/examples/config.yaml.example`：`ai_decision_advice.enabled: false`
  示例；
- `tests/test_ai_decision_advice_config.py`（新增，8 例）；
- `tests/test_deepseek_responses.py`（新增，7 例）。

## Validation

- `python3.12 -m pytest tests/test_ai_decision_advice_config.py
  tests/test_deepseek_responses.py -q` → 15 passed；
- `python3.12 -m pytest tests/test_config_yaml.py
  tests/test_config_loader_validation_cache.py
  tests/test_config_template_inheritance.py -q` → 83 passed；
- `./om config validate --source yaml --market us/hk --config-yaml
  configs/examples/config.yaml.example` → ok；
- 端到端确认：`yaml_to_market_user_config` 与
  `build_layered_runtime_config_from_user_config` 均输出
  `ai_decision_advice: {"enabled": false}`（passthrough 生效）。

## Residual risks

- DeepSeek Responses `web_search` 真机参数形态：assigned to release gate
  （受控 canary），adapter 以注入 HTTP 层隔离；
- `enabled: true` 的 API key 检查发生在 config validate；运行时缺失 key 的
  行为由 S3/S5 的调用侧兜底（unavailable）——covered by later approved slice。

## Completion status

Complete；进入 code review gate。
