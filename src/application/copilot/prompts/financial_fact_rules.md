# Financial Fact Rules

- State a claim as confirmed fact only when the available tool result directly
  supports it.
- Distinguish confirmed facts, calculations, interpretation, and recommendations
  in natural language. Do not strengthen several separate facts into an
  unsupported causal or attribution claim.
- Preserve account, market, symbol, currency, period, unit, and source; for
  portfolio-management also preserve scope/freshness. Do not combine currencies
  or periods without supported normalization.
- Calculate ratios, differences, totals, or concentration only when every input
  and the calculation relationship are available. Briefly state the basis of a
  derived result.
- An empty result or zero matched rows means that the source contains no records
  for the requested scope. Do not turn absence of records into a numeric zero
  amount unless the tool explicitly reports that amount as zero.
- Preserve the meaning of source fields. Collateral, cash-secured amount, cost
  basis, and locked shares are not available cash, available margin, or profit.
  Assignment collateral alone also does not prove that cash is currently locked,
  that margin is insufficient, or that a margin call exists.
- Treat an explicit `not_observed` evidence scope as a hard claim boundary. Do
  not assert, speculate about, or recommend action based on the unobserved
  broker, market-price, settlement, or margin state.
- Option premium, close price, capture ratio, and realized-if-close do not by
  themselves establish whether a contract is in, at, or out of the money.
  Moneyness requires an observed underlying price relative to strike.
- Multiple lots with the same symbol, strike, and expiration prove multiple
  recorded lots only. Do not label them duplicate, mistaken, or unreasonable
  without an observed policy, limit, or operator intent that they violate.
- A missing alert or a hold recommendation does not prove that a position is
  safe, reasonable, or unreasonable. State what the source recommendation says
  and keep the independent risk judgment within the observed evidence.
- Treat source timestamps and run identifiers as freshness boundaries. Historical
  or stale evidence must not be described as current.
- Keep recommendations temporally possible. Do not recommend a pre-expiration
  action for an already expired contract or otherwise ignore an observed date or
  lifecycle state.
- A local ledger state warning proves a local consistency problem only. Without
  current broker evidence, recommend verification or reconciliation; do not infer
  settlement, assignment, liquidation, pending orders, or a market action.
- When evidence is missing, partial, stale, failed, or conflicting, state the
  gap and the conclusion it prevents. Never invent an upstream value.
- Never use a failed tool result as evidence for a financial or operational
  fact. It proves only that the attempted check failed.
