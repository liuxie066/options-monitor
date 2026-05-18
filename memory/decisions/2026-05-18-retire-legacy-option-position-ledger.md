# Retire legacy option position ledger module

`domain/domain/option_position_ledger.py` is retired after the canonical ledger
cutover. Runtime position ownership is now:

- `domain/domain/ledger/` for canonical event, lot, projection, and invariants.
- `src/application/ledger/event_codec.py` for stored event decoding and legacy
  JSON import boundaries.
- `src/application/ledger/publisher.py` for publishing canonical projections to
  legacy-compatible `position_lots` records.

Tests that need legacy-shaped event payloads should use test-local helpers, not
a production domain compatibility module.

