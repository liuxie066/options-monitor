## Context

需要增加版本更新检查功能，同时保持本地 `VERSION` 作为唯一版本真源，不引入新的发布配置。

## Decision

使用远端 `origin` 的 Git tags 作为“最新版本”来源，通过共享应用层服务同时供 CLI 与 WebUI 调用。

## Rationale

- 与现有发布流程一致，仓库已经约定 `VERSION` + `v<version>` tag。
- 不依赖 GitHub Releases 或额外配置，部署面最小。
- CLI 和 WebUI 共用同一比较逻辑，减少分叉和展示不一致。
