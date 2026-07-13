# 流水线

DispatchThis 注册 **`core.function.metaAnalysis` 的克隆**，并向其中插入自己的
activity。所有操作都是 IL 表达式改写；不会修补字节。

## 注册与顺序

`__init__.py` / `workflow.py` 插入八个 activity。
`analysis.plugins.dispatchThis.indirectJumpsCalls` 是无操作的设置 activity，其余为恢复
工作流阶段：

| Activity ID | 阶段 | 插入位置（之前） |
| --- | --- | --- |
| `analysis.plugins.dispatchThis.indirectJumpsCalls` | LLIL 开关 | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectPatcher` | LLIL | `core.function.generateMediumLevelIL` |
| `extension.DispatchThis.IndirectCallPatcher` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.GlobalConstantResolver` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.BranchConditionTranslator` | MLIL | `core.function.generateHighLevelIL` |
| `extension.DispatchThis.CorrelatedStoreRecovery` | MLIL | `core.function.generateHighLevelIL` |
| `analysis.plugins.dispatchThis.stringDecrypt` | MLIL | `core.function.generateHighLevelIL` |
| `analysis.plugins.dispatchThis.deflatten` | MLIL | `core.function.generateHighLevelIL` |

间接分支解析器在**生成 MLIL 前**运行，因为去平坦化需要先让平坦化 CFG 出现（间接跳转已
成为真实边）。其余六项在生成 HLIL 前按“调用解析 → 全局常量解析 → 分支条件翻译 → 关联
存储恢复 → 字符串解密 → 去平坦化”运行。MLIL activity 由函数阶段状态门控，只有间接分支
解析稳定后才提交会触发重新分析的修改。工作流回调拥有这些修改：
`set_user_indirect_branches`、`set_call_type_adjustment`、全局 data-var 类型设置和分析完成
回调调度。

协调规则见
[`adr/0003-function-phase-state-for-workflow.md`](adr/0003-function-phase-state-for-workflow.md)，
完整证据与当前见证规则见
[`adr/0011-complete-evidence-and-current-il-witnesses.md`](adr/0011-complete-evidence-and-current-il-witnesses.md)，
计划自有调用 load 清理见
[`adr/0012-call-target-slice-owned-load-cleanup.md`](adr/0012-call-target-slice-owned-load-cleanup.md)，
可选语义 profile hook 见
[`adr/0013-optional-semantic-profile-hooks.md`](adr/0013-optional-semantic-profile-hooks.md)。
新二进制识别器应作为内置解析 profile 添加；见
[`resolver-profiles.md`](resolver-profiles.md)。

## 各活动

### 1. 间接分支解析器（LLIL）— `passes/low/gadget_llil.py`

`resolve_llil_jump_plan` 解析每个解码 gadget `jump(reg)`（以及 tail-call 形态），从重定位
跳转表解码目标并返回只读计划。`apply_llil_jump_rewrites` 在当前 LLIL 中改写单目标跳转。
工作流回调拥有会触发重新分析的 `set_user_indirect_branches` 修改，并为每个源记录按函数
回执。分支解析稳定后，工作流还会通过 `BinaryView.add_analysis_completion_event` 调度
`Unresolved Indirect Control Flow` 标签清理。

每个计划保存当前 LLIL jump 见证。改写或提交元数据前，pass 会按源分组事实，要求一个
完整且无冲突的语义结果，其 operation、地址、instruction/expression identity、目标表达式
和 IL owner 仍与当前 LLIL 匹配。回执本身绝不抑制解码：只有完整目标元组与 Binary Ninja
当前非自动用户分支元数据精确一致的回执，才会离开下一轮解码前沿；缺失、自动、子集、
超集或已变更元数据都强制重新识别。这避免反复解析用户知情数据流为已解析分支创建的
`LLIL_JUMP_TO` 形态，同时不隐藏新工作。未匹配 gadget 形态为 debug 事件，因为扩展的
CFG 常在下次重新分析前暴露中间形态；畸形、冲突或过期的分支事实保持 warning。

函数会扩展，故工作流会以无需手工循环、无需字节修补的方式**迭代到不动点**。先只读地
解析目标，再应用单目标当前 IL 改写并为整批重建一次 SSA。多目标计划不进入改写后端，
因为其 CFG 由用户分支元数据而非常量跳转目标表示。仅当每个未解析间接分支源均被用户
分支元数据覆盖，且本轮未提交新的分支修改时，分支解析才稳定。

### 2. 间接调用解析器（MLIL）— `passes/medium/indirect_calls.py`

`plan_indirect_calls` 在不修改函数状态的前提下折叠每个导入调用的解码
（`target = (encoded + key) mod 2^48`）。`apply_indirect_call_rewrites` 预校验完整计划批次，
创建全部替换，再以 `replace_expr` 仅将每个当前调用**目标表达式**改为 `const_pointer`。
它为整批 finalize MLIL 并生成一次 SSA。此类仅表达式 overlay 不复制整函数，也不调用
`AnalysisContext.set_mlil_function`，以免 Binary Ninja 重建完整 LLIL 到 MLIL 映射。pass
刻意不改写 profile 提供的 `decode_def`；死解码指令只属于重新计算的 SSA 目标切片和阶段
清理。

调用与描述性解码见证会在调用目标或调用类型修改前重新绑定到精确当前非 SSA MLIL；过期
profile 事实按失败即关闭处理。解码见证本身绝不改写。

每个调用计划还拥有仅馈入 `call.dest` 的精确当前 SSA 到达定义切片。PHI 展开所有输入；
只有映射到精确当前非 SSA 赋值的整变量 SSA 定义会成为清理根。字段、split 和 aliased
链按失败即关闭处理。无法证明完整 slice 时，该 fresh plan 不具备 cleanup ownership，receipt
保持开放。load 赋值具有单独的 `cleanup_load_roots` 见证，因为通用清理仍须
把 load 视为可观察。两个根集合都在修改边界从当前调用重新计算，profile 不提供索引，也
不能授权清理。目标改写后，当前 SSA 存活性仅在见证 load 的结果在已过时目标切片外无使用
时 NOP 它。该证明使用调用点数据流而非 BinaryView xref；回调参数及任何真实消费者因此
保持赋值存活。call、store、intrinsic、未实现 IL 及其他行为指令绝不因位于调用前而被接纳。
保存的调用回执证明 callee，不证明清理所有权，因此工作流绝不通过扫描回执地址前的赋值
重建根。仅由回执重新绑定出的 direct call 没有当前 cleanup slice；它会保持 cleanup receipt
开放，而不会以空根集合宣告收敛。

目标成为裸常量后，调用只有调用约定猜测而无原型，HLIL 可能把参数显示为 `/* nop */`。
工作流构造调用点类型：参数取自当前 MLIL 参数表达式，callee 仅提供返回类型、调用约定
和相关 ABI 元数据；随后用 `set_call_type_adjustment` 安装类型。

> [!IMPORTANT]
> `set_call_type_adjustment` 是会安排重新分析的*函数级*编辑（不同于当前 pass 只消费的
> `replace_expr`）。每轮都应用会使分析无限循环，因此工作流在
> `Function.session_data["dispatchthis_workflow_state"]` 中记录按函数调用调整回执。

回执不是事实来源：每轮对每个安全具体覆盖都将期望原型与 `get_call_type_adjustment` 比较，
仅提交真实差异，读回后才标为已调整。因此即使 callee 自身被混淆、BN 推断出空或不完整
参数表，当前调用点参数仍保留。当前 fallthrough 还会覆盖过早的 noreturn 推断。callee 没有
可用函数类型，或任一调用点参数没有可用表达式类型时，工作流不应用覆盖，而不是虚构类型。
特别是它不会调用 `set_call_type_adjustment(addr, None)`，也不会将 `None` 与 BN 有效自动
推断类型比较：清除用户覆盖仍可能暴露自动类型，否则工作流会持续重新进入。

### 3. 分支条件翻译器（MLIL）— `passes/medium/branch_conditions.py`

`set_user_indirect_branches` 使用 Binary Ninja 的 user-informed dataflow，因此双目标间接
跳转可呈现为已解析 `switch`/`MLIL_JUMP_TO` 形态。间接分支和全局常量解析稳定后，翻译器
将这些双目标 switch 改写回 `MLIL_IF` 表达式。把昂贵 CFG copy 放到全局 data-var 编辑及其
所需重新分析之后，可避免重复安装同一个 overlay。若 switch 是已有
`IF -> two private constant-selector arms -> one decode join` 菱形的尾部，它会原地重定向
源 `IF`，而不在 join 处创建第二个条件。两个解码目标必须唯一，且每个独立有效的 selector
见证必须一致。臂和 join 必须是私有、无副作用的目标解码代码，其写入变量在菱形内局部且
属于 jump-destination 依赖链。所有权证明失败时，翻译器保留既有 join-site 行为，不绕过
未知代码。臂中赋值的 selector 必须在 join 前缀中不变；fallback 把源谓词移到 join 时，该
谓词也必须在两臂和 join 前缀中不变。原地改写源 IF 不移动谓词，因而不施加后一限制。
这种可重复的展示改写没有修改回执。其精确连续源赋值前缀作为分支清理根提交；SSA 存活性
保留状态/比较复制，只 NOP 死亡目标解码结果。

翻译产生 copy 后，先以 `AnalysisContext.set_mlil_function` 安装中间 MLIL；同一 callback
的分支 cleanup 只使用该 `new_mlil`，不用 Python binding 可能仍返回的旧 `ctx.mlil`。它在
该 MLIL 上反复规划到空计划；失败或重复会停止本轮。即使局部收敛，只要本轮 NOP 了目标
解码，branch cleanup receipt 仍保持开放，并只为该 translator 调用记录一次性的
`cleanup_overlay_ready`。末尾的 deflatten activity 仅在该标记仍存在时，才会以同一份当前
MLIL 再次检查 branch receipt 根；只有根为空才可读取它。新 translator 尝试、phase invalidation
或 cleanup 失败都会清除标记；这不是跨轮缓存，也不放宽 call cleanup gate。去平坦化仍由独立 activity 执行，
因此不会跳过关联存储恢复。

HLIL 验证看控制流语义，不看 `switch/case` 形状或 workflow receipt：应追踪已识别的 dispatcher
状态比较，确认它们没有被不透明 state token 比较重新表达；共享出口若能证明原条件不变，应
显示原条件。无法证明时保留 token fallback 是安全的，不应为了外观强行改写。已解析的双目标
branch transition 可以安全地成为 `MLIL_IF`；多目标 dispatcher 不强制改写。去平坦化只有在
branch-target cleanup receipt 收敛后才读取 MLIL，避免残留 target-decode 破坏纯度证明。

所有权规划器仅为本次翻译器调用构建两个惰性索引：来自所有 store/未知内存效果根的共享
alias 图，以及变量到读取/取地址基本块的映射。这样在不为每个候选菱形重扫整函数的同时，
保留相同的失败即关闭逃逸和作用域局部性证明。两个索引都不跨越 MLIL 修改或重新分析。
至少有一个控制流计划被接纳时，全部已选顶层 `IF`/`JUMP_TO` 替换通过一次 MLIL
copy-transform 安装，因为它们共享复制标签并改变 CFG 边；空计划不复制或安装 MLIL 函数。

### 4. 全局常量解析器（MLIL）— 活动 profile

活动 profile 的 `plan_global_constant_slots` 返回已证明槽位/类型事实。工作流解析每项事实的
const 限定类型，应用 BinaryView 级 `define_user_data_var` 修改，读回当前 data-var 类型作为
视图级事实，并记录按函数回执。同一槽位的冲突事实被拒绝。仅当全部回执仍与 BinaryView
类型匹配时，当前函数全局阶段才稳定。

默认 profile 的范围刻意狭窄：`.data` 中 qword 槽位、非零常量偏移链、有效已解析地址，且
已知直接引用函数中没有向该槽位存储。其他 profile 可证明不同形态和类型，无需把修改所有
权移出工作流。

### 5. 关联存储恢复（MLIL）— `passes/medium/correlated_stores.py`

全局常量稳定后，活动 profile 可识别目标与源来自关联同级 PHI 的 join-block store。
`apply_correlated_stores_mlil` 原子地在各自前驱臂插入每个具体 store，并 NOP 合并 store。
不支持或不完整计划保持当前 MLIL 不变。

### 6. 字符串解密（MLIL，可选）— `passes/medium/string_decrypt.py`

由 `String Decrypt` 设置门控。间接分支、间接调用及全局常量阶段稳定前，工作流回调直接
返回；它不要求当前函数先完成去平坦化。

活动 profile 的 `plan_string_decrypt_calls` 检查当前 MLIL 并返回明文事实，不写注释。
profile 可使用 `dispatchthis_mlil_stable`，要求候选 callee 已成功安装去平坦化替换。共享
后端 `apply_decrypted_string_comments` 将接纳事实变为以下形式的函数级注释：
`[decrypt] <escaped-string>, src=0x... dst=0x...`；已有手工注释行会保留。

默认 profile 只识别其样本家族的直接解密调用形态：两个参数、key-prefix 与加密 payload
读取、一个完整 key-modulus/output-length 对、向目标的字节写入以及一次性 done-flag 写入。
其他 profile 自行承担完整识别证明。

### 7. 去平坦化器（MLIL，可选）— `passes/medium/deflatten.py`

由 `Enable Deflattening` 设置门控，且仅在函数阶段状态报告 branch、call、global 均稳定，
并且 call-target cleanup receipt 已收敛后运行。branch-target cleanup receipt 已收敛时可直接
运行；若它仅因本轮已安装 overlay 的实际 NOP 保持开放，必须持有 `cleanup_overlay_ready` 并在
同一当前 MLIL 上重新证明 branch cleanup 根为空。
否则 CFG、调用语义或恢复出的状态机仍可能不完整；残留 target-decode 也会破坏调度器纯度证明。

- 活动解析 profile 的 `plan_deflatten_redirections` 识别二进制特定的调度器/状态写入形态，
  并将状态令牌映射到目标原始块。调度器行可用相等、不等或有符号/无符号
  `LT`、`LE`、`GT`、`GE` 比较。规划器保留操作数顺序和令牌宽度，随后通过调度器 CFG
  重放每个已恢复的具体令牌；不求解符号区间。每个比较别名均须由该行中更早处唯一的整变量、
  等宽直接复制链建立，并结束于调度器行共享的状态输入。字段/split/aliased 读取是可能的
  观察者，而非精确复制。谓词变量条件必须通过精确 SSA 到非 SSA 映射解析为该行中更早的
  当前比较，复制链也必须先于该比较。辅助比较块只有完整前缀通过路由纯度证明后，才加入
  调度器边界。分支条件翻译会在本分析前移除已证明私有解码菱形，因此去平坦化不携带第二个
  用于合成翻译尾部形态的识别器。独立 OBB 状态变量只有经一个等宽整变量 latch 才能映射到
  比较变量，该 latch 必须是唯一调度器入口且至少由两个独立目标头区域共享。该显式 latch
  之外的反向边界扩展只接受 `NOP* + GOTO` 块。默认 profile 委托给
  `compute_redirections`。
- `rewrite_redirections_mlil` 使用 MLIL copy-transform 后端构建原子替换：每个私有调度器
  出口重定向至其唯一已证明目标；条件转移明确选择私有臂出口改写、私有共享尾出口改写或
  完整证明的条件捷径。共享出口默认用具体 state token 路由；原 IF 为直接变量/常量比较且其
  输入在 arm/共享尾中未改写、未逃逸且没有 STORE、未知内存效果或未建模语义时，改为复制原
  条件。仅每个计划 `obsolete_state_writes` 集合内的精确指令索引才变为 NOP。
  见 [`conditional-deflattening.md`](conditional-deflattening.md)。任一被拒绝的重定向都会
  丢弃整次替换。
- 所选边改写保留状态执行时，目标与清理证明相互独立。不确定目标不产生计划；目标已证明
  但清理不确定时保留空 `obsolete_state_writes` 集合。会绕过这些写入的条件捷径则需要完整
  私有清理/状态通道证明，否则被拒绝。
- 部分/split/aliased 状态写入、未解析 struct 或 pointer store，以及整变量或字段地址逃逸
  一律按失败即关闭处理，而非忽略为无关 IL。call、syscall 或 intrinsic 接收可能状态指针
  会使目标证明而非仅清理证明失效。地址逃逸入内存后，即使后续未知内存效果或非精确 store
  没有显式指针参数，也会使令牌失效。若 holder 含有 `&state`，未知操作保留 `&holder` 也
  构成逃逸。未实现 IL 始终拒绝该转移。
- 工作流通过 `AnalysisContext.set_mlil_function` 安装替换，然后发布
  `dispatchthis_mlil_stable` 以便跨函数字符串解密识别；不发布去平坦化令牌或变量清理 map。

清理所有权与原子性决策记录于
[`adr/0010-plan-owned-atomic-deflatten-cleanup.md`](adr/0010-plan-owned-atomic-deflatten-cleanup.md)。

## 为什么 MLIL 阶段每轮都会重放

间接调用、分支条件、关联存储和原子去平坦化的 MLIL 改写是从未改变的 LLIL 派生的
*overlay*。每次重新分析都会从 LLIL 再生 MLIL 并撤销它们，所以这些 pass **每轮都会重跑**，
以保持改写，而非首次应用后锁死。分支/调用目标解码 cleanup 在当前 MLIL 上收敛到空计划
才关闭 receipt；不为确认它而安排重新分析。自然重新分析后，工作流从当前 IL 重建计划。

## `session_data` 键

| 键 | 含义 |
| --- | --- |
| `dispatchthis_mlil_stable` | `{start: bool}`：原子去平坦化替换已安装；仅作跨函数字符串解密门控 |
| `dispatchthis_tag_cleanup_pending` | `set(start)`：等待分析完成回调的视图级集合 |

函数作用域阶段状态位于 `Function.session_data["dispatchthis_workflow_state"]`；协调规则见
[`adr/0003-function-phase-state-for-workflow.md`](adr/0003-function-phase-state-for-workflow.md)：

| 字段 | 含义 |
| --- | --- |
| `profile_id` | 函数作用域证据的解析 profile 来源；含恢复证据的状态不能重绑到其他 profile，空状态可重绑 |
| `branch.stable` | 间接分支解析已到达当前不动点 |
| `branch.receipts` | `{source_addr: (target_addr, ...)}`，已针对当前用户分支元数据验证 |
| `branch.cleanup_done` | 当前分支回执的分支目标解码清理已无剩余改动 |
| `branch.cleanup_overlay_ready` | 仅当前 translator/MLIL overlay：已 NOP 且局部收敛，允许下游作一次空根复证 |
| `call.stable` | 间接调用解析已到达当前不动点 |
| `call.receipts` | `{call_addr: target_addr}`，调用类型决策已完成：读回具体覆盖，或当前调用点证据无需覆盖 |
| `call.targets` | `{call_addr: target_addr}`，验证为当前调用目标，包含无需类型调整的调用 |
| `call.cleanup_done` | 当前调用回执的调用目标解码清理已无剩余改动 |
| `global.stable` | 全局常量解析已为该函数到达当前不动点 |
| `global.receipts` | `{slot_addr: type_string}`，验证为该函数的全局常量 data-var 类型 |

## 分析环境

在 Binary Ninja 5.3+ 上，最早符合条件的解析回调会为当前 Function 建立所需分析环境，而非
在导入 DispatchThis 时。它仅在需要时用 `SettingsResourceScope` 覆盖继承值，并在 profile
识别或恢复工作前全部读回验证。写入或验证失败则跳过该轮工作流；Function 覆盖设置在
DispatchThis 禁用后刻意保留。

| 设置 | 必需值 |
| --- | --- |
| `analysis.limits.maxFunctionSize` | `0`（无限制） |
| `analysis.limits.expressionValueComputeMaxDepth` | `99999` |
| `analysis.limits.maxFunctionAnalysisTime` | `1800000` ms（30 分钟） |
| `analysis.limits.maxFunctionUpdateCount` | `1024` |
| `analysis.outlining.builtins` | `false` |
