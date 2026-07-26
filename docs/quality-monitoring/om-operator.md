# OM Quality Producer 操作契约

OM producer 只读取现有 runtime/intake/ledger/lifecycle 事实，并通过独立
`refresh_cache=True` OpenD 查询取得最终期权持仓。检查不会修改交易事件、
position lots、生命周期 case 或 OpenD 数据。

本地入口：

```text
./om quality refresh --config-key us --config-key hk
./om quality refresh --config-key us --day-end-strict
./om quality status --json
./om-agent run --tool quality_status --input-json '{}'
```

`refresh` 是定时 producer 入口，会原子发布：

```text
<OM_RUNTIME_ROOT>/output_shared/state/quality/status.v1.json
<OM_RUNTIME_ROOT>/output_shared/state/quality/control_state.v1.json
```

第二个文件只保存差异首次出现时间、下一次只读复查时间以及生命周期首次深对账时间，
不保存账户 ID、完整持仓或 OpenD 原始响应。

只读 HTTP：

```text
OM_QUALITY_READ_TOKEN=<independent-token> ./om quality serve --host 127.0.0.1 --port 8792
```

- `GET /health` 只证明 endpoint 进程可用；
- `GET /quality/status` 需要独立 bearer token，只读取已发布 artifact；
- HTTP 请求不会调用 OpenD、不会 replay repair、不会写 evidence；
- 默认只允许 loopback；生产受控内网绑定必须显式设置
  `OM_QUALITY_ALLOW_REMOTE_BIND=true` 并由外围传输层保护。

门禁：

- `OM_QUALITY_ONBOARDED=false` 时 producer 可部署和建立 baseline，但不改变消费者行为；
- 完成生产 baseline 与 Hub onboarding 后设为 `true`；
- 此后 stale artifact 或明确 blocking 结论会阻断 close advice 和正式
  option performance；
- 普通候选扫描不读取该门禁；
- 没有临时 observe/bypass 开关，门禁实现故障通过回滚 producer release 处理。

调度语义：

- 常规 producer 每 15 分钟执行；
- 持仓首次差异保存 `next_recheck_at_utc=+1m`，第二次窗口到 `+5m`；
- 调度器只在到期时再次运行同一个只读 refresh，不在单次进程中 sleep；
- 日终使用 `--day-end-strict`，首次确定性差异立即阻断。
