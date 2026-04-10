POLICY_REPO_PATH ?= $(CURDIR)
POLICY_ACTION ?= running

policy-check:
	python3 scripts/policy_check.py --repo-path "$(POLICY_REPO_PATH)" --action "$(POLICY_ACTION)"

deploy-prod:
	$(MAKE) policy-check POLICY_ACTION=deploy
	python3 scripts/deploy_to_prod.py --apply
