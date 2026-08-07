# Release Process

这份文档只面向维护者。

## 开发与发布分离

`main` 是下一个版本的可发布候选，生产环境只消费已经发布的 release tag，不追随 `main`。

每个完整开发单元应当：

1. 在独立分支实现并通过对应测试；
2. 在 `CHANGELOG.md / Unreleased` 中记录需要对外说明的变化；
3. 提交、推送并合并到 `main`；
4. 不修改 `VERSION`，不创建 tag 或 GitHub Release，也不升级生产。

积累到一个完整批次后再执行独立发布。默认可以按一个完整主题、2–5 个有意义的变化或不超过
一周的等待时间组成批次；生产阻断、安全问题和高严重度缺陷可以单独走 hotfix。

发布说明以人工确认的 `Unreleased` 为语义真源，不从 commit message 自动猜测或改写。
纯内部重构如果没有用户或运营价值，可以只保留在提交历史中。

## 版本规则

- 稳定版：`MAJOR.MINOR.PATCH`
- 预发布版：`MAJOR.MINOR.PATCH-<label>`
- Git tag 必须带前缀 `v`

`VERSION` 是版本真源。

### 自动版本建议规则

发布意图写在 `CHANGELOG.md` 的唯一 `## Unreleased` 区段中。自动分类只接受以下三级标题和单行 bullet：

```markdown
## Unreleased

### Breaking Changes
- 删除或不兼容地改变公开契约。

### New Features
- 增加向后兼容的用户或运营能力。

### Improvements
- 改进已有功能的体验、性能、可读性、可靠性或操作效率。

### Bug Fixes
- 修复实际行为与预期契约不一致的问题。
```

推荐优先级：

- `Breaking Changes` 非空：`major`；
- 否则 `New Features` 非空：`minor`；
- 否则只有 `Improvements` / `Bug Fixes`：`patch`；
- `Unreleased` 为空或包含未知标题、普通段落、嵌套列表等无法归类内容：返回 `needs_input`，不猜版本。

分类边界：

- `New Features`：用户或运营人员可以完成以前不能完成的事情；内部新增类、字段或测试工具不自动算新功能。
- `Improvements`：已有行为本来正确，现在变得更清晰、更快、更稳定或更容易操作。
- `Bug Fixes`：修复已经存在的错误、遗漏、重复、错误计算或状态不一致。
- `Breaking Changes`：删除公开能力，或者以旧调用无法继续工作的方式改变命令、配置或工具契约。

历史版本中的 `Added` / `Changed` / `Fixed` 保持不变；新分类只用于 `Unreleased` 和未来版本。

先只读预览：

```bash
./om-agent run --tool version_update --input-json '{"bump":"auto","apply":false,"remote_name":"origin"}'
```

工具会基于指定 remote 的最高稳定 tag、当前 Git 工作区和 `Unreleased` 返回建议版本与
`recommendation_digest`。确认建议后，原样带回 preview 的 base、target 和 digest：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true ./om-agent run --tool version_update --input-json '{
  "bump":"auto",
  "apply":true,
  "confirm":true,
  "remote_name":"origin",
  "recommendation_digest":"sha256:<preview digest>",
  "expected_base_version":"1.3.0",
  "expected_target_version":"1.4.0"
}'
```

apply 会重新计算证据；发生变化时返回 `stale` 且不写入。成功时只更新 `VERSION`，不会修改
Changelog、commit、push、创建 tag、发布 GitHub Release 或升级生产。正常发布还必须把
已确认的 `Unreleased` 内容移动到 `## <version> - <date>`，并保留一个空的 `## Unreleased`。手动
`bump=patch|minor|major` 与 `target_version` 流程保持可用。

---

## 准备发布

从最新、干净的 `origin/main` 开始：

1. 确认当前 `VERSION` 与远端最新稳定 tag 一致；
2. 检查从最新 tag 到 `main` 的所有提交，确认 `Unreleased` 没有遗漏、重复或错误分类；
3. 运行只读自动版本建议并由维护者确认 major、minor 或 patch；
4. 生成 `release/coverage/v<version>.json`，逐项完成 commit 与 Release Notes 的映射；
5. 将 `Unreleased` 移入日期化的目标版本段落，并更新 `VERSION`；
6. 渲染最终 Release Notes，确认只包含目标版本且分类顺序正确；
7. 运行发布前检查；
8. 把 `VERSION`、`CHANGELOG.md` 和 coverage manifest 作为唯一的
   `chore: release <version>` 提交推送到 `main`。

Delta coverage manifest 不是从 commit message 猜 Release Notes。它先确定上一稳定 tag 和
当前 `HEAD`，生成完整 commit inventory，并复制已经人工维护的 `Unreleased` 条目。维护者必须：

