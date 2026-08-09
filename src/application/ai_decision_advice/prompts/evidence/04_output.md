输出合同：

1. 只输出一个严格 JSON 值，符合调用方给出的 JSON Schema；不输出任何解释性
   文字、Markdown 或代码围栏。
2. 每条证据包含：topic、claim（事实主张）、event_status（developing /
   resolved / expired）、event_time、source（title / publisher / url /
   published_at）。
3. 如果可靠来源相互冲突，把每条冲突主张分别作为独立证据输出，并在 claim
   中如实陈述冲突。
4. 如果没有发现符合来源标准的新证据，输出空 evidence 列表；这是正常的
   搜索完成结果。
5. 搜索覆盖的是输入给出的查询 cutoff 之后的增量信息；新标的首次搜索时
   覆盖最近 30 天以及仍在持续的历史事项。
