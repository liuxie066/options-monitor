## Context

仓库代码已直接依赖 `import futu`，但安装依赖声明缺少 Futu Python SDK，已有 `.venv` 的轻量启动脚本也不会检测该依赖是否缺失。

## Decision

将 `futu-api` 加入 `requirements.txt`，并把 `run_watchlist.sh` 的启动前依赖检测扩展到 `import futu`。

## Rationale

- 保持改动最小，复用现有 `pip install -r requirements.txt` 流程。
- 满足“如果没有就安装一个”的要求，覆盖首次安装与已有虚拟环境补装两种场景。
- 不额外引入新的安装框架或专用脚本。
