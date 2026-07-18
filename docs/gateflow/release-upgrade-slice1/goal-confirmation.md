# Gateflow Goal Confirmation — release-upgrade-slice1

- **Recorded at**: 2026-07-18T16:32:43Z
- **Work unit**: 发布流程优化首批 Slice 0 + Slice 1
- **Branch**: `codex/release-upgrade-slice1`
- **Base**: `origin/main` at `b5ba7693`, VERSION `1.2.413`
- **User confirmation**: 已确认创建工作分支并执行 Slice 0 + Slice 1。

## Goal

在不修改生产升级逻辑、配置、服务或通知路径的前提下：

1. 冻结执行时最新基线；
2. 消除两个单测中的真实 30 秒等待；
3. 让 release preflight 的 `--full` 模式不再先重复执行 focused agent/plugin tests；
4. 保留 cooldown、错误聚合、release metadata、dependency graph 和完整 pytest 的验证语义。

## Success signals

- 两个目标测试各自小于 0.5 秒；
- full preflight 三次运行 median 不高于 45 秒；
- full suite 测试行为和断言不被削弱；
- dedicated cooldown test 仍断言 30 秒等待值；
- 本 work unit 不修改 production runtime/config/service behavior。

## Non-goals

- 不实施 timeout/process cleanup、dependency lock、shared venv、CI workflow 或 uv rollout；
- 不删除生产 stale cache；
- 不提交、推送、发布或修改服务，除非后续 gate 明确需要并符合授权。

## Direct evidence

- 最新基线 full preflight 三次为 108/103/106 秒（median 106 秒），full pytest 为 101.60/98.09/101.16 秒（median 101.16 秒）；
- 两个目标测试各约 30 秒，替换 sleep 后核心逻辑分别约 0.0203 秒和 0.0056 秒；
- `tests/test_required_data_prefetch_inprocess.py` 已有独立 cooldown 测试，通过 monkeypatch 记录并断言 `30.0`；
- `scripts/release_preflight.sh` 当前在 `--full` 时先跑 focused tests，再跑包含这些文件的 full pytest。

## Boundary decision

优先只改 tests 和 release preflight；不引入通用 clock abstraction。若测试现有 monkeypatch 能力足够，不改生产代码。

## Blocking questions

无。
