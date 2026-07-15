# 由核心独占工作流并运行时绑定样本语义

DispatchThis 核心独占 workflow/activity 的创建、排序与注册；样本插件不得插入 activity 或提交 Binary Ninja 修改，只能注册一个返回恢复事实或计划的纯样本语义提供者。核心 callback 在运行时取得该提供者，并独占 AnalysisContext、BinaryView 和 Function 修改、阶段状态、回执、失效传播及时序门控，使外部插件加载顺序无需改变已注册且不可变的 workflow；本决策取代 ADR-0005。
