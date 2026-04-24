POLICY_REPO_PATH ?= $(CURDIR)
POLICY_ACTION ?= running
PYTHON ?= $(if $(wildcard .venv/bin/python),./.venv/bin/python,python3)

policy-check:
	python3 scripts/policy_check.py --repo-path "$(POLICY_REPO_PATH)" --action "$(POLICY_ACTION)"

deploy-prod:
	$(MAKE) policy-check POLICY_ACTION=deploy
	python3 scripts/deploy_to_prod.py --apply

deploy-safe:
	$(MAKE) policy-check POLICY_ACTION=deploy
	bash scripts/deploy_safe.sh

test:
	$(PYTHON) tests/run_tests.py

test-all:
	$(PYTHON) tests/run_tests.py --all

smoke:
	$(PYTHON) tests/run_smoke.py

agent-spec:
	chmod +x ./om-agent
	./om-agent spec

agent-smoke:
	chmod +x ./om-agent
	$(PYTHON) -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py

release-check:
	chmod +x ./om-agent
	$(PYTHON) scripts/release_check.py
