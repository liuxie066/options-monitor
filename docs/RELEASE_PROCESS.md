# Release Process

这份文档只面向维护者。

## 版本规则

- 稳定版：`MAJOR.MINOR.PATCH`
- 预发布版：`MAJOR.MINOR.PATCH-<label>`
- Git tag 必须带前缀 `v`

`VERSION` 是版本真源。

---

## 发布前检查

```bash
VERSION="$(cat VERSION)"
python3 scripts/release_check.py --tag "v${VERSION}"
python3 tests/run_smoke.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
python3 -m pytest tests/test_layered_config.py
./om config build --market us --user-config configs/examples/user.example.us.json --dry-run
./om config build --market hk --user-config configs/examples/user.example.hk.json --dry-run
./om-agent spec
```

同时确认：

- `VERSION` 正确
- `CHANGELOG.md` 中存在对应版本段落
- README 与 Agent 文档没有明显过期命令
- 更新检查功能读取远端 `origin` 的 Git tags，并与本地 `VERSION` 比较

---

## 自动发布

合并到 `main` 的版本提交如果修改了顶层 `VERSION`，GitHub Actions 会自动：

- 读取 `VERSION` 生成 `v<version>` tag
- 校验 `CHANGELOG.md` 是否存在对应版本段落
- 运行 smoke / agent plugin 测试
- 发布对应 GitHub Release

因此常规发布只需要把版本元数据改好并推到 `main`；不需要再手动补打上同名 tag。

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

升级默认 dry-run：

```bash
./om update apply \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor
```

确认升级时才会下载 tag、在新 release 内准备 `.venv`、安装 runtime/server 依赖、校验新目录、迁移上一 release 的 `configs/user*.json`、重建并校验 runtime config、切换 `current` symlink，并按 `service.profile.json` 重启长期运行的 trade-intake service：

```bash
./om update apply \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --confirm
```

默认不自动跨 major；需要跨 major 时显式传 `--allow-major`。

升级会根据 `/var/lib/options-monitor/service.profile.json` 里的 `markets` / `config_paths` 恢复用户 overlay，然后逐个执行新 release 的 `./om config build` 和 `./om config validate`。overlay 来源按顺序包括 runtime config metadata 记录的 source path、`/var/lib/options-monitor/configs/`、当前 release、以及 `releases/` 下最近一个包含完整 `configs/user*.json` 的旧 release。切换 symlink 前会检查 `user.common.json` 和目标 market 的 `user.hk.json` / `user.us.json`；缺失或 rebuild/validate 失败时会 fail fast，并在 `upgrade_status.json` 写入 remediation。切换 symlink 后会再用 current symlink 重建/校验一次，保证 tick 看到的 runtime config freshness 与当前代码一致。

如果 systemd unit 使用 `User=<deploy_user>` 运行自动升级，`service render` 会在 profile 中标记 trade-intake 和 Feishu WS 等长期服务重启使用 `sudo -n systemctl restart ...`。服务器需要给运行用户配置最小 sudoers 授权，例如：

```sudoers
liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service
liuxie ALL=(root) NOPASSWD: /usr/bin/systemctl restart options-monitor-trade-intake.service
liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-feishu-ws.service
liuxie ALL=(root) NOPASSWD: /usr/bin/systemctl restart options-monitor-feishu-ws.service
```

如果 release/config 已切换成功但服务重启失败，升级状态会写成 `upgraded_restart_failed`，并记录 `symlink_switched=true`、`config_rebuilt`、`restart_failed_services` 和 `manual_remediation`。这种部分成功状态不会让自动升级 unit 因已知的服务重启权限问题反复 failed；按 remediation 手工重启服务并补齐 sudoers 即可。

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

`./om service render --include-auto-upgrade` 会额外渲染每天北京时间 06:10 的升级 timer。这个开关是显式 opt-in；普通 `service render` 不会默认启用自动升级。自动升级部署应让 `--repo-root` 指向 `current` symlink，并让生产 config 位于 runtime root，例如 `/var/lib/options-monitor/config.us.json` 和 `/var/lib/options-monitor/config.hk.json`。

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
- 示例配置是否仍能通过 `validate_config.py`
- Agent/tool 合同测试是否通过
