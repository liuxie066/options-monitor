# Deploy

`options-monitor` 的服务化部署以同一套运行时契约同时支持 Linux 和 Mac：

- 代码根目录：`repo_root`
- 运行时目录：`runtime_root`
- active option positions SQLite：`<runtime_root>/output_shared/state/option_positions.sqlite3`
- Linux 服务管理：`systemd`
- Mac 服务管理：`launchd`

完整步骤见 [`docs/DEPLOY_LINUX_MAC.md`](docs/DEPLOY_LINUX_MAC.md)。

最小渲染命令：

```bash
./om service render --target systemd --runtime-root /var/lib/options-monitor --env-file /etc/options-monitor/options-monitor.env --markets us hk --accounts lx sy --output-dir /tmp/options-monitor-service
./om service render --target launchd --runtime-root "$HOME/Library/Application Support/options-monitor" --markets us hk --accounts lx sy --output-dir /tmp/options-monitor-service
```

渲染结果只生成服务文件和安装命令，不会自动安装、启动、发送通知或修改生产配置。
