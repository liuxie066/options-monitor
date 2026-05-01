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
python3 scripts/validate_config.py --config configs/examples/config.example.us.json
./om-agent spec
```

同时确认：

- `VERSION` 正确
- `CHANGELOG.md` 中存在对应版本段落
- README 与 Agent 文档没有明显过期命令
- 更新检查功能读取远端 `origin` 的 Git tags，并与本地 `VERSION` 比较

---

## 打 tag

```bash
VERSION="$(cat VERSION)"
git tag "v${VERSION}"
git push origin main
git push origin "v${VERSION}"
```

如果只是准备版本元数据，还没有准备发布，不要提前打 tag。正式发布时 tag 必须与 `VERSION` 完全一致，只是多一个 `v` 前缀。

---

## 发布后关注点

- `./om-agent spec` 输出是否正常
- 示例配置是否仍能通过 `validate_config.py`
- Agent/tool 合同测试是否通过
