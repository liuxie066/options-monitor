PYTHON ?= $(if $(wildcard .venv/bin/python),./.venv/bin/python,python3)

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
