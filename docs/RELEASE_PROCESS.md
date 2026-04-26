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
python3 scripts/release_check.py
python3 tests/run_smoke.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
python3 scripts/validate_config.py --config configs/examples/config.example.us.json
./om-agent spec
```

同时确认：

- `VERSION` 正确
- `CHANGELOG.md` 中存在对应版本段落
- README 与 Agent 文档没有明显过期命令

---

## 打 tag

```bash
git tag v0.1.0-beta.1
git push origin v0.1.0-beta.1
```

---

## 发布后关注点

- `./om-agent spec` 输出是否正常
- 示例配置是否仍能通过 `validate_config.py`
- Agent/tool 合同测试是否通过
