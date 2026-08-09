"""AI Decision Advice: audited suggestion layer for opening candidates.

Two stages share this package:

- External Evidence Collector (public web evidence via DeepSeek Responses
  native web_search; no account context);
- AI Decision Advice (frozen candidate/portfolio/option-position/evidence
  inputs -> strict JSON advice -> deterministic validation and rendering).

See docs/AI_DECISION_ADVICE_DESIGN.md for the authoritative contract.
"""
