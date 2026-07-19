# Remote Disk and Logging Stability Plan

## Goal

在不丢失核心 OM 证据、不掩盖生产故障的前提下，把远端磁盘从“依赖人工清理”改为：

1. Futu/OpenD 故障 fail-fast，不允许 oneshot 服务无限重试；
2. runtime status 不再周期性向 journal 输出超大 pretty JSON；
3. journald/rsyslog 有可验证、可回滚的容量护栏；
4. Shadow Replay receipts 有自动保留策略；
5. Incus 宿主存储池有独立治理和告警。

## Current evidence

- 根盘清理后约 91% 使用、9.0 GB 可用。
- Incus 容器自身只解释约 9.5 GB；约 80 GB 属于宿主共享池的其他消费者。
- 最近 30 分钟：
  - `options-monitor-runtime-status.service`: 37,907 行；
  - 单次 runtime-status 完整 JSON 约 1.6 MB、37,915 行，每 15 分钟运行；
  - `options-monitor-trade-intake.service`: 1,550 行；
  - `options-monitor-auto-close-us.service`: 717 行；
  - `options-monitor-strategy-lab-sample.service`: 382 行。
- Futu 错误包含 `需要手机验证码`、`init connect fail`、`RemoteClose`。
- `auto-close-us` 和 `strategy-lab-sample` 是 oneshot，但处于 `activating` 且 timeout/runtime 均无限。
- `trade-intake` 是长期服务，当前也在重复连接失败。
- `strategy-lab-build`、`strategy-lab-settle`、`upgrade` 存在 failed unit 状态。

## Non-goals

- 不通过删 datasets 或 audit state 换空间。
- 不把容器内日志限制描述为宿主池容量保证。
- 不在未恢复 OpenD 认证前盲目重启所有 OM 服务。
- 不引入新的日志平台或第三方依赖作为本轮前置条件。

## Work Unit 0 — Emergency containment

### Actions

1. 暂停会无限重试的非长期 oneshot：
   - stop `options-monitor-auto-close-us.service`；
   - stop `options-monitor-strategy-lab-sample.service`；
   - 暂停对应 timers，直到 OpenD 认证恢复并完成 dry-run。
2. `trade-intake` 暂不直接停止；先确认其业务必要性和具体失败账号。若日志继续洪泛且无法认证，再由 CEO 决定短暂停止。
3. 记录变更前后：服务状态、日志行/分钟、磁盘可用空间。

### Success signals

- 被停止的 oneshot 不再有 Futu 重试日志；
- 总 syslog 速率在 10 分钟窗口显著下降；
- 不新增 position/trade 写入错误。

### Rollback

- OpenD 恢复后先手工 dry-run对应命令；通过后再 start timer/service。

### Gate

涉及停止生产服务，必须单独获得 CEO 明确确认。

## Work Unit 1 — Restore OpenD authentication and fail-fast semantics

### Actions

1. 确认 `lx`、`sy` 两个 OpenD gateway 中哪个要求手机验证码；完成必要的人机认证。
2. 使用项目只读入口分别验证 quote/trade context，不运行 tick、不写交易状态。
3. 对以下 oneshot 增加有界执行：
   - systemd `RuntimeMaxSec`；
   - 应用层连接尝试上限/退避；
   - 验证码或 auth-required 错误立即 fail closed，而不是无限创建 context。
4. 长期 `trade-intake` 对 auth-required 错误进入降级状态并限频告警，不做高频 reconnect loop。

### Recommended limits

- auto-close / strategy-lab sample: `RuntimeMaxSec=10m`，应用连接失败应更早退出；
- reconnect: 指数退避并设置上限；auth-required 类错误不自动无限重试。

### Validation

- 聚焦测试连接失败、auth-required、timeout、取消和重启恢复；
- 远端 dry-run 证明缺少认证时在限定时间内失败；
- 认证恢复后一次执行成功且不产生重复 context storm。

## Work Unit 2 — Reduce runtime-status log amplification

### Preferred repo-owned fix

