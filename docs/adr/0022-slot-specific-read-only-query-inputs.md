# 用槽位 Query 限制样本语义输入

`SampleSemantics` 的六个 callable 分别接收一个由 DispatchThis 核心定义的冻结槽位 Query，
而不是不一致的位置参数或通用可变 context。每种 Query 只携带该槽位需要的原生
`BinaryView`、`Function`、当前 LLIL/MLIL 和核心只读证据；原生 Binary Ninja 对象按契约
只读。核心绝不向样本插件传递 `AnalysisContext`、Settings、workflow state、receipt 或
session data。样本插件必须把所有修改意图编码为返回的恢复事实或计划。
