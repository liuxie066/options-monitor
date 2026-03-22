# options-monitor 配置说明（config.json）

当前推荐使用根目录的 `config.json` 作为唯一入口配置。

- `templates{}`：策略模板（通用底线/偏好，可复用）
- `symbols[]`：标的清单（每个标的的个性化范围：strike/DTE 等）
- `outputs`：输出数量
- `alert_policy`：提醒分级策略参数
- `portfolio`：持仓/现金/期权占用的来源（目前复用 `../portfolio-management/config.json` + Feishu Bitable 表）
- `notifications`：通知配置（当前会生成 notification 文本；实际“发送”由 OpenClaw 完成）
- `schedule`：扫描与通知节流参数

YAML 配置已进入 legacy（仅保留参考）。
