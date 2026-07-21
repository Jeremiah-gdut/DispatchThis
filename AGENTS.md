# DispatchThis Agent 工作指南

## MUST READ!

每个会话必须先用 BN CLI 精确阅读以下 Binary Ninja 文档：
```bash
bn docs "Binary Ninja Intermediate Language: Overview"
bn docs "Binary Ninja Intermediate Language: Low Level IL"
bn docs "Binary Ninja Intermediate Language: Medium Level IL"
bn docs "Binary Ninja Intermediate Language: High Level IL"
bn docs "Modifying ILs"
bn docs "Important Concepts" --match 'E:\\BN\\docs\\dev\\concepts.html'
bn docs "User Informed Data Flow"
bn docs "Binary Ninja Workflows"
```

## 先做这四件事

1. 阅读 `CONTEXT.md`；若改动涉及 workflow、provider、当前 IL、PHI、cleanup 或 deflatten，先从 `docs/adr/README.md` 定位相关 ADR。
2. 划清修改边界：pass 只能分析、构建计划或改写当前 IL；会触发重新分析的修改只能由 `workflow.py` 的 callback 提交。
3. 只使用当前 `AnalysisContext` 所见的 IL；函数级 receipt 放入 `Function.session_data["dispatchthis_workflow_state"]`。
4. 用精确 witness、最小测试和实际 GUI workflow 验证；改动 activity 注册或 callback 后完整重启 Binary Ninja。

## Agent 技能

### 问题跟踪

本仓库的 issue 位于 GitHub Issues；外部 PR 不是 triage 面。参见
`docs/agents/issue-tracker.md`。

### 分诊标签

使用默认的 mattpocock/skills triage 标签词汇。参见
`docs/agents/triage-labels.md`。

### 领域文档

本仓库只有一个上下文：根目录的 `CONTEXT.md` 与 `docs/adr/`。参见
`docs/agents/domain.md`。

## DispatchThis 工作流说明

### 修改边界

工作流回调是编排边界。pass 模块可以构建计划并改写当前 IL，但会触发 Binary
Ninja 重新分析的修改必须位于 `plugins/DispatchThis/workflow.py`。

模块边界的命名应保留完整领域术语；内部工作流回调和状态辅助函数应简短。优先使用
`resolve_calls_mlil`、`branch_stable`、`cleanup_decode` 等名称；避免
`workflow_` 之类的冗余前缀，以及
`indirect_call_resolving_is_stable` 之类过长的谓词。

将以下 API 视为会触发重新分析：

- `Function.set_user_indirect_branches`
- `Function.set_call_type_adjustment`
- `BinaryView.add_analysis_completion_event`

不得从低层或中层 pass 模块调用这些 API。每次工作流运行都重复同一修改，可能使函数
分析循环不止。

函数级 phase 状态使用
`Function.session_data["dispatchthis_workflow_state"]`。不得把间接分支或调用
receipt 放在 `BinaryView.session_data` 中。
`BinaryView.session_data` 只适用于视图级的时序/状态，例如待执行的 tag-cleanup
完成回调。

### 固定 phase 顺序与收敛

当前 phase 顺序约束：

- 间接调用和全局常量解析可独立于间接分支解析收敛；它们保留在固定的 MLIL
  activity 位置，但 branch 不得作为其 callback 的前置 gate。
- 分支条件翻译只等待间接分支恢复；它不等待调用或全局 phase。关联 STORE 仍等待
  branch/call/global，deflatten 仍等待 branch/call/global、分支条件及其 cleanup 证明。
- 分支目标清理在分支条件翻译之后运行；分支和调用清理都必须在当前 MLIL 上按精确根反复
  规划到空计划，才可标记 `cleanup_done`。计划重复、应用失败、未证明的 fresh call slice 或
  direct-call receipt 都会阻止 deflatten。分支 receipt 仅因本轮 translator 在已安装的当前
  overlay 中实际 NOP 且局部收敛而保持开放时，才设置一次性的 `cleanup_overlay_ready`；deflatten
  仅在该标记存在并能在同一份当前 MLIL 上重新证明没有分支 cleanup 根后继续。新 translator
  尝试、任何 invalidation、重复计划或应用失败都必须清除该标记；不得借此复用跨重新分析索引。
