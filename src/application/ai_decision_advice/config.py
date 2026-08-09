from __future__ import annotations

from typing import Any, Mapping

from src.application.secret_store import LLM_DEEPSEEK_API_KEY, SecretProvider, resolve_secret

CONFIG_KEY = "ai_decision_advice"

# v1 fixed contract (docs/AI_DECISION_ADVICE_DESIGN.md sections 4, 6, 13, 17).
PROVIDER = "deepseek"
MODEL = "deepseek-v4-flash"
API_KEY_ENV = "DEEPSEEK_API_KEY"
CREDENTIAL_NAME = LLM_DEEPSEEK_API_KEY
BASE_URL = "https://api.deepseek.com"

EVIDENCE_REFRESH_INTERVAL_SECONDS = 4 * 60 * 60
EVIDENCE_FULL_RECHECK_SECONDS = 24 * 60 * 60
EVIDENCE_STALE_SECONDS = 8 * 60 * 60
EVIDENCE_REFRESH_BUDGET_SECONDS = 5 * 60
EVIDENCE_BATCH_SIZE = 5
EVIDENCE_MAX_CONCURRENT_BATCHES = 2
EVIDENCE_LOOKBACK_DAYS = 30

ADVICE_ACCOUNT_BUDGET_SECONDS = 30

SHARED_STATE_DIRNAME = "ai_decision_advice"
EXTERNAL_EVIDENCE_FILE = "external_evidence.jsonl"
SYMBOL_IDENTITY_SNAPSHOT_FILE = "symbol_identity_snapshot.json"
ADVICE_RECORDS_FILE = "ai_decision_advice.jsonl"


def ai_decision_advice_enabled(cfg: Mapping[str, Any] | None) -> bool:
    section = (cfg or {}).get(CONFIG_KEY)
    if not isinstance(section, Mapping):
        return False
    return bool(section.get("enabled"))


def resolve_api_key(
    environ: Mapping[str, str] | None = None,
    *,
    secret_provider: SecretProvider | None = None,
) -> str | None:
    return resolve_secret(
        CREDENTIAL_NAME,
        provider=secret_provider,
        environ=environ,
        legacy_env_name=API_KEY_ENV,
    )
