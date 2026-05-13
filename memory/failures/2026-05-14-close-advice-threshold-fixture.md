## Failure

Changing close advice remaining-annualized defaults from `0.08/0.12` to
`0.045/0.07` made a runner test fixture with `mid=0.4`, `strike=100`, and
`dte=30` stop qualifying as `strong`.

## Lesson

Threshold changes can invalidate fixtures whose main assertion is unrelated to
tiering. Recompute fixture economics and keep the setup inside the new target
tier before trusting a failing assertion as a product regression.