- 调用目标清理在间接调用解析稳定、且本轮未提交新的调用类型调整后运行。仅由当前 fresh
  call plan 证明的 SSA slice 可关闭其 receipt；仅凭 direct-call receipt 重新绑定不得关闭，
  必须保持开放至重新取得当前计划或没有调用 receipt。

### Deflatten 计划、清理与原子性

Deflatten 计划自行持有清理证据。每个计划带有精确的当前 MLIL 指令索引集合
`obsolete_state_writes`。无法证明后继时不得生成计划；后继已证明、但无法证明
状态写已过时时，保留清理集合为空的计划。不得通过扫描整个函数中相同 token 或变量
来重新发现清理点。

无条件计划带有其私有 dispatcher `exit_jumps` 的全部边；所有出口都必须将具体
状态 token 重放到同一目标。条件目标证明要求每个 arm 中的每条 CFG 路径都终止于
dispatcher 入口；仅证明某个出口可达并不充分。多个有效条件候选属于歧义，不能按
列表或 block 顺序消解。deflatten 后端在一次原子的 MLIL copy-transform 中应用这些
边改写、条件改写以及精确状态写 NOP；任一选中的改写无效时，丢弃整个替换。没有独立的
deflatten 清理 activity 或 state-token/state-variable session map。

### Dispatcher 路由与条件改写

Dispatcher 路由支持变量/常量 `MLIL_CMP_E`、`MLIL_CMP_NE`，以及有符号或无符号的
`LT`、`LE`、`GT`、`GE`。保留操作数顺序和 token 位宽，再通过 dispatcher
CFG 重放每个恢复出的具体 token。不得推断符号 token 区间，也不得在重放存在歧义时
选择目标。

#### 条件改写的准入条件

跳过 arm 工作的条件改写只能包含控制流以及已证明的状态选择依赖链上的赋值。每条路径
都必须建立相同 token；作用域中出现一次相同写入并不充分。当每个 arm 都有私有
GOTO 直接进入 dispatcher 比较行时，改写这些出口，使状态写仍会执行。只有在整个
状态通道仅服务于 dispatcher，且每个跳过的写都已证明私有时，才允许改写原始 IF。
私有的 arm-and-merge 区域可以包含其他已建模语义：若两条路径共用一个最终 dispatcher
GOTO，则保留整个区域。默认用已写入的状态 token 改写该 GOTO；仅当原 IF 是直接
变量/常量比较，且两个 arm 与共享尾部均不写该变量、不取其地址、没有 STORE、未知内存效果
或未建模语义，且源 IF 之前未取其地址时，才可重放原 IF 条件。两种模式均不清理状态写。可能的状态修改仍会拒绝此
模式。若完整 arm-and-merge 区域存在外部入口，条件计划必须被拒绝：
即使只改写 arm 出口，也会改变外部路径对该出口的使用。识别基于指针的状态 STORE 的
profile，必须证明从 STORE 目标到状态变量地址的一条完整且唯一、并支配该 STORE 的
定义链；变量曾经保存过 `&state` 不是充分证据。

#### 重放、别名与可能的状态修改

Dispatcher 重放只能跳过 NOP/GOTO 路由 block 和已证明状态变量依赖链内的直接变量复制。
选中的比较行若含无关赋值、副作用，或将状态替换为常量，必须拒绝。由 dispatcher
派生的临时变量不得在 dispatcher 外有 observer。任何被改写的入口/arm 区域都必须仅对
其声明的 owner block 私有；只检查最终出口 block 不够。这里的“直接变量”是完整的
`MLIL_VAR`/`MLIL_VAR_SSA` 读取。字段、拆分及别名读取都是 observer/alias 证据，
绝不能视为状态或指针的精确复制。