- 为每条 `release_notes[]` 填入一个或多个对应的完整 commit SHA；
- 对确实没有用户、配置、runtime 或运营影响的 commit，在 `no_release_note[]` 中写完整 SHA
  和非空理由；
- 不得把有对外影响的 commit 归入 `no_release_note`；
- 如果审阅后 `HEAD` 或 Changelog 又变化，使用 `--refresh` 重新生成 inventory 并复核。

准备 manifest：

```bash
TARGET_VERSION="1.6.1"
./.venv/bin/python scripts/release_delta.py --target-version "${TARGET_VERSION}"

# HEAD 或 Changelog 变化后刷新；仍然有效的映射会保留
./.venv/bin/python scripts/release_delta.py \
  --target-version "${TARGET_VERSION}" \
  --refresh
```

发布门禁会双向核对：上一稳定 tag 到审阅 `HEAD` 的每个 commit 都必须有处置，目标
Changelog 的每条 Release Note 也必须映射到 commit。审阅后最多只能再有一个严格命名为
`chore: release <version>` 的直接子提交，而且它只能修改 `VERSION`、`CHANGELOG.md` 和对应
coverage manifest；这可以阻止审阅完成后夹带代码。

Release Notes 预览：

```bash
VERSION="$(cat VERSION)"
./.venv/bin/python scripts/release_check.py \
  --tag "v${VERSION}" \
  --require-current-taxonomy \
  --require-delta-coverage \
  --render-notes-out /tmp/options-monitor-release-notes.md
```

输出分类顺序固定为：存在时的 `Breaking Changes`、`New Features`、`Improvements`、`Bug Fixes`；
空分类不输出。

---

## 发布前检查

先用只读 advisor 看本次变更建议跑哪些检查：

```bash
./.venv/bin/python scripts/release_test_plan.py --mode standard --base origin/main
```

它只读取 git diff 和 `VERSION`，输出 JSON 计划，不执行测试、不写文件。`--mode fast|standard|full` 用来选择预检强度；如果命中 ledger/position/trade 等高风险路径，计划会显式要求完整 pytest。

常规本地预检先跑统一入口：

```bash
make release-preflight ARGS="--full"
```

这会按快到慢的顺序检查：

- 当前 Python 解释器和 git 工作区状态
- `VERSION` / `CHANGELOG.md` / tag metadata
- 上一稳定 tag 到当前版本的 commit-to-release-note coverage
- `docs/DEPENDENCY_GRAPH.md` 是否过期
- agent plugin focused tests
- 完整 pytest（传 `--full` 时）

如果只是想先看 metadata / dependency graph / focused tests，可省略 `--full`。如果需要在提交后确认发布 commit 没有额外本地改动：

```bash
make release-preflight ARGS="--full --require-clean"
```

展开后的手工命令如下：

```bash
VERSION="$(cat VERSION)"
./.venv/bin/python scripts/release_check.py \
  --tag "v${VERSION}" \
  --require-current-taxonomy \
  --require-delta-coverage
./.venv/bin/python scripts/generate_dependency_graph.py --check
./.venv/bin/python tests/run_smoke.py
./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
./.venv/bin/python -m pytest \
  tests/test_config_yaml.py \
  tests/test_config_template_inheritance.py \
  tests/test_config_authoring_transaction.py \
  tests/test_runtime_config_identity.py \
  tests/test_service_deploy.py \
  tests/test_inbound_control.py \
  tests/test_setup_check.py \
  tests/test_cli_operator_commands.py
./om config init --dry-run --output /tmp/options-monitor-config.yaml --runtime-output-dir /tmp/options-monitor-runtime-config
./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run
./om-agent spec
```

同时确认：

- `VERSION` 正确
- `CHANGELOG.md` 中存在对应版本段落
- `release/coverage/v<version>.json` 完整覆盖上一稳定 tag 以来的所有 commit 和目标版本所有
  Changelog 条目
- README 与 Agent / Tool Gateway 文档没有明显过期命令
- 更新检查功能读取远端 `origin` 的 Git tags，并与本地 `VERSION` 比较

---

## 自动发布

合并到 `main` 的版本提交如果修改了顶层 `VERSION`，GitHub Actions 会自动：

- 读取 `VERSION` 生成 `v<version>` tag
- 精确匹配对应的日期化版本段落并严格校验新分类
- 使用完整 Git 历史验证 commit-to-release-note coverage，不接受漏项、无理由排除或审阅后夹带代码
- 渲染只包含目标版本的 Release Notes
- 运行 smoke / agent plugin 测试
- 发布对应 GitHub Release

因此常规发布只需要把版本元数据改好并推到 `main`；不需要再手动补打上同名 tag。
普通开发提交因为不修改 `VERSION`，不会触发这条发布工作流。

