# Gateflow Goal Confirmation — Earnings Near-Expiry Window

- Work unit: `earnings-near-expiry-window`
- Gate: `goal confirmation`
- Date: 2026-08-11
- Status: confirmed by user
- Branch: `feat/earnings-near-expiry-window`
- Base: `origin/main@8902f9fd`

## User problem

当前 Sell Put、Covered Call 以及 Combo Yield 的 Funding Put 只要在扫描日到合约到期日之间发现已知财报，
就会把合约作为财报风险拒绝。这个规则把距离到期仍很远的财报也当作硬风险，过于严格；同时，OpenD
无法覆盖某个区间和“已知存在财报”没有被清楚地区分，部分合约缺证据时还可能让整组已具备完整证据的候选
失去推荐资格或不产生明确告警。

## Confirmed goal

把财报硬过滤改为统一的“临近到期窗口”规则：`N = 6` 个自然日，包含边界。对候选合约，以市场本地
扫描日期和到期日计算：

```text
pending = earnings_date >= market_local_scan_date
days_before_expiration = (expiration_date - earnings_date).days
blocking = pending and 0 <= days_before_expiration <= 6
```

- `blocking = true`：作为已知财报风险，拒绝该合约。
- 财报日在扫描日之后且到期日之前、但 `days_before_expiration > 6`：保留为软风险证据和提示，不拒绝。
- 财报日在到期日之后：与该合约无关。
- 财报日在扫描日之前：视为历史事件，与该次开仓无关。
- 财报日与市场本地扫描日同日：整天都按“尚未发生”处理，不依赖 OpenD 的时间戳或发布类型推断是否已发生。

同一条规则应用于 Sell Put、Covered Call 和 Combo Yield Funding Put。Combo Yield 不设置第二套窗口，
Participation Call 也不新增独立财报过滤。

## Evidence and rationale

- 当前 `earnings_calendar.project_earnings_for_expiry()` 按完整的扫描日至到期日判断事件和失败区间；
  `candidate_engine.evaluate_opening_candidate_policy()` 随后只要 `earnings_has_event` 为真就拒绝。
- 生产 14:00 港股批次中，0700.HK 的已知财报日为 2026-08-12。相对 2026-08-21 到期日为 9 天，
  按新规则不应因为财报被过滤；9992.HK 的 2026-08-20 财报相对 2026-08-21 为 1 天，应过滤，
  相对 2026-08-28 为 8 天，不应过滤。
- OpenD 返回的 `earnings_timestamp` 可能是 00:00、12:00 或 12:30 等日历字段，不能证明同日事件已实际发布；
  用户明确选择同日整天按未发生处理。
- Combo Yield Funding Put 已复用 Sell Put underwriting 路径，因此正确的所有权边界是共享候选引擎和
  财报证据投影，而不是在 Combo 编排层复制策略。

## Success signals

1. 硬过滤窗口固定为 6 个自然日且包含第 0 天和第 6 天；第 7 天及更远的到期前财报不触发硬过滤。
2. 同日财报在市场本地自然日内始终视为 pending；逻辑不再使用日内时间戳判断已经发生。
3. Sell Put、Covered Call、Combo Yield Funding Put 使用同一政策版本和同一窗口；不存在策略间漂移。
4. OpenD 对合约硬窗口覆盖完整且没有阻断事件时，合约可继续经过 RV、收益率、流动性、资金/持仓容量等
   原有门槛；财报放行不等于最终入选。
5. OpenD 对硬窗口覆盖不足且没有足以作出确定拒绝的阻断事件时，该合约以
   `risk_earnings_unavailable` fail closed；不得伪装成“已知存在财报”或干净的无事件。
6. 某些合约硬窗口证据不可用时，只阻断受影响合约/范围；其他证据完整的候选仍可推荐，同时 Daily Brief
   和 AI Advice 明确披露候选全集不完整。若没有任何可推荐候选且仍有硬证据缺口，Advice 继续 fail closed。
7. 仅软窗口覆盖失败时不阻断候选，但必须保留可审计的软证据缺口；已知较远财报作为非阻断提示保留。
8. 财报证据、候选决策、策略 policy hash、冻结候选快照和面向用户的风险说明能区分：阻断事件、较远事件、
   硬证据不可用、软证据不完整。
9. 0700.HK/9992.HK 边界反例、OpenD 分段失败、三种策略一致性、部分候选仍可推荐、零候选 fail closed
   均有确定性回归测试；相关 analyze、测试和 `git diff --check` 通过。

## Non-goals and safety boundary

- 不修改 RV、Delta、收益率、流动性、权利金、乘数、费用、资金容量、持仓覆盖或排序规则。
- 不保证 0700.HK 或其他标的一定产生候选；新规则只移除不合理的财报硬过滤。
- 不给 Combo Yield Participation Call 增加独立财报门槛，不扩展 Combo Yield AI Advice 适配器。
- 不把 N 做成第二套 Combo 配置，也不引入按策略、账户或标的不同的窗口。
- 不修改 `config.yaml`、`config.us.json`、`config.hk.json`、secret 或生产运行数据。
- 不重写历史运行 artifact，不把旧证据静默解释成新政策结果。
- 不发送真实通知，不合并 main，不发布、不部署、不升级远端。

## Overdesign deliberately excluded

本轮使用一个版本化的共享领域常量和现有 earnings projection / Candidate Engine / snapshot / renderer 边界。
不新增配置层、数据库表、事件服务、后台任务、第二套排名器或 Combo 专用过滤器。

## Blocking open questions

无。用户已经确认 N=6、自然日、包含边界、三种适用路径、同日整天 pending、部分范围隔离和上述范围边界。
