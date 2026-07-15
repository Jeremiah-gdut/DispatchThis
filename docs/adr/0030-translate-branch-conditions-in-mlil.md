# 在 MLIL 翻译分支条件

分支条件翻译保留在 branch、call 与 global 阶段稳定后的 MLIL activity；LLIL deinbr 只产生
条件分支目标事实。当前 workflow 直到 MLIL 阶段才能确认 call/global 稳定，而 MLIL
copy-transform 可以在同一轮安装并直接供后续 HLIL 使用；下沉到 LLIL 要么提前改变已确认的
阶段顺序，要么额外触发一轮重新分析，均不能简化正确性边界。

translator 只接受两种当前 MLIL 状态：目标投影与回执一致的同源 switch-like
`MLIL_JUMP_TO` 可改写，精确表达回执条件及 true/false 方向的同源 `MLIL_IF` 已满足。其他
MLIL 形态、目标不一致或缺失/歧义映射均按站点失败处理；HLIL 结构不是输入，也不触发上游
IF、CFG 或其他形态的回退搜索。