如果 VERSION push 已触发发布但门禁失败，应先在 `main` 修复根因并重新完成 release delta
审阅与发布前检查，再从 GitHub Actions 手动运行 `Release from VERSION`。手动入口使用当前
`main` 的 `VERSION` 和提交 SHA，仍执行同一套 metadata、coverage、测试、归档和发布步骤；
不得用它跳过失败门禁或从未审阅的提交补发 tag。

---

## 远端自动升级

远端升级只消费已经发布成功的 GitHub release tag，不追 `main`。

推荐部署布局：

```text
/opt/options-monitor/
  releases/
    1.2.68/
    1.2.69/
  current -> releases/1.2.69

/var/lib/options-monitor/
  service.profile.json
  upgrade_status.json
  locks/upgrade.lock
```

升级检查只读：

```bash
./om update check \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor
```

发布或升级后的 compact 验证只读：

```bash
./om update verify \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --no-check-latest
```

`update verify` 汇总当前 symlink、版本、runtime config freshness、事件源配置、最近 upgrade status 和长期 service health；`--no-check-latest` 会跳过 git tag 查询，适合 release 已确认后快速复核远端状态。`upgrade.status` / `upgrade.last_status` 表示最近一次升级结果，`upgrade.has_status_record` 表示是否存在 `upgrade_status.json`；是否有新版本只看 `version.upgrade_available`。

升级默认 dry-run：

```bash
./om update apply \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor
```

确认升级时才会从本机 git cache materialize release、在新 release 内准备 `.venv`、安装 runtime/server 依赖、校验新目录、从 profile 记录的 YAML authoring source 重建并校验 runtime config、切换 `current` symlink，补齐当前 release 新增的缺失 service/timer，并按升级前 `service.profile.json` 重启长期运行的 service：

```bash
./om update apply \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --confirm
```

默认不自动跨 major；需要跨 major 时显式传 `--allow-major`。

升级会根据 `/var/lib/options-monitor/service.profile.json` 里的 `markets` / `config_paths` 重建 runtime config。profile 必须记录 `config_authoring.source=yaml` 和 `config_authoring.config_yaml`，升级时执行 `./om config build --source yaml --config-yaml <path>` 重建对应 market 的 runtime config。旧 profile 缺少 YAML authoring source 时会 fail closed；先用 `config migrate-yaml` 完成一次性迁移，再重新 `service render --config-yaml <path>`。切换 symlink 前缺失来源或 rebuild/validate 失败时会在 `upgrade_status.json` 写入 remediation。切换 symlink 后会再用 current symlink 重建/校验一次，保证 tick 看到的 runtime config freshness 与当前代码一致。

切换 symlink 后会执行 service drift reconcile：当前 release 的 `render_service_bundle()` 是期望状态，旧 profile 只提供账号、市场、env file、deploy user、Feishu WS、auto-upgrade 和已显式收编的 Feishu Agent credential 等部署意图。reconcile 会写入缺失的 systemd unit/profile 和 profile-owned helper/drop-in、修复 helper 模式、`daemon-reload`，并启用缺失 timer 或 credential oneshot。credential oneshot 还要求 `Result=success`。升级流程随后会用 reconcile 后的 profile 重启长期 service，并检查 `is-active` / `is-enabled`；Feishu WS 还会额外执行 `./om inbound feishu-ws --check`。`./om service drift --runtime-root /var/lib/options-monitor` 是同一逻辑的只读检查，`--confirm` 才会应用修复。

存量主机如果仍使用 `/usr/lib/systemd/system/options-monitor-feishu-agent-credential.service`，首次升级到包含 repository-owned credential 资产的 release 后，必须用新 release 显式执行一次 `service drift` dry-run 和 `--confirm`。原因是升级进程由旧 release 启动，无法在同一次切换中可靠使用尚未加载的新 reconcile 逻辑。收编后 profile 会保留 `feishu_agent_credential` opt-in，后续手动升级、自动升级和回滚都按同一契约 reconcile。在旧 release 回滚窗口结束前保留 `/usr/lib` legacy unit，不由 drift 自动删除。

当 profile 已收编 Feishu Agent credential 时，升级后 Feishu WS 健康检查会在 `sudo` 进程内显式合并 profile 的基础 `env_file` 和 `feishu_agent_credential.runtime_env_file`。命令行只传文件路径，不传或输出明文凭据；这避免 `sudo` 清理父进程环境后出现假性 `missing Feishu app credentials`。

如果 systemd unit 使用 `User=<deploy_user>` 运行自动升级，`service render` 会在 profile 中标记 trade-intake 和 Feishu WS 等长期服务重启使用 `sudo -n systemctl restart ...`。服务器需要给运行用户配置最小 sudoers 授权，例如：

