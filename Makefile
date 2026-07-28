SELECTED_PYTHON := $(shell bash -c 'source scripts/python_runtime.sh && om_select_repo_python "$(CURDIR)"')
ifeq ($(strip $(SELECTED_PYTHON)),)
$(error options-monitor requires Python >= 3.12; runtime selection failed)
endif
PYTHON := $(SELECTED_PYTHON)

test:
	$(PYTHON) tests/run_tests.py

test-all:
	$(PYTHON) tests/run_tests.py --all

smoke:
	$(PYTHON) tests/run_smoke.py

lint:
	$(PYTHON) -m ruff check .

agent-spec:
	chmod +x ./om-agent
	./om-agent spec

agent-smoke:
	chmod +x ./om-agent
	$(PYTHON) -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py

release-check:
	chmod +x ./om-agent
	$(PYTHON) scripts/release_check.py --require-current-taxonomy --require-delta-coverage

release-preflight:
	bash scripts/release_preflight.sh $(ARGS)
