# 使用函数 phase 状态协调 workflow

DispatchThis 通过基于 `Function.session_data` 的函数 phase state 模块协调每函数 workflow
pass。

初始范围是间接分支解析和间接调用解析。Deflatten 与 cleanup 仅会在其 ARM64 特定行为修正后
加入这一 phase 模型。

第一版不迁移 deflatten 或 cleanup 状态；它覆盖间接分支解析的 receipt/stability、间接调用
解析的 receipt/stability，以及分支条件翻译 gate。

每个 workflow phase 同时记录 readiness 与已提交的、会触发重新分析的修改。间接分支解析
记录已应用 user branch metadata 的分支 source。间接调用解析则分别记录已验证的调用目标和
完成的调用类型决策：要么读回了具体 override，要么当前证据表明无需 override。后续 phase
利用这些记录跳过旧修改，只提交新事实。

间接分支解析和间接调用解析各分两层：

- 每次 workflow 运行都可重建的纯解析计划；
- 决定是否提交 Binary Ninja 函数状态编辑的 decision/receipt 层。

间接调用解析依赖间接分支解析稳定。分支条件翻译等待分支、调用和全局常量解析稳定；其
workflow activity 位于全局解析之后，因此 data-var 编辑及其重新分析会先收敛，再安装昂贵
的 CFG overlay。翻译是当前 MLIL 上的展示性改写，不持有 mutation receipt。

Phase cleanup 只在所属 phase 稳定后运行。间接分支解析的 cleanup 在分支条件翻译之后运行，
以便 translator 仍能读取已解析的 `MLIL_JUMP_TO` 形状和它所需的目标解码赋值。间接调用
解析的 cleanup 则在间接调用解析稳定后运行。

间接分支和间接调用的 phase cleanup 会复用现有的 decode-gadget taint/dead-residue 思路，
但不复用完整的 deflatten cleanup pass。Phase cleanup 可以 NOP 纯解码计算；调用 cleanup
还可以 NOP 由明确 plan 持有、且 SSA-dead、位于完整 `call.dest` 定义 slice 中的 load。
它不得折叠控制流、把 xref 作为 ownership 证据，或 NOP deflatten 状态写。

Phase cleanup 必须从所属 phase 的已解析 site 出发，而不是从所有 decode-gadget magic
constant 出发。间接分支 cleanup 以已解析分支目标的解码 site 为根；间接调用 cleanup 以已
解析调用目标的解码 site 为根。这避免在间接调用解析运行前，分支 cleanup 删除调用目标
解码输入。

Cleanup receipt 是函数级的 phase boolean，不按分支 source 或调用 site 分别记录。某个 phase
cleanup 在当前 MLIL 中按其自有精确根重复规划；只有本轮未 NOP 任何内容且计划为空时才将
`cleanup_done` 设为真。计划重复或应用失败会保持 receipt 开放并阻止下游；本轮实际 NOP 后也
必须留待下一工作流轮次从当前 IL 确认 overlay，不得为了确认主动安排重新分析。唯一的同轮
例外是分支 cleanup：deflatten 可对同一当前 MLIL 重算 branch receipt 根，确认为空后读取该
overlay；这不持久化任何根，也不适用于调用 cleanup。
上游 receipt 改变也会使所属 cleanup receipt 失效。

Workflow callback 是编排 seam。pass 模块可以产生计划并执行当前 IL 改写，但 workflow callback
独占 user branch metadata、call type adjustment、analysis completion callback 等会触发
Binary Ninja 重新分析的修改。

函数 phase state 模块暴露 phase 语义操作，而不是原始 dict 或 set。仅将原始 dict access 从
`BinaryView.session_data` 移到 `Function.session_data` 并不够；该模块持有 readiness、
receipt comparison 和下游 invalidation 规则。

仅当所有当前未解析的间接分支 source 都由 user branch metadata 覆盖、本轮没有提交新的
branch mutation、且没有 receipt target 改变时，间接分支解析才稳定。某 source 的解析 targets
与 receipt 不同时，DispatchThis 把它当作下游 invalidation：记录变化、更新 receipt、
重新提交 user branch metadata，并清除依赖它的间接调用解析 receipt。

当函数 phase state 为空、但 Binary Ninja 已具有该函数的 user indirect branch metadata 时，
DispatchThis 从这些 metadata 初始化 branch receipt。这覆盖插件热重载和重新打开的 BNDB，
同时不重复提交同一 branch mutation。

Branch receipt 只有在 read-back 后才能缩窄下一轮识别范围。workflow 将每个 receipt 完整的
规范化 target tuple 与 Binary Ninja 当前的非自动 user branch metadata 比较，只把完全匹配的
项作为已验证 branch frontier。metadata 缺失、自动、为子集/超集或已改变的 receipt 仍留在
识别 frontier。这样可避免增量收敛反复解码 Binary Ninja 已解析的 `LLIL_JUMP_TO` 形状，
同时不会让 session state 自行剪枝当前分支。

这是必要的，因为 Binary Ninja 函数状态编辑可再次调度函数分析并重新进入 workflow。仅有
stable boolean 不够：即使恢复事实未变，重复同一 mutation 也可能使分析持续运行。

`Unresolved Indirect Control Flow` tag cleanup 不属于分支解析稳定性。一旦分支解析稳定，
DispatchThis 便安排 analysis completion callback，从已由 user branch metadata 覆盖的 source
移除这些 tag。

view-level state 仅对 BinaryView 级时序有效，例如与
`BinaryView.add_analysis_completion_event()` 绑定的 tag cleanup pending state。

函数 phase state 与活动 resolver profile ID 绑定。空状态可以重新绑定；已经包含 recovery
evidence 的 legacy 或不匹配状态必须 fail closed；一个 binary profile 的 receipt 不能作为另一个
profile 的证据。

Receipt 用于协调提交，不替代 Binary Ninja 当前事实。分支稳定性会读回 user branch metadata，
具体调用类型 override 通过 `get_call_type_adjustment` 验证，全局稳定性读取当前
data-variable type。完成的 no-override 调用决策不会提交 `None`，也不会与 Binary Ninja
的有效 automatic type 比较。当前 IL 改写还必须具有 ADR-0011 描述的精确 instruction
witness。
