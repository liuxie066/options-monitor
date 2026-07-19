# Implementation — trade-intake-auth-aware-restart slice 3

- Gate: implementation
- Scope: generated systemd lifecycle policy
- Changed: `src/application/service_deploy.py`, `tests/test_service_deploy.py`
- Decision: optional renderer field; exit 78 is restart-preventing only for trade intake.
- Validation: `tests/test_service_deploy.py` -> 100 passed.
- Docs: generated unit behavior is captured in Gateflow artifacts; no CLI syntax changed.
- Residual risks: deployed units are not updated by this Draft PR; rollout must render/install units under a separate production gate.
- Status: complete pending review.
