# 运行与数据质量监控 — Phase 5 生产上线 Runbook

- **状态**：执行前已固化；生产动作待逐项授权
- **日期**：2026-07-26
- **实施计划**：[implementation-plan.md](implementation-plan.md)
- **完成证据**：[implementation-status.md](implementation-status.md)
- **检查矩阵**：[check-matrix.md](check-matrix.md)

本文是 Phase 5 的唯一跨仓库生产执行顺序。它不替代 OM、PM 各自的发布和安装入口，也不授权任何生产写入。

## 1. 不可合并的授权边界

以下动作必须分别具有明确授权；只读检查不能推导出写权限：

| Gate | 动作 | 最小授权 |
|---|---|---|
| `AUTH-REL` | 推送分支/tag、创建私有仓库、发布 GitHub Release | 三仓库发布 |
| `AUTH-SVC` | 创建用户/目录、安装依赖、修改 env/config/systemd、启停服务 | 目标实例生产变更 |
| `AUTH-DATA` | PM OpenD 全量同步并写 holdings/cash/MMF | 指定账户业务数据写入 |
| `AUTH-NOTIFY` | 使用真实飞书机器人发送 incident/recovery | 真实质量通知 |
| `AUTH-DEADMAN` | 配置外部 endpoint/token、执行 missed-heartbeat | 外部 dead-man |
| `AUTH-RB` | 停止正式消费者、切换旧版本、再升级恢复 | 生产回滚演练 |

任何 secret、真实 `acc_id`、持仓、现金、MMF、NAV 或原始 broker payload 均不得写入 Git、本文、质量状态、命令输出归档或飞书告警。

## 2. 发布和回滚锚点

| 系统 | 当前生产 | 目标 | 目标代码必须包含 | 首次回滚锚点 |
|---|---|---|---|---|
| OM | `1.4.30` | `1.4.31` | `feat/quality-monitoring@c5cc3384` | `1.4.30` |
| PM | `v0.1.26@c6288e7` | `v0.1.27` | `feat/quality-monitoring@c66422a` | `v0.1.26@c6288e7` |
| Hub | 未安装 | `v0.2.0` | `main@bcb583a` | 停止 Hub 并恢复部署前备份 |

发布后必须记录最终 tag SHA，并证明目标 tag 包含表中提交。tag、Release、安装包 SHA 或版本任一不一致立即停止。

## 3. 证据记录规则

每一步只保存以下脱敏证据：

- UTC 时间、release/tag SHA、包 SHA；
- systemd unit 的 active/enabled/result；
- HTTP 状态码、Schema/producer/instance/version；
- dataset/check ID、scope 的 account label/market、status、reason code；
- snapshot/receipt 的脱敏 ID、hash、count、as-of、completeness；
- incident fingerprint/ID、通知类型/ID、delivery state；
- 回滚前后版本、受隔离消费者和验证结果。

不得保存原始 `/quality/status` 全量文件，除非先通过脱敏检查。所有生产命令输出先在受控终端审阅，再提取上述字段。

## 4. Step 0 — 上线前只读闸门

### 4.1 本地发布物

必须同时满足：

- OM 工作区干净，`VERSION=1.4.31`，release metadata、dependency graph、focused/full tests 通过；
- PM `feat/quality-monitoring` 工作区干净，`VERSION=0.1.27`，完整 tests 与 touched Ruff 通过；
- Hub `main` 工作区干净，`version=0.2.0`，tests/Ruff/compileall 通过；
- canonical Schema 四份副本 SHA 相同；
- Hub wheel 从已提交源码隔离构建、安装和 import 成功。

### 4.2 生产现状

只读记录：

```text
systemctl is-system-running
systemctl --failed
systemctl list-timers --all
OM current version/tag/SHA
PM current version/tag/SHA
Hub absent/current version
loopback listeners
```

若有正在运行的 PM 写任务、OM tick、升级或维护任务，不开始发布/部署。当前已知 `portfolio-futu-evening.service` 失败和 `partial_write_possible=true` 必须保留为 preexisting 事实，不得因升级清除。

## 5. Step 1 — 发布三仓库

需要 `AUTH-REL`。

### 5.1 OM

按 [RELEASE_PROCESS.md](../RELEASE_PROCESS.md) 执行 VERSION-driven Release：

