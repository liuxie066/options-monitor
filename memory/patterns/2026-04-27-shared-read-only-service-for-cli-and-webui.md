## Pattern

对同一只读能力同时暴露给 CLI 和 WebUI 时，先收敛为 `src/application/` 下的共享服务，再让各入口只负责参数接线和结果展示。

## Why it worked

- 结果结构只定义一次，CLI 与 WebUI 不会各自漂移。
- 测试可以优先覆盖纯函数，再补入口薄封装测试。
- 后续如果要接 agent/tool surface，可以直接复用同一服务。
