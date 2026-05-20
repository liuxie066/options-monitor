# Getting Started

这份文档从“已经安装好代码”开始，目标是让普通用户把 OM 第一次安全跑起来。

还没安装时先看 [INSTALL.md](INSTALL.md)。

---

## 1. 先做只读检查

```bash
./om setup check
```

`setup check` 只读。它不会写配置、不会写 env-file、不会启动服务、不会创建定时任务、不会连接 OpenD 或 Feishu。

它会检查：

- repo / venv / Python 依赖是否完整
- `config.us.json` / `config.hk.json` 是否存在且可校验
- env-file 是否可解析，Feishu Bot 和写入开关是否配置
- runtime root 和期权持仓 SQLite 路径
- 本机是否已有 systemd/launchd service 或 timer
- 下一步应该运行什么命令

如果要忽略本地 `.env/options-monitor.env`，做一次隔离检查：

```bash
./om setup check --no-local-env-file
```

---

## 2. 初始化 runtime config

推荐入口：

```bash
./om setup init --market us --account lx --futu-acc-id <futu-account-id>
./om setup init --market hk --account lx --futu-acc-id <futu-account-id>
```

旧入口仍保留兼容：

```bash
./om setup --market us --futu-acc-id <futu-account-id>
./om init runtime --market us --futu-acc-id <futu-account-id>
```

初始化后先校验配置：

```bash
./om config validate --config-path config.us.json --market us
./om config validate --config-path config.hk.json --market hk
```

---

## 3. 配置 env-file

真实凭证放 env-file，不放 runtime config。

本地默认路径：

```bash
.env/options-monitor.env
```

Linux 推荐路径：

```bash
/etc/options-monitor/options-monitor.env
```

先复制示例，再按需填写：

```bash
mkdir -p .env
cp -n configs/examples/options-monitor.env.example .env/options-monitor.env
./om settings doctor
```

`settings doctor` 会脱敏显示来源和缺失项。

---

## 4. 跑系统诊断

```bash
./om doctor --config-key us
./om doctor --config-key hk
```

也可以直接看运行状态：

```bash
./om status --config-key us
./om runs --limit 10
```

---

## 5. 可选：Feishu long-connection

Feishu Bot 走同一组 `OM_FEISHU_BOT_*` env 设置。配置后先做只读检查：

```bash
./om inbound feishu-ws --check
```

长期运行时才需要 service 化；不要在安装或初始化阶段自动启动。

---

## 6. 可选：长期运行服务

本地临时使用可以手动跑：

```bash
./om run tick --config config.us.json --accounts lx
```

服务器长期运行先 render 服务文件：

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --include-feishu-ws \
  --output-dir /tmp/options-monitor-service
```

`service render` 只生成文件和安装命令，不会自动 install、enable 或 start。确认后再按输出的命令安装和启用。
