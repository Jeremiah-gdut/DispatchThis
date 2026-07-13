# 对 MLIL 控制流改写使用 copy transformation

DispatchThis 通过 Binary Ninja 的 copy-transformation API 复制当前 function、借由复制后的
source-block label 解析 destination，并经 `AnalysisContext.set_mlil_function()` 安装替换，
以实现会改变控制流边的 MLIL rewrite。分支条件翻译和 deflatten 使用该 backend；仅 expression
改写仍可使用 `replace_expr`。每次使用该 backend 的 transformation 都是原子的：任一选中的
控制流 rewrite 失败或 replacement 无法安装时，DispatchThis 丢弃它，而不是发布部分 graph 或
依赖 replacement 的 receipt。依赖 replacement 的 branch cleanup receipt 与 deflatten
metadata/stability 只在安装成功后发布；安装后的分支 cleanup 必须在该 copy 上局部收敛，且仅
当本轮没有 NOP 时才可发布 receipt。发生 NOP 后由下一工作流轮次从当前 IL 确认 overlay；
同轮 deflatten 只能再次在该 copy 上证明 branch root 为空，不能读取持久化索引。Binary Ninja
5.3 之前的版本和旧 assignment fallback 不在范围内。
验证比较恢复出的 CFG edge 和 target block；检查 HLIL 的可读输出，但不将其视作稳定的文本
interface。这样保留 Binary Ninja 的 label 和 IL-mapping 语义，而不改变 resolver plan、
workflow phase 顺序或 cleanup ownership。
