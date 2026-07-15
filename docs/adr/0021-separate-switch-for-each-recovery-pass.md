# 为每个恢复 pass 提供独立开关

DispatchThis 为七个用户可见恢复 pass 分别提供开关：间接分支目标恢复、间接调用目标恢复、
全局数据语义恢复、分支条件翻译、路径关联 STORE 恢复、字符串恢复和去平坦化。路径关联
STORE 恢复暂时保留为独立可选 pass；它由样本插件规划、核心验证并原子应用。目标 cleanup、
回执、失效传播、类型调整、注释写入和原子改写属于其上层 pass 的正确性后端，不单独开关。
七个开关沿用当前插件菜单模式：核心通过 `register_for_function` 为当前函数提供菜单项，菜单
回调把状态写入该 Function 的 ResourceScope 设置并安排重新分析；Function Analysis Settings
只作为持久化和 workflow eligibility 机制，不要求用户进入设置面板。

菜单始终维护有效的 pass 依赖闭包：开启下游 pass 会同步开启全部传递前置 pass；关闭上游
pass 会同步关闭所有依赖者，并使其下游恢复证据失效。核心不得在菜单状态为关闭时隐式运行
某个 pass，也不保留必然无法满足的开关组合。

直接依赖边固定为：间接调用目标依赖间接分支目标；全局数据语义依赖间接调用目标；分支条件
翻译依赖全局数据语义；路径关联 STORE 与字符串恢复分别只直接依赖全局数据语义；去平坦化
只直接依赖分支条件翻译。依赖按传递闭包展开，因此去平坦化同时要求 branch/call/global/
translation 开启，但不要求 STORE 或字符串恢复开启；STORE、字符串恢复和条件翻译也不互为
前置。固定 activity 顺序仍保证多个可选 pass 同时开启时按 pipeline 先后运行。cleanup、receipt
和稳定性检查是所属 pass 的内部 readiness gate，不是额外用户开关。
