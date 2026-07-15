# 用纯 ValuePolicy 扩展完整值求解

完整多值求解器由 DispatchThis 核心拥有定义图遍历、PHI、位宽、符号性、标准 BNIL 运算、
循环和完整性。样本插件可以提供纯 `ValuePolicy`，只处理核心不认识的样本特有运算或受控
内存读取；policy 只接收当前表达式和已经完整求出的操作数值，返回 `Handled(values)`、
`NotHandled` 或 `Inconclusive(reason)`。policy 不得自行递归定义图或修改 Binary Ninja，也
不得被保存进恢复事实或计划。它是函数式扩展点，不是规则 DSL 或另一套值引擎。