1. 将已验证质量分支合并到目标 `main`，不改写无关提交；
2. 推送后等待 `release-from-version` workflow；
3. 验证 `v1.4.31` tag、Release 非 draft、tag SHA 包含 `c5cc3384`；
4. 重新渲染并核对只含 `1.4.31` 的 Release Notes。

不得在本步骤升级生产。

### 5.2 PM

1. 将 `feat/quality-monitoring@c66422a` 合并到目标 `main`；
2. 完整测试通过后创建并推送 `v0.1.27`；
3. 等待 tag-triggered Release workflow；
4. 验证 Release 非 draft、tag SHA 包含 `c66422a`。

不得在本步骤运行 Futu 写同步。

### 5.3 Hub

1. 创建私有仓库 `investment-quality`；
2. 推送 `main@bcb583a`；
3. 创建 `v0.2.0` tag 和非 draft Release；
4. 上传从该 tag 隔离构建的 wheel及其 SHA-256；
5. 从 Release 重新下载、校验 SHA、隔离安装并确认 `investment_quality.__version__ == 0.2.0`。

## 6. Step 2 — 部署 Hub scaffold

需要 `AUTH-SVC`。本步保持所有 producer、Watchdog 和 dead-man `not_onboarded`。

建议稳定布局：

```text
/opt/investment-quality/
  current -> releases/0.2.0
  releases/0.2.0/
/etc/investment-quality.env
/var/lib/investment-quality/
```

要求：

1. 创建专用 `investment-quality` 用户/组，不授予 OM/PM 数据库权限；
2. release 目录只来自已验证的 `v0.2.0` 包；
3. `.venv` 安装锁定的 wheel；
4. env 文件 `0600 root:root`，使用相互独立的 Hub read/operator、OM producer、PM producer token；
5. 初始配置：

```text
IQ_BIND_HOST=127.0.0.1
IQ_OM_ONBOARDED=false
IQ_PM_ONBOARDED=false
IQ_WATCHDOG_ONBOARDED=false
IQ_DEADMAN_ENDPOINT=
IQ_DEADMAN_TOKEN=
```

6. 用 `scripts/install_linux.py` 渲染 unit 到临时目录；
7. `systemd-analyze verify` 通过后才安装 unit；
8. 启用 `investment-quality.service`，暂不启用 Watchdog timer；
9. 验证：

```text
127.0.0.1:8785 监听
GET /health 成功
iq check 成功
OM/PM component=not_onboarded
incident/outbox 均为空
没有飞书通知
```

## 7. Step 3 — PM producer、账户映射和只读 baseline

需要 `AUTH-SVC`；本步不需要 `AUTH-DATA`。

### 7.1 升级前

1. 备份 `/etc/portfolio-management/config.yaml`、env 和 systemd unit；
2. 记录 `v0.1.26@c6288e7`；
3. 确认 `portfolio-nav-daily.service`、`portfolio-futu-evening.service` 未运行；
4. 保留失败 journal 和既有 sync evidence；
5. 预演 checkout/install/systemd 变化，不修改配置和服务。

### 7.2 部署但不 onboard

1. checkout 已验证的 `v0.1.27`；
2. 安装依赖；
3. 保持 `quality.onboarded=false`；
4. 配置独立 `quality.read_token`、`quality.accounts=[lx,sy]`；
5. 使用只读入口在本机发现账户：

```text
pm futu accounts --market US --json
```

输出包含敏感 `acc_id`，只允许操作者现场查看。将核实后的 REAL 账户分别写入 `futu.accounts.lx/sy`，要求：

```text
trd_env=REAL
trd_market=US
cash_currency=CNH
acc_id 显式、存在、唯一且两账户不重复
```

6. 运行 `pm config doctor --require-futu --require-quality --json`；
7. 先执行 installer dry-run，再启用 PM API 和独立 15 分钟 quality timer；
8. API `/health` 与鉴权 `/quality/status` 分别验证。

### 7.3 只读 baseline

对 `lx`、`sy` 分别执行 full sync dry-run；不得带 `--write`：

```text
pm futu sync --account <account> --dry-run --no-service --json
```

只记录：

