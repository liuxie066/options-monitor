# Close Advice v3 Devflow Impl scope

下方 JSON 是实施完成时的检查点。其 hash、文件清单、验证结果与 next_action 不代表后续 DeepReview 修复后的工作树现值；当前结果以最新 review artifact 和工作树为准。

```json
{
  "goal": "Implement single Close Advice v3 policy with independently sealed trading-calendar evidence and near-expiry hold",
  "design_doc": "docs/CLOSE_ADVICE_CONTRACT.md",
  "design_sha256": "0f025583a76086eccb3807d90ffc123b8bc52f1a6563262691c926f0ca9a1a0e",
  "implementation_workspace": "<task-worktree>/options-monitor",
  "review_base": "origin/main@915095650316a6e9d5d82c0e8924046528d19f91",
  "head": "915095650316a6e9d5d82c0e8924046528d19f91",
  "baseline_git_status": [
    " M docs/CLOSE_ADVICE_CONTRACT.md",
    " M domain/domain/close_advice.py",
    " M src/application/close_advice_runner.py",
    " M src/application/config_validator.py",
    " M tests/test_agent_plugin_smoke.py",
    " M tests/test_assistant_runtime.py",
    " M tests/test_close_advice_runner.py",
    " M tests/test_daily_decision_brief_service.py",
    " M tests/test_strict_close_advice.py"
  ],
  "implementation_baseline": {
    "staged": [],
    "unstaged": [
      {
        "path": "docs/CLOSE_ADVICE_CONTRACT.md",
        "status": " M",
        "sha256": "0f025583a76086eccb3807d90ffc123b8bc52f1a6563262691c926f0ca9a1a0e",
        "size": 24869
      },
      {
        "path": "domain/domain/close_advice.py",
        "status": " M",
        "sha256": "774fb2d97d13af71f99f93bc456ef7150fa72a1c83e33b5b49cd9bd6551f36df",
        "size": 15333
      },
      {
        "path": "src/application/close_advice_runner.py",
        "status": " M",
        "sha256": "3d4ff0a5078d812a1a6552dc755f06bc44de2bd13462659f0d060c4e3ba1e3ed",
        "size": 86371
      },
      {
        "path": "src/application/config_validator.py",
        "status": " M",
        "sha256": "5980793d5066dcaac906f99f1066b5fe16cebff36b7215ef436a7b6b52215c4a",
        "size": 75588
      },
      {
        "path": "tests/test_agent_plugin_smoke.py",
        "status": " M",
        "sha256": "99545ba08957aa725bd866b52f5f9863532339e2e0b8fa76ed719b408b13d537",
        "size": 207295
      },
      {
        "path": "tests/test_assistant_runtime.py",
        "status": " M",
        "sha256": "a1747f45f2572d241f826647c1c71a68b370025a64b63f620708eebc433816c6",
        "size": 9446
      },
      {
        "path": "tests/test_close_advice_runner.py",
        "status": " M",
        "sha256": "1f0a155b01f4efe79a6bc87c33f80584041a2ece4c94446f3b94322cb8dc1758",
        "size": 20495
      },
      {
        "path": "tests/test_daily_decision_brief_service.py",
        "status": " M",
        "sha256": "b92c531ba254ace7a950c4957bc457a2417dd4f7fee21c1febc261de2adedc8b",
        "size": 102311
      },
      {
        "path": "tests/test_strict_close_advice.py",
        "status": " M",
        "sha256": "5441046123e782e8f0491f3d993e91f8f85d012ead755d0ee8e5c627d9823735",
        "size": 10124
      }
    ],
    "untracked": []
  },
  "success_signals": {
    "S1": "Each eligible lot has one row; economic failure holds; valid near expiry low delta holds; otherwise close only with sufficient evidence",
    "S2": "Independent market calendar handles holidays, half days, market-local dates, current session boundary and sealed receipt/hash",
    "S3": "Scheduled and manual entries use same v3 policy; missing evidence is not_evaluable and does not notify; old reports fail closed"
  },
  "authorized_slices": [
    {
      "slice": "calendar",
      "signals": [
        "S1",
        "S2",
        "S3"
      ],
      "depends_on": [],
      "owners": [
        "src/application/close_advice_required_data.py",
        "src/application/tick_account_execution.py",
        "src/application/opend_symbol_fetching.py",
        "src/application/opend_symbol_outputs.py",
        "src/application/close_advice_runner.py",
        "relevant tests"
      ]
    },
    {
      "slice": "decision_consumers",
      "signals": [
        "S1",
        "S3"
      ],
      "depends_on": [
        "calendar"
      ],
      "owners": [
        "domain/domain/close_advice.py",
        "src/application/close_advice_runner.py",
        "readers and Daily Brief",
        "relevant tests"
      ]
    }
  ],
  "scope_note": "Pre-existing nine task edits migrated byte-for-byte from older worktree. The prior clean .devflow/scope.md belonged to completed Bot work and remains in Git history at review_base.",
  "slice_checkpoints": [
    {
      "slice": "calendar",
      "diff_fingerprint": "f4d2211c7604b073fad01383fbfbbf2264355c73d138fc2572d5055122a93275",
      "validation": "tests/test_close_advice_required_data.py: 26 passed; frozen v3 close/hold and calendar timeout/cross-year evidence",
      "done": true,
      "checkpoint_note": "Final restricted-diff fingerprint; no earlier temporal checkpoint was preserved"
    },
    {
      "slice": "decision_consumers",
      "diff_fingerprint": "df8a9e9d5ab64d0d27a9742454b504d8a4a9debf4894977cce2d8ee874b20ba1",
      "validation": "Related pytest suite: 313 passed; Ruff --no-cache passed; git diff --check passed",
      "done": true
    }
  ],
  "status": "implementation_completed",
  "inventory": [
    {
      "path": "docs/CLOSE_ADVICE_CONTRACT.md",
      "sha256": "0f025583a76086eccb3807d90ffc123b8bc52f1a6563262691c926f0ca9a1a0e",
      "size": 24869,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "domain/domain/close_advice.py",
      "sha256": "99db3a8336c9273c2c9a86a56d8b9c04d73a6798c8c5c010aeb1e9e4d5807ea5",
      "size": 16471,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "src/application/agent_tools/close_advice_read_impl.py",
      "sha256": "a9b2f2221e3679b7e5cbd6b2f185ec62f6ea5b013136a9d97a30f52752000ef6",
      "size": 34911,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "src/application/close_advice_required_data.py",
      "sha256": "330363823d227fa2940e194d339a9b68c6f0bbd12ef06431141055c38eb81122",
      "size": 32258,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "src/application/close_advice_runner.py",
      "sha256": "5029cac45955c3dc92fd295d7d3fbbfefaa04fe4d385dbef6274be53867dff20",
      "size": 94197,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "src/application/config_validator.py",
      "sha256": "46a7c502c1a27224c8cc61fe79c86d2698054c4ea93fcbf84324389b91860ee6",
      "size": 75588,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "src/application/daily_decision_brief_service.py",
      "sha256": "1250603987cb4fc288c0c3af97720498e3a616ee1a72b1646a16dd46bfb18588",
      "size": 103920,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "src/application/tick_account_execution.py",
      "sha256": "c36befd137a1aa324ad13f5ba52b9c5fe176a09f9fba9e972f73c55813b2280f",
      "size": 71596,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "tests/test_agent_plugin_smoke.py",
      "sha256": "ffcc6358df900236d771372ddf433ff06e4acfcbf5e4e27e209e0c1293f5dc9e",
      "size": 207421,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "tests/test_assistant_runtime.py",
      "sha256": "3260cf96ea0516fea48e9672d90d5f5358d4fc01f92af2ce145aabefddd72a14",
      "size": 9446,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "tests/test_close_advice_required_data.py",
      "sha256": "42e520f84e38c73cbe91450cec22f7a3f945e67ba6550701c916448d93535db8",
      "size": 53304,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "tests/test_close_advice_runner.py",
      "sha256": "31256a599567c0c14d1c9e55dddc92c5c25cae8020af74d5bfc5d9fececb81a5",
      "size": 22875,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "tests/test_daily_decision_brief_service.py",
      "sha256": "920941a1f6e0437061972d5946d02b7c4f275799ea538ee1855f3b9db374e35b",
      "size": 102311,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "tests/test_strict_close_advice.py",
      "sha256": "9fd6af713ed0c4e0446f8e27ac94314b4302141ec862731a7a90303f73927169",
      "size": 10782,
      "status": "M",
      "classification": "planned"
    },
    {
      "path": "tests/test_tick_account_execution_barrier.py",
      "sha256": "d091ee711566e687a9c5d3a5556a5755d115bef207d20c25b2948e4ac12c520e",
      "size": 59124,
      "status": "M",
      "classification": "planned"
    }
  ],
  "content_revision": "08302a129b6b1787621bc8cbbacebe8bd4a25c1b838a0e2139dbe013e0ebcad7",
  "evidence_paths": [
    "docs/CLOSE_ADVICE_CONTRACT.md",
    "tests/test_close_advice_required_data.py",
    "tests/test_close_advice_runner.py",
    "tests/test_strict_close_advice.py"
  ],
  "residual_risks": [
    {
      "item": "No local historical Close Advice reports available for v2/v3 per-lot replay",
      "classification": "needs-new-issue-or-user-decision",
      "owner": "Close Advice strategy owner",
      "destination": "Read-only historical replay before production activation"
    }
  ],
  "next_action": "Await separately authorized Review or Delivery"
}
```
