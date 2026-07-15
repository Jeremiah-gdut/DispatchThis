# Deflatten 前要求函数级条件翻译完备

分支条件翻译按站点独立应用并保留已成功的结果，但任一有效条件回执对应的失败站点都会保持
函数级 branch cleanup 未完成，从而阻止 deflatten，直到该站点成功或其条件回执失效。
`BranchTargetFact.condition=None` 不创建条件回执，因此不属于该门。我们不引入按区域
deflatten，因为它要求每个 `DeflattenPlan` 额外声明并证明其分支站点依赖，会扩大当前函数级
原子改写边界。

这也包括 true/false 最终解析到同一目的地的退化条件目标：它们以去重目标及
`condition=None` 交付，不会因 Binary Ninja 合法折叠为 `GOTO` 而形成永久失败的条件门。