每个 dispatcher 比较别名都必须在同一行的更早位置有一条唯一、等宽的直接复制链，且
选中的行必须终止于同一个状态输入。不能只因为外部定义能追溯至状态就接受别名：它可能
在另一 dispatcher 入口上已过期。字段、拆分和别名写都是可能的状态修改，并把
`STORE_STRUCT` 视为其他指针 STORE。未解析的可能修改会拒绝 transition。
`MLIL_ADDRESS_OF_FIELD` 在 `MLIL_ADDRESS_OF` 作为地址逃逸的所有场景中同样是地址
逃逸。检查 observer 和可能别名时，跟随字段、拆分、别名及 `vars_read` 使用；不要
施加固定的定义深度上限。一旦状态地址被存入内存或被未知操作保留（包括
`holder = &state; call(&holder)`），随后的调用、tail call、syscall、intrinsic、
trap、breakpoint 或非精确 STORE 都是可能的状态修改，即使没有显式指针参数。
`MLIL_UNIMPL` 与 `MLIL_UNIMPL_MEM` 总会拒绝 transition，因为无法证明其状态效果。

#### 身份、predicate 与当前 IL witness

变量身份是语义证据。绝不可用 `str(...)` 或 `repr(...)` 比较、作为 key，或去重
Binary Ninja 变量/寄存器；不同存储对象可能有相同显示名称。显式规范化 SSA wrapper，
之后使用底层对象的 equality/identity。

当 IF 条件是谓词变量时，它的比较定义必须是同一行中更早的当前非 SSA 指令。通过
`non_ssa_form` 映射 SSA 定义，并在使用前验证其当前指令身份。将比较定义而不是之后
的 IF 作为状态复制的 use point。将可能的状态指针传给 call、tail call、syscall 或
intrinsic 会使目标证明失效。仅当零偏移指针复制通过唯一、支配使用点、且复制过程始终
保持已知指针宽度的定义链时，才可接受；字段值、截断复制及其他指针算术都是未解析的
可能修改。
应用 profile 计划前，验证每个计划的 GOTO/IF 在其当前 MLIL 指令索引处仍匹配 operation、
expression identity 与 address。只接受非负、精确的 `int` 指令索引（不能是
boolean），并要求当前指令报告同一索引。对每个目标 basic-block start 也使用同样的
精确整数规则。

#### 路由前缀纯度

只有完整路由前缀通过 purity proof 的比较行，才能加入 `dispatcher_starts` 或从
observer 检查中排除。这同样适用于非支配/辅助比较行，不只适用于选中的主导 cluster。

### MLIL overlay 与 cleanup receipt

`set_user_indirect_branches` 使用 Binary Ninja 的 user-informed dataflow，可能使已解析
分支呈现为 `MLIL_JUMP_TO` 或 switch-like 形状。分支条件翻译有机会读取前，必须保留
目标解码 IL。HLIL 中的 switch 只是展示结果；不要求 MLIL 始终维持为单个
`MLIL_JUMP_TO`。

#### Cleanup 范围

Phase cleanup 必须保持狭窄：只 NOP 从所属 phase 已解析 site 出发的、已死的纯目标解码
赋值。不得折叠控制流、NOP call/STORE，或删除 deflatten 状态写。

#### 跨函数稳定标记

`BinaryView.session_data["dispatchthis_mlil_stable"]` 只用作字符串解密识别的跨函数
gate。启用 deflatten 的新一轮分支翻译会清除当前函数 marker；末尾 deflatten activity
只有在原子替换安装成功后才发布它。

#### Receipt 失效

Cleanup receipt 表示当前 IL 已没有该 phase 所属的待清理改动，而不只是尝试过清理：

- 分支目标 receipt 改变会使 `branch.cleanup_done` 失效。
- 调用目标 receipt 改变会使 `call.cleanup_done` 失效。
- 分支目标改变还会使整个调用 phase 失效。

#### Overlay 收敛

