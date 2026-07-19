# Final Closeout — trade-intake-auth-aware-restart

- Gate: final closeout
- Work unit: `trade-intake-auth-aware-restart`
- Status: final closeout pass
- Draft PR: https://github.com/liuxie066/options-monitor/pull/88

## What changed

- The trade push listener now checks authentication/readiness on its existing OpenD trade context.
- Explicit phone-verification classification stops the listener with stable process exit code 78 and writes `status=blocked`, `stage=auth_required`, classifier code/message, and detail.
- Retryable failures retain automatic recovery with exponential backoff capped at 60 seconds; a healthy observation resets the delay.
- Multi-source listeners propagate terminal auth and stop sibling listeners.
- Generated trade-intake systemd units retain `Restart=always` but add `RestartPreventExitStatus=78`.
- README troubleshooting documents the manual recovery path after OpenD phone verification.

## Verification

- Focused trade-intake, watchdog/error-policy, and service-deploy tests: 159 passed.
- Python compileall for changed application modules: passed.
- `git diff --check`: passed.
- GitHub checks after the PR-review commit: Analyze/actions, Analyze/python, CodeQL, agent-plugin, and guardrails passed.

## Finding status

- Plan review findings PR-1 through PR-4: fixed and re-reviewed.
- Slice review finding CR-1: fixed by reducing steady-state health polling from one second to five seconds.
- Aggregate deepreview findings DR-1 and DR-2: fixed and re-reviewed.
- PR review: pass, no accepted findings.
- No unclassified finding remains.

## Docs decision

Public troubleshooting documentation was updated because terminal exit 78 intentionally requires operator recovery after authentication.

## Remaining risks / owners

- Whether the production Futu SDK exposes `需要手机验证码` through the existing trade context's global-state response: production canary after release.
- Five-second state-check traffic and resulting log rate: production observation after release.
- Generated unit installation and service restart: separate CEO-authorized production rollout.

## Issue link status

This work unit was not tied to a GitHub issue; no closing keyword or issue comment is required.

## Next entry point

CEO may review Draft PR #88. Merge, release, production upgrade, generated-unit installation, service restart, and production canary remain separately authorized actions.
