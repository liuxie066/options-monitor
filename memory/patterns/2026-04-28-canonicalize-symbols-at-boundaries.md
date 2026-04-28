## Pattern

对 symbol / underlier 做 alias 解析时，只在输入边界 canonicalize 一次，然后让后续业务层只消费 canonical symbol。

## Why it worked

- 把高风险问题集中在少数入口修复，而不是在整个仓库大规模替换 `upper()`。
- OpenD、Futu、watchlist、持仓、trade normalize 等链路能共享同一 alias 规则，不再各写一份市场判断。
- 系统级 contract test 可以直接锁住 `POP -> 9992.HK` 这类关键别名，不依赖具体实现细节。
