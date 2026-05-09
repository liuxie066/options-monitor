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
