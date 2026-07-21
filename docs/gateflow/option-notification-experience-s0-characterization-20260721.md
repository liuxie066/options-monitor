# Gateflow Slice 0 Implementation — Characterization 与容量基线

- Gate：implementation slice 0
- Work unit：期权监控通知体验升级
- Scope：只读 characterization、本地测试基线、远端容量证据
- Changed files：仅本 artifact
- Production mutations：无
- Real notifications：未触发

## Decisions and evidence

1. 当前 systemd HK/US timer 均为市场当地时间工作日 `09..16:00/10:00`，保留 10 分钟唤醒可行。
2. 当前 runtime `OM_RUNTIME_ROOT=/var/lib/options-monitor`，生产代码为 `/home/liuxie/apps/releases/1.4.0`。
3. 远端 87 个有 pipeline metrics 的 run、172 个账户样本：
   - min 2.174s；median 3.337s；P95 5.761s；P99 14.784s；max 32.090s；
   - 两账户并发，最近正常 HK 批次约 3.4–4.0s 完成；
   - `15:50` 最近批次约 4s 完成，通常能在 `16:00` recovery slot 前形成首次发送尝试。
4. 88 个有 prefetch audit 的 run：每轮 prefetch item median 10；估算 OpenD call median 10、P95 56.3、max 64。最短 20 分钟目标间隔相对现有耗时有充分余量。
5. 现网 artifacts 证明同一 scheduled target 会在下一次 10 分钟唤醒重复扫描：
   - `09:40` target 在 `09:40` 和 `09:50` 均运行；
   - `10:00`、`11:00`、`14:00`、`15:00` 等 target 也出现 `:00` 与 `:10` 重复；
   - 根因与计划一致：`last_run_utc_by_account` 保存完成时间，而不是 target identity。
6. 最新 `15:50` brief revision 已成功持久化但 `delivery_kind=none`，tick reason 为 `no_account_notification`，直接证明当前 material-change 策略不满足固定报告点“无变化也发送”。

## Validation

```text
python3.12 -m pytest -q \
  tests/test_scan_scheduler_notify_semantics.py \
  tests/test_scan_scheduler_scan_per_account.py \
  tests/test_multi_tick_scheduler_application.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_daily_decision_brief_repository.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_scenarios.py \
  tests/test_daily_decision_brief_cli.py
```

Result: `109 passed in 1.29s`.

仓库 `.venv/bin/python` 未安装 pytest，因此测试使用项目支持的 `python3.12`；未安装依赖、未修改环境。

## Docs decision

容量与运行时证据只记录在 Gateflow artifact；不修改用户文档。

## Residual risks and uncovered areas

- OpenD call count 来自 audit payload 中 call counters 的求和，不是 broker 侧服务计数：分类为 rollout observation；不阻塞本地实施。
- 现有 artifacts 只证明近期 HK/US 实际负载，不保证未来标的数量大幅增长后仍满足 20 分钟：分类为 production rollout monitoring。
- `15:50` 极端 32s 样本仍远低于 10 分钟 recovery slot，但 provider latency/故障不包含在 pipeline metric：由 Slice 4 exact retry 与 rollout canary 覆盖。

## Completion status

- Slice 0 implementation：pass
- Capacity gate：pass-with-monitoring
- Current gate / next entry point：Slice 0 code review
