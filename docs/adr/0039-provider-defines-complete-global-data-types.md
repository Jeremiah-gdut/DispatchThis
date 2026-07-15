# Provider 定义完整全局数据类型

`GlobalDataFact` 直接携带样本 provider 已证明的原生 Binary Ninja `Type`，而不是类型字符串或
仅给现有类型增加 const 的窄操作。provider 因而可以准确表达指针、数组、结构体和每一层 const；
DispatchThis 核心不推断或改造类型，只验证槽位地址、映射范围、类型宽度、重叠/同址冲突和
应用后的精确读回，并独占 `define_user_data_var` 及重新分析时序。完整类型由样本语义拥有，
修改边界由核心拥有，避免为每种新数据形态扩展公共 API，也避免字符串类型在插件间漂移。