```sudoers
liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service
liuxie ALL=(root) NOPASSWD: /usr/bin/systemctl restart options-monitor-trade-intake.service
liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-feishu-ws.service
liuxie ALL=(root) NOPASSWD: /usr/bin/systemctl restart options-monitor-feishu-ws.service
```

如果 release/config 已切换成功但服务重启失败，升级状态会写成 `upgraded_restart_failed`，并记录 `symlink_switched=true`、`config_rebuilt`、`restart_failed_services` 和 `manual_remediation`。这种部分成功状态不会让自动升级 unit 因已知的服务重启权限问题反复 failed；按 remediation 手工重启服务并补齐 sudoers 即可。

升级默认把下载缓存放在 `repo_root` 同级的 `_cache/`，也可用 `OM_UPGRADE_CACHE_ROOT` 或 `--cache-root` 覆盖。代码物料使用 `_cache/git/options-monitor.git`：首次 `git clone --mirror`，后续 `git fetch --tags --prune`，再用 `git archive` 解包到目标 release，因此不会每次重新 clone 完整 tag 工作树。release 目录不保留 `.git`；后续 `update check` 和确认升级会在当前 release 不是 git checkout 时从 `_cache/git/options-monitor.git` 读取 remote 与 release tags。

Release runtime 依赖安装默认使用 `OM_UPGRADE_INSTALLER=auto`：先检测宿主机 PATH 上的 `uv`，可用时把当前运行中的 Python 3.12+ `sys.executable` 传给 `uv venv --python`，再执行 `uv pip install -p .venv/bin/python ...`；不可用或 auto 模式下 uv 安装失败时回退到原 pip 流程。升级流程不会自动安装 uv；需要加速时应在宿主机安装一次。可用 `OM_UPGRADE_INSTALLER=pip` 强制旧流程，或 `OM_UPGRADE_INSTALLER=uv` 强制 uv 且失败即中止升级。依赖下载缓存默认复用 `_cache/uv` 和 `_cache/pip`；只配置了 `PIP_INDEX_URL` 时，升级会把它映射为 uv 命令的 `UV_INDEX_URL`。

release 清理默认 dry-run，不删除文件：

```bash
./om service cleanup \
  --repo-root /opt/options-monitor/current \
  --releases-root /opt/options-monitor/releases \
  --cleanup-downloads \
  --cleanup-pip-cache
```

输出会列出当前 active release、将保留的 release、将删除的旧 release、将清理的缓存目录以及预计释放空间。默认 `--keep-releases 2`，即保留当前版本和最近一个回滚版本；小于 2 的值会被提升为 2。真正删除必须显式确认：

```bash
./om service cleanup \
  --repo-root /opt/options-monitor/current \
  --releases-root /opt/options-monitor/releases \
  --cleanup-downloads \
  --cleanup-pip-cache \
  --confirm
```

清理只处理旧 release 和显式允许的缓存，不会触碰 `/var/lib/options-monitor`、SQLite、`output*`、locks、runtime config、用户 overlay config、当前 active release 或最近一个 rollback release。需要额外清理系统缓存时可加 `--include-apt-cache` 或 `--journal-vacuum-size 64M`。

确认升级成功后也可以追加后置清理：

```bash
./om update apply \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --confirm \
  --cleanup-after-upgrade
```

后置清理只在升级成功、symlink 已切到目标 release、runtime config rebuild/validate 成功、active release 可确认且至少保留 2 个 release 时执行。`--repo-root` 不是 symlink 字面路径时，确认升级会 fail fast，不会提前 clone 到错误的 release 布局。

回滚同样默认 dry-run：

```bash
./om update rollback \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor

./om update rollback \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --to-version 1.2.68 \
  --confirm
```

`./om service render --include-auto-upgrade` 会额外渲染每天北京时间 06:10 的升级 timer。这个开关是显式 opt-in；普通 `service render` 不会默认启用自动升级。自动升级部署应让 `--repo-root` 指向 `current` symlink，并让生产 config 位于 runtime root，例如 `/var/lib/options-monitor/config.yaml`、`/var/lib/options-monitor/config.us.json` 和 `/var/lib/options-monitor/config.hk.json`。使用 YAML authoring 时，render 命令要同时传 `--config-yaml /var/lib/options-monitor/config.yaml`，让 profile 后续驱动 YAML-aware rebuild。

---

## 手动打 tag（补发 / 重跑）

```bash
VERSION="$(cat VERSION)"
git tag "v${VERSION}"
git push origin main
git push origin "v${VERSION}"
```

如果需要补发历史版本，或者需要显式重跑 tag 驱动的发布流程，仍可手动打 tag。正式发布时 tag 必须与 `VERSION` 完全一致，只是多一个 `v` 前缀。

---

## 发布后关注点

- `./om-agent spec` 输出是否正常
- 示例配置是否仍能通过 `./om config validate`
- Tool Gateway/tool 合同测试是否通过