1. 为 `runtime_status` systemd 调用增加 compact/summary 输出模式，或把完整 JSON 原子写入 bounded latest snapshot 文件；
2. journal 只保留单行 summary：`ok/status/warnings/version/drift`；
3. 错误详情写 stderr，确保告警仍可见；
4. service render 是配置真源，不依赖长期手工 override。

### Tactical fallback

若短期不能发布代码，可用 systemd drop-in 将 stdout 从 journal 移出，但必须保留 stderr，并明确这会降低可观测性；只作为临时措施。

### Success signals

- 单次 runtime-status journal 输出不超过 20 行或 16 KB；
- 仍可从既有 read-only tool/状态文件读取完整诊断；
- timer 15 分钟频率可以保留，无需通过降低监控频率掩盖输出问题。

## Work Unit 3 — Logging guardrails with safe rollout

### journald proposal

先修复洪泛并测量 24 小时稳定速率，再最终定值。初始建议：

```ini
[Journal]
SystemMaxUse=1G
SystemKeepFree=5G
MaxRetentionSec=7day
```

说明：

- 1 GB 比原 512 MB 更有可能保留有效排障窗口；
- `SystemKeepFree=5G` 只是 journald 自身的软保护，不是宿主池保证；
- 实际保留期由容量与时间中更严格者决定。

### rsyslog/logrotate proposal

```text
daily
rotate 7
compress
delaycompress
maxsize 250M
```

- 明确 `maxsize` 只在 logrotate 执行时检查，不是实时硬上限；
- 根因修复后先保留每日 timer，观察 24–48 小时；
- 只有稳定后仍出现单日大文件，才评估 hourly timer，避免无证据扩大系统级变更。

### Safe rollout

1. 备份原配置并记录 SHA-256；
2. 临时文件写入，`install` 原子替换；
3. `systemd-analyze cat-config` 验证 journald effective config；
4. `logrotate --debug` 做静态验证；
5. 受控真实 canary 验证 owner/group/mode、postrotate 和 rsyslog 继续写入；
6. 记录 rollback 命令；
7. 观察 30–60 分钟日志速率和服务状态。

### Gate

修改 `/etc`、restart journald 或触发真实轮转前，必须单独获得 CEO 确认。

## Work Unit 4 — Shadow Replay receipt retention

### Actions

- 保留 datasets 和 backtests；
- receipts 默认远端保留 7 天；
- 清理前必须确认最新本地 archive inventory 已 verified；
- 将 retention 做成 repo-owned dry-run-first 命令或受控 timer，不用裸 `find -delete` 作为长期机制；
- 每次清理输出 count、bytes、cutoff、archive manifest reference。

### Success signals

- receipts 长期低于约 500 MB；
- 任意远端删除都可在本地归档找到；
- 清理失败不会影响 Strategy Lab dataset。

## Work Unit 5 — Incus host storage governance

### Actions

1. 获取宿主只读权限，统计 containers/images/backups/snapshots；
2. 解释当前约 80 GB 容器外使用量；
3. 设置宿主池告警，建议 warning <15 GB、critical <8 GB；
4. 对镜像、快照和备份建立独立保留策略。

### Boundary

该 work unit 属于基础设施 ownership，不能由容器内 journald 配置替代。

## Sequencing

1. **WU0 紧急遏制**：先阻止无限重试继续制造日志。
2. **WU1 OpenD 认证与 fail-fast**：修复业务根因。
3. **WU2 runtime-status compact 输出**：消除最大稳定日志源。
4. 观察至少 24 小时日志速率。
5. **WU3 容量护栏**：按实测速率落配置并做 canary/rollback。
6. **WU4 receipts 自动保留**。
7. **WU5 宿主池治理**，可与 WU1–WU4 并行，但必须完成。

## Overall success criteria

- 根盘普通可用空间持续高于 8 GB；宿主最终目标高于 15 GB。
- 没有 oneshot 服务长期停留在 `activating`。
- auth-required 不产生无限 reconnect loop。
- runtime-status 单次 journal 输出不超过 20 行或 16 KB。
- journal <= 1 GB；syslog 单日增长有基线且轮转成功。
- Shadow Replay receipts <= 7 天并有 verified local archive。
- 所有生产配置变更均有备份、canary、rollback 和验证记录。
