## Failure

一开始把版本对象做成 `dataclass(order=True)`，直接依赖元组字段排序，导致预发布版本和稳定版本的优先级不符合 semver 规则。

## Lesson

版本比较不要偷懒依赖结构体默认排序；需要显式比较函数，尤其是包含 prerelease 规则时。
