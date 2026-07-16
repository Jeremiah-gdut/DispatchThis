# 以来源和操作数路径重新绑定站点条件

核心在调用 `set_user_indirect_branches` 前，将跳转源、恢复点条件的 `ILAnchor` 以及有方向目标
保存为函数会话中的稳定标量。`ILAnchor` 只记录 owner 机器来源、source operand、相对操作数
路径及预期 operation/位宽；重新分析后只接受当前 LLIL 的唯一匹配及其唯一当前 MLIL 映射，
不保存旧 IL 对象、instruction/expression index、字符串表示或表达式 DSL。

translator 还要求回执中的两个目标地址分别唯一对应当前 switch-like 跳转的目标块。安装 IF 或
判定 `ALREADY_SATISFIED` 时，只接受 condition 根在当前 LLIL 的唯一重绑及其唯一当前 MLIL
映射。若同一 anchor 恰好命中多个候选，只有它们全都是同 operation/位宽、同顺序的直接
LLIL 比较，且每个寄存器或常量操作数也精确相同，才可视为同一条件；任何嵌套表达式、不同
寄存器/常量或其他逻辑等价关系仍是歧义。后端深拷贝 MLIL 根及其嵌套子表达式，不复制或
内联变量定义，也不触发定义展开、目标重算或通用条件等价求解。