MLIL 改写是 overlay。真正的函数重新分析可能重新生成 MLIL，从而抹掉 NOP/if/call-
destination 改写；但不能为确认 cleanup 主动安排重新分析。分支/调用 cleanup 在本回调的
当前 MLIL 上局部收敛；计划重复或应用失败则保持 receipt 开放并阻止下游 deflatten。只要本轮
实际 NOP 了任何内容，也保持其 cleanup receipt 开放，以便下一工作流轮次从当前 IL 重放或
确认 overlay。此时仅持有同轮 `cleanup_overlay_ready` 标记的分支 cleanup 可由同一 MLIL 的空根
重证明供 deflatten 使用；自然重新分析后，工作流只从当前 IL 重算计划，绝不重用旧索引。

### 验证 workflow 重新绑定

插件热重载对 workflow activity callback 并不可靠。修改工作流注册或回调代码后，应优先
完整重启 Binary Ninja GUI 再验证。直接 `bn py exec` 调用回调可以证明 Python 逻辑，
但不能保证 GUI workflow 已重新绑定该 activity。

## Binary Ninja workflow/API 提醒

### 改 callback 前先阅读

修改该插件前，应先查阅以下本地文档：

- `D:\\BN\\docs\\dev\\workflows.html`
- `D:\\BN\\docs\\dev\\bnil-overview.html`
- `D:\\BN\\docs\\dev\\bnil-modifying.html`
- `D:\\BN\\docs\\dev\\uidf.html`
- `D:\\BN\\api-docs\\binaryninja.workflow-module.html`

### Activity 与 AnalysisContext

Binary Ninja workflow 是 activity 的 DAG。函数 workflow 按函数粒度运行，可与模块 workflow
并发独立执行。activity 在 workflow 间共享，其回调必须可重入；不得把每函数的 pass
状态放在 activity 对象或模块全局变量中。

已注册 workflow 不可变。通常的定制方式是 clone/modify/register，而
`Workflow.insert(activity, activities)` 会在同一层级、目标 activity 之前插入。
注册或调整顺序后，必须验证实际绑定到 GUI 的 workflow，不能只相信源码顺序。

Workflow activity callback 接收 `AnalysisContext`。改写分析状态时，优先使用 context
当前的 `function`、`llil`、`mlil` 和 `hlil`；context 代表进行中的 pipeline，而
`func.medium_level_il` 等属性可能反映已重新生成或过期的 IL。

### IL 层与改写

BNIL 层级的选择很重要：

- LLIL 最接近 lift 后的机器语义，适合尽早恢复间接分支目标以重建 CFG。
- MLIL 具有变量、数据流、传播后的常量、调用点信息和有用的 `PossibleValueSet`；将其
  用于间接调用解析、条件翻译和目标解码清理。
- HLIL 面向展示和结构化。不要把 HLIL 作为间接分支/调用解析决策的事实来源。
- SSA form 是生成的分析产物。用 SSA 做 def-use 推理，但改写对应的非 SSA IL
  expression/instruction。

用 `replace_expr` 修改 IL 后，调用 `finalize()` 重建 IL basic block，并在后续 pass
依赖更新的数据流前调用 `generate_ssa_form()`。若 workflow activity 创建替换 MLIL
function，通过 `AnalysisContext.set_mlil_function(...)` 安装它，而不是只修改脱离
context 的对象。

UIDF 主要通过 MLIL/dataflow 工作。设置 user-informed value 或间接分支会触发函数重新
分析、简化分支，并生成 jump-table/switch-like 输出。在 DispatchThis 中，这意味着在相关
branch/call phase 稳定、且其 translation/cleanup pass 已消费前，必须保留目标解码 IL。

有用的验证命令：

- `bn workflow active`
- `bn workflow show core.function.metaAnalysis --depth immediate`
- `bn api-docs show binaryninja.workflow.AnalysisContext --docs-dir D:\\BN\\api-docs`
- `bn docs show dev\\workflows.html`
