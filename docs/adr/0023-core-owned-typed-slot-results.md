# 用核心强类型对象表达槽位结果

六个样本语义槽位只返回 DispatchThis 核心定义的冻结结果对象：`BranchTargetFact`、
`CallTargetFact`、`GlobalDataFact`、`CorrelatedStorePlan`、`StringRecoveryFact` 和
`DeflattenPlan`。结果对象只包含恢复证据、当前 IL 见证或声明式修改意图；核心负责重新验证
并应用。接口不接受裸字典、任意元组、样本插件自定义结果类型、子类或修改 callback，避免
字符串键漂移、缺失字段和样本代码取得修改权。

`CallTargetFact` 从首版公开契约起就以非空、去重的 `targets` 元组携带该站点的完整 callee
集合，而不是单个首选地址。当前后端只应用单元素集合；这项应用限制不改变事实的完整性。

`GlobalDataFact` 携带完整原生 Binary Ninja `Type`，而不是类型名字符串或“只加 const”的布尔
意图。provider 决定并证明指针、数组、结构体及各层 const 语义；核心只验证事实边界并应用，
从而既保留样本灵活性，也不把类型推断移入 workflow。
