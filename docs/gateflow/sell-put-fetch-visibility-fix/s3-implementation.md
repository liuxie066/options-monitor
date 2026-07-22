# Gateflow Implementation — S3 Quality and Rollout Budget

- Gate: `implementation`
- Work unit: `sell-put-fetch-visibility-fix`
- Slice: `S3`
- Scope: read-only production evidence, request-budget calculation, and broad local regression.
- Production files: none.
- Generated architecture artifacts: `docs/DEPENDENCY_GRAPH.md`, `docs/dependency_graph.mmd`.
- External mutations: none; no notification, config/state write, service action, market-data canary, release, or deployment was performed.

## Comparable scheduled-run baseline

The latest seven successful comparable US scheduled runs were read from the production runtime. Each ran both lx and sy scans, had a usable lx prefetch summary, `errors=0`, `failed_count=0`, and `budget_triggered=false`.

| run | lx pipeline ms | sy pipeline ms | run max ms | prefetch snapshot codes | prefetch OpenD snapshot calls | option-chain calls | rate-gate wait s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `20260721T150035Z-2456d6` | 3598 | 3022 | 3598 | 629 | 8 | 0 | 0 |
| `20260721T160036Z-2a0a3d` | 3437 | 2961 | 3437 | 631 | 8 | 0 | 0 |
| `20260721T170036Z-472cb8` | 3108 | 2958 | 3108 | 625 | 8 | 0 | 0 |
| `20260722T134031Z-e3bb35` | 2287 | 2668 | 2668 | 625 | 8 | 50 | 199.318479 |
| `20260722T140036Z-add5f4` | 3272 | 2535 | 3272 | 602 | 8 | 0 | 0 |
| `20260722T150008Z-e6bb67` | 2858 | 2933 | 2933 | 619 | 8 | 0 | 0 |
| `20260722T160016Z-b4821f` | 2818 | 2955 | 2955 | 613 | 8 | 0 | 0 |

Sorted run maxima are `[2668, 2933, 2955, 3108, 3272, 3437, 3598]` ms. Linear p95 is `3549.7` ms, so the planned duration ceiling is:

```text
max(p95 * 1.25, p95 + 30000 ms) = 33549.7 ms
```

The separate hard ceiling remains 600 seconds. The 199.318479-second rate-gate wait in the 13:40 run is recorded explicitly: `pipeline_ms` excludes that wait, so it is not hidden inside the duration baseline.

## Fault-run request budget

For `20260722T140036Z-add5f4`, the run-level prefetch covered eight symbols and requested 602 snapshot codes in eight OpenD snapshot batches. The cached TCOM chain for the two selected expirations contains the following contracts inside each account plan:

- run-level correct TCOM window `34.456..43.07`: ten codes total across Put 35/40 and Call 45/50/55, one batch;
- old lx account window `17.41984..21.7748`: four Call-only codes, one batch;
- old sy account window `34.456..43.07`: eight codes, one batch;
- fixed lx/sy account window `34.456..43.07`: eight codes per account, one batch each.

The production chain is sparse and has no 42.5 strike. The existing coverage tolerance therefore causes both account reads to refetch their account plan even when their specs are identical. On that real chain the conservative full-run estimate is:

| case | snapshot codes | snapshot batches | expirations | option-chain calls |
|---|---:|---:|---|---:|
| old behavior | `602 + 4 + 8 = 614` | `8 + 1 + 1 = 10` | unchanged | unchanged |
| fixed behavior | `602 + 8 + 8 = 618` | `8 + 1 + 1 = 10` | unchanged | unchanged |
| delta | `+4` (`+0.65%`) | `0` | `0` | `0` |

Decision: **pass**. The fix expands only the lx TCOM account snapshot by four codes, does not add a snapshot batch, and does not change expiration or option-chain requests. This is materially below the planned operational budget. A live duration comparison still belongs to the authorized post-release canary because no market-data run was permitted in this slice.

## Regression results

- Focused S1 gate: `35 passed`.
- Focused S2 gate: `53 passed`.
- Tick/regression gate: `118 passed, 4 warnings`.
- Full repository gate: `2998 passed, 10 skipped, 6 warnings`.
- Previously reported 18 subprocess failures were test-environment-only: the clean temporary worktree lacked `.venv/bin/python`. Pointing the temporary worktree at the main checkout's existing virtualenv made all 18 pass; that temporary symlink was removed after validation.
- The remaining initial failure was the expected generated dependency-graph drift after deleting `src/application/pipeline_steps.py`. Regenerating the two tracked graph artifacts reports `production_modules=479`, `cycles=0`, and makes `tests/test_dependency_graph_generator.py` pass.
- `git diff --check`: pass.

## Residual risk and destination

- Same-spec account refetch on a sparse live option chain remains a performance/coverage optimization opportunity. It does not affect visibility correctness and, for the observed TCOM run, does not increase snapshot batch count. It is deferred to a separate account-independent market-window/coverage work unit if live metrics justify it.
- Cross-process shared-file atomicity remains outside this work unit and should be handled as a concurrency/recovery bug only if reproduced.
- Live canary timing, release, and deployment remain pending explicit operator authorization.

- Status: `accepted`.