- 账户 fingerprint/env/market/source currency；
- source snapshot complete、pagination、position count、payload hash；
- positions/cost/cash/MMF diff count 和 verdict；
- PM replica/NAV/price/FX/finality reason code；
- 当前失败或历史异常的 `preexisting=true` 分类。

dry-run 不建立可冒充成功同步的 durable receipt；它只用于审阅差异。若账户、方向、分类、空快照、现金字段或 MMF 字段不确定，停止，不进入真实同步。

## 8. Step 4 — PM 真实同步、可信证据和 onboarding

需要 `AUTH-DATA`、`AUTH-NOTIFY` 和 `AUTH-SVC`。

### 8.1 业务写入

仅对已批准账户逐个执行：

```text
pm futu sync --account <account> --write --confirm --no-service --json
```

每个账户必须：

1. 使用同一完整 OpenD generation 写 positions、securities cash、fund MMF；
2. 三阶段均成功；
3. 写后立即 readback；仅不一致时 30 秒后只读复查；
4. `partial_write_possible=false`；
5. reconciliation 四个 dataset 均 trusted；
6. 真实 PM 同步回执按既有机器人规则投递；
7. 质量 artifact 刷新后，旧的 `DatetimeFieldConvFail` 不再出现。

任一账户失败即停止后续 onboarding；不得重复写来“碰运气”，先按 receipt 指明的数据集诊断。

### 8.2 PM onboarding

成功同步且 baseline 已接受后：

1. 将 PM `quality.onboarded=true`；
2. 将 Hub `IQ_PM_ONBOARDED=true`；
3. 重启 PM API/Hub，主动刷新一次 PM artifact；
4. 等待 Hub 成功 poll；
5. 比较 PM CLI、PM API、Hub component/dataset 的 as-of、status、reason；
6. 验证本地正式 NAV gate 读取本地 artifact，不依赖 Hub 在线；
7. 未 trusted 的 required dataset 必须立即阻断正式 NAV。

不设置观察期，不提供 bypass。

## 9. Step 5 — OM producer、baseline 和 onboarding

需要 `AUTH-SVC`。

### 9.1 部署但不 onboard

1. 用 `om update apply` 先 dry-run，再确认升级到已发布 `1.4.31`；
2. 保持 `OM_QUALITY_ONBOARDED=false`；
3. 配置独立 `OM_QUALITY_READ_TOKEN`；
4. 用当前 service profile 加 `--include-quality-monitoring` 只读渲染；
5. 审阅 unit diff、`systemd-analyze verify` 后安装并启用：
   - quality HTTP；
   - 15 分钟 refresh；
   - 1 分钟 due probe；
   - US/HK 日终 deep reconciliation；
6. 验证原 trade intake、OpenD、tick、Feishu/WeChat 服务没有漂移。

### 9.2 只读 baseline

执行一次 US+HK 权威 refresh：

```text
om quality refresh --config-key us --config-key hk
om quality status --json
```

验证：

- intake/checkpoint/unresolved；
- full replay 与 materialized projection；
- OpenD option positions；
- 1/5 分钟收敛；
- lifecycle pending/stale/legacy gap；
- 已知 11 条历史 stale 明确分类，不自动修复；
- 普通候选扫描不被无关持仓异常阻断。

### 9.3 OM onboarding

baseline 接受后：

1. 设置 `OM_QUALITY_ONBOARDED=true`；
2. 设置 Hub `IQ_OM_ONBOARDED=true`；
3. 重启 OM quality HTTP/相关常驻消费者和 Hub；
4. 刷新 artifact 并等待 Hub poll；
5. 验证 close advice、正式 option performance 按账户/市场 fail closed；
6. 验证 Hub 自动启用既定 PM/OM 跨服务依赖，不增加隐藏配置开关。

## 10. Step 6 — Watchdog、真实飞书与 dead-man

需要 `AUTH-NOTIFY`、`AUTH-DEADMAN` 和 `AUTH-SVC`。

### 10.1 Watchdog 权限

远端若无 `setfacl/getfacl`，安装 `acl` 属于额外系统包变更，必须包含在授权中。使用目录 traverse ACL 和 quality 子目录 default ACL，使原子替换后的 artifact 仍可读取：

- Hub 用户只能 traverse OM/PM runtime 父目录；
- 只能 read/stat 两个 quality artifact 目录/文件；
- 不得读取 SQLite、holdings、trade events、NAV 或其他 output。

