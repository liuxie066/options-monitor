# Tests Can Anchor Retired Compatibility Services

During the option ledger cutover, runtime code had already stopped importing `option_positions_service`, but tests still imported it heavily through `svc`. That made the old service appear removable while it was still anchored by regression setup code.

Lesson: before deleting a compatibility module, search both `src` and `tests`. If tests still use the compatibility name, migrate them to the canonical owner modules first; otherwise the compatibility surface remains part of the de facto API and deletion will be deferred again.
