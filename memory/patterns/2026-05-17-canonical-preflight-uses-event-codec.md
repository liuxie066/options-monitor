# Canonical preflight uses event codec

When manual void or repair builds preview events as canonical `TradeEvent` payloads,
ledger preflight must decode them through `src.application.ledger.event_codec`.

Do not pass canonical preview payloads through the legacy trade-event importer:
it expects `position_effect` / legacy side fields and will reject valid canonical
`event_type` / `contract_key` payloads before projection can run.