先验证 `sudo -u investment-quality`：

- 能读取两份质量 artifact；
- 不能读取 OM ledger/PM 数据库；
- 能查询列入 allowlist 的 systemd unit/timer。

再写入精确 Watchdog target JSON，运行一次 `iq watchdog`，审阅脱敏 artifact 后启用 timer并设置 `IQ_WATCHDOG_ONBOARDED=true`。

### 10.2 真实飞书 incident/recovery

使用同一飞书机器人，但 notification type 保持独立。受控故障选择 `portfolio-quality-refresh.timer`：

1. 先刷新 PM artifact；
2. 停止该 timer，不停止 PM OpenD/业务同步；
3. 运行 Watchdog并等待 Hub poll；
4. 验证只产生一个 account-safe incident 和一次飞书 incident；
5. 在 artifact 变 stale 前恢复 timer，主动 refresh、Watchdog、Hub poll；
6. 验证同一 fingerprint recovered，并只发送一次 recovery；
7. 验证 outbox retry/dedup 和 `RT-HUB-002` 仍健康。

### 10.3 External dead-man

1. endpoint 必须为无 query/fragment/userinfo 的 HTTPS URL；
2. token 只进入 root-only env；
3. provider 的 15 分钟 missed-heartbeat 告警必须在 Hub 外部；
4. 正常验证 payload 只有 `service`/`alive`；
5. 经 `AUTH-RB` 停止 Hub超过 provider 阈值，验证外部告警；
6. 恢复 Hub并验证 heartbeat/recovery。

## 11. Step 7 — Retention、权限、性能和回滚

### 11.1 Retention 与权限

- scheduler 每次成功 poll 调用 `purge_expired`；
- 验证 normal 30 天、blocking/control 400 天；
- active incident、pending/failed outbox 不删除；
- DB/env/artifact/backup 权限符合设计；
- retention canary 只写 Hub 控制面测试记录，不写 OM/PM 业务数据。

### 11.2 性能

比较上线前后：

- OM tick、trade intake、quality refresh/deep refresh；
- PM sync、quality refresh、NAV；
- Hub poll/dispatch；
- OpenD 请求频率和超时。

不得以性能理由降低完整性或放宽门禁。

### 11.3 首次回滚规则

首次上线的旧 OM/PM 版本尚无本地质量门禁，因此：

- producer rollback drill 必须在对应 producer onboarding 前先执行一次；
- onboarding 后若必须回退到 pre-quality 版本，先隔离正式消费者：
  - PM：停止 `portfolio-nav-daily.timer` 和 PM API；保留 evening OpenD 同步仅在已验证旧路径安全时运行；
  - OM：停止 US/HK tick 和 Feishu/WeChat 正式建议入口；保留 trade intake/OpenD；
- Hub 保留 incident 并把对应 producer 标为 unavailable/incompatible；
- 业务数据、trade events、holdings、cash/MMF、NAV、receipt 不随代码回滚；
- 修复版重新上线、artifact 新鲜且 required dataset trusted 后，才恢复消费者。

OM 使用受控 `om update rollback`；PM checkout 已记录的 `v0.1.26@c6288e7` 并重装依赖；Hub 切回备份 symlink或停止服务。所有回滚均先 dry-run/备份，逐项验证版本、服务、数据不变量和恢复路径。

## 12. 最终完成闸门

Phase 5 只有在以下项目全部有当前生产证据时才能完成：

- 三个 Release/tag/package 身份一致；
- Hub/PM/OM/Watchdog/dead-man 正常；
- `lx`/`sy` 显式唯一 REAL 映射；
- PM 当前完整 sync receipt 和四类 reconciliation trusted；
- OM 当前 intake/replay/OpenD/lifecycle 状态已分类；
- producer CLI、producer API、Hub 结论一致；
- PM 部分写入不再被 `/health=ok` 掩盖；
- 真实 incident/recovery、dedup/retry 和 external missed-heartbeat 已验证；
- retention、权限、性能和 pre/post-onboarding rollback 边界已验证；
- 系统能够按账户、数据集、as-of 和 evidence 回答“现在是否健康、数据是否可信”。

缺少任一项时，[implementation-status.md](implementation-status.md) 必须保持 Phase 5 未完成。
