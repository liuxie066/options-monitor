## Context

US cron 的实际生效配置已经是 `config.us.json`，HK 对应 `config.hk.json`。仓库内的 CLI、agent、WebUI、healthcheck、watchlist、multi-account tick 等入口也已经默认指向这两份 market-specific runtime config。

与此同时，`config.json` 仍可能作为本地历史文件存在。如果不明确其地位，就会留下“修改了 config.json 但 cron 没生效”的双真相源风险。

## Decision

1. `config.us.json` 和 `config.hk.json` 是唯一 canonical runtime entrypoints。
2. `config.json` 不是 runtime 入口，不参与 US/HK cron 的正式配置读取。
3. 如果本地或仓外仍保留 `config.json`，它只能被视为 legacy compatibility artifact，不能继续被文档、脚本或人工操作默认当成正式配置。
4. 所有运行入口应继续显式传 market-specific config，或在未传入时默认回落到 `config.us.json` / `config.hk.json`。

## Rationale

- 当前代码已经完成绝大多数治理：`scripts/agent_plugin/config.py`、`src/interfaces/cli/main.py`、`scripts/validate_config.py`、`scripts/send_if_needed.py`、`scripts/multi_tick/main.py` 等都默认使用 `config.us.json` / `config.hk.json`。
- `domain/domain/config_contract.py` 已将 canonical configs 固定为 `config.us.json` / `config.hk.json`。
- `CONFIGS.md` 和 `docs/GUARDRAILS.md` 已明确禁止把 `config.json` 当作 runtime 入口。
- 因此这里不需要代码大重构，真正需要的是清除认知层歧义，防止 legacy 文件继续被误认为“活配置”。

## Operational Guidance

- US cron：只认 `config.us.json`
- HK cron：只认 `config.hk.json`
- 文档、脚本示例、排障口径：统一只引用 market-specific config
- 发现本地仍保留 `config.json` 时，默认把它视为待迁移/待删除遗留文件，而不是正式配置源

## Follow-up

- 后续可以做一次仓外运行环境审计，确认没有外部 shell/cron/launchd 仍在传 `config.json`。
- 如果确认没有任何外部依赖，可以删除本地遗留 `config.json`，进一步消除双真相源风险。
