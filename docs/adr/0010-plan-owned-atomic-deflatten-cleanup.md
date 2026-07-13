# 让 deflatten cleanup 由计划持有并保持原子性

Deflatten 的 state-write cleanup 必须从证明每个恢复 transition 的同一次当前 MLIL 分析中
推导。每个 redirection plan 携带精确 instruction index 的 `obsolete_state_writes` 集合，
以及映射到已记录当前 MLIL instruction 的
`obsolete_state_write_witnesses`。目标证明与 cleanup 证明相互独立：目标不确定时不生成
计划；目标已证明、但 cleanup 不确定时，仍生成 cleanup set 为空的有效计划。函数其他位置
匹配到 state variable、token value 或低位 token bit 不是 cleanup evidence。

无条件计划包含原区域中所有私有 `exit_jumps`。从每个 dispatcher entry 重放其具体
`(state_token, width)` 必须到达同一 common target。Dispatcher replay 按原始 operand order
和 bitvector width 计算变量/常量 `MLIL_CMP_E`、`MLIL_CMP_NE` 及有符号或无符号
`LT`、`LE`、`GT`、`GE` predicate。它不做 symbolic range solving；不支持的比较、
width mismatch 或歧义路由都会拒绝 transition。

条件 transition 中，每个选中 arm 的每条路径都必须终止于 dispatcher entry，并建立相同的
具体 token；仅存在可达性，或作用域中某处有一次相同写入，都不是 target proof。被条件 rewrite
跳过或绕过的 assignment 必须属于恢复出的 state-selection dependency chain，且不能在 arm
scope 外保持 live。共享出口 rewrite 可以保留其他已建模语义，因为它只改变共同的最终 GOTO；
但完整 arm-and-merge region 必须私有。原区域中有多于一个有效 conditional candidate 时存在
歧义，拒绝该区域。基于指针的 state STORE 需要一条从 STORE destination 到 state variable
address 的完整、唯一的 definition chain。该链上每个 definition 都必须支配其 use。

只有包含 `NOP* + GOTO` 的 dispatcher pass-through block 才能在 concrete replay 中被隐式展开。
直接 copy 仅在明确证明的 comparison row 中，或一个唯一、等宽的 shared state latch 中保留
token。该 latch 必须是至少两个拥有不同 state writer 的独立 target-head region 的 dispatcher
ingress；OBB-local selection join 不足以满足要求。无关 assignment、副作用、constant state
replacement，或派生 comparison variable 的非 dispatcher observer 都会拒绝受影响的 dispatcher。
entry 与 arm ownership 检查覆盖整个被改写 region，而非仅其最终出口。

每个 comparison value 都必须由其自身 dispatcher row 内的一条唯一直接 copy chain 产生，并终止
于被选中行共享的 state input。planner 不能仅因某个临时值的定义最终追溯到 state 就接受它：
该值可能在另一 dispatcher entry 路径上已过期。可能消耗保留 bit 的 partial、split、aliased
state write，以及无法解析为一次精确 state update 的 `STORE_STRUCT` 或其他 pointer write，
都会拒绝 transition。`ADDRESS_OF_FIELD` 在 `ADDRESS_OF` 作为地址逃逸的所有场景中同样
算地址逃逸。只有完整 `MLIL_VAR`/`MLIL_VAR_SSA` read 是精确直接 copy；`VAR_FIELD`、
split 和 aliased read 应保守地跟踪为 observer 或 possible alias，但不能替代完整 value。
读取证明包含 Binary Ninja 明确的 `vars_read` metadata。变量 worklist、binding 和去重使用
Binary Ninja variable equality/identity，绝不使用 `str` 或 `repr`：两个不同 storage object
可能有相同 display name。辅助/非主导 comparison block 仅在完整 routing prefix 与选中行一样
通过 purity proof 后才加入 dispatcher boundary；不能仅因比较追溯到 state，就把不纯的 IF
block 藏在 observer analysis 之外。

当 `MLIL_IF` condition 是 predicate variable 时，planner 将其 SSA definition 映射回
`non_ssa_form`，验证精确的当前非 SSA instruction，并且只在 definition 位于同一 dispatcher
row 的更早位置时接受它。state copy chain 必须位于该 comparison 之前，不能只位于之后的 IF
use 之前。若 call、tail call、syscall 或 intrinsic 接收可能的 state pointer，则 target proof
失效，因为它们可能替换原本具体的 token。只有所有已知 copy width 相同，完整的 zero-offset
pointer copy 才能证明为精确 state store；field value、truncating copy、nonzero arithmetic 及
其他歧义 arithmetic 仍是 fail-closed 的 possible mutation。Possible-address traversal 会跟随所有
可用的 field/split/aliased definition，不设固定深度上限。若 state address 已存入 memory 或被
unknown operation 保留——包括间接的 `holder = &state; call(&holder)`——后续 unknown memory
effect 或 non-exact STORE 即使没有显式 pointer argument，也会使 token proof 失效。地址已逃逸
时，trap 和 breakpoint 是 unknown effect。由于无法证明状态语义，`MLIL_UNIMPL` 与
`MLIL_UNIMPL_MEM` 无条件拒绝 transition。

`rewrite_redirections_mlil` 在一次 MLIL copy-transform 中，一起验证并应用每个选中的 exit
rewrite 或 conditional rewrite，以及每个精确 state-write NOP。若任一选中 instruction 缺失、
operation 不受支持、与其他 replacement 冲突，或无法复制，则丢弃完整 replacement。复制前，
计划的 source owner、operation、expression identity、address 和相关 GOTO/IF operand 必须仍
与 current MLIL 同一 index 的 instruction 匹配。每个 cleanup witness 以同样方式重新绑定；只有
其 SET_VAR 或 STORE destination、source、size 与适用 offset 均匹配时，才能将其变为 NOP。
Rewrite 与 cleanup index 必须是非负、精确的 `int`（不能是 boolean），当前 instruction
必须报告相同 index，每个 target basic-block start 也须遵守相同精确整数规则。这样既能在
cleanup 不确定时保留 CFG recovery，又不会允许部分 graph mutation。

Cleanup ownership 比 target proof 更严格。条件计划中，来自 rewritten arm 或 region 外的 incoming
edge 会拒绝计划，因为 shared-exit mutation 会影响外部路径。对无条件计划，这种 edge 只会使
`obsolete_state_writes` 为空，只要 owned exit 本身仍有效。当每个私有 arm 都到达不同、且
直接进入 dispatcher comparison row 的 GOTO 时，条件计划使用 exit-preserving rewrite。当两条
arm 汇合到一个私有 shared tail 时，planner 保留完整 region，默认只把其唯一的最终 dispatcher
GOTO 替换为对具体 state token 的 comparison。若原 IF 是直接变量/常量比较，且其变量在 arm 与
shared tail 内不被写入、不逃逸、没有 STORE、未知内存效果或未建模语义，则原条件可以带当前
IF witness 一起复制到 shared exit；否则保持 token comparison。此模式不做 state-write cleanup。
否则，仅当所有跳过的 state-channel work 都已证明只服务于 dispatcher 且为私有时，planner 才可 shortcut 原始 IF；
此时 cleanup 不确定会拒绝 shortcut，不能静默绕过 write。

任何可能消费保留 bit 的 partial/split/aliased state mutation 也会阻止 cleanup 先前 state
write。若 edge plan 保留执行，它仍可有效，但 cleanup set 保持为空；不得从后续 full-write
pattern 推断 cleanup safety。

独立的 deflatten Cleanup workflow activity、函数范围 NOP scan，以及
`dispatchthis_state_consts` / `dispatchthis_state_vars` view-level map 均已移除。
`dispatchthis_mlil_stable` 仅保留为跨函数 string decrypt gate：启用 deflatten 的新一轮
分支翻译先清除当前函数 marker，末尾 deflatten activity 仅在安装 atomic replacement 后发布它。
Binary Ninja 重新分析可能抹掉 MLIL overlay，因此后续 workflow run 会依据 current MLIL
重新计算 plan 与精确 cleanup evidence。

Branch-target 与 call-target phase cleanup 仍严格以各自 recovery fact 为根，不会删除 deflatten
state write。Call cleanup 还可移除一条 SSA-dead load assignment，但仅当 current call plan
将该精确 instruction 标为完整 `call.dest` definition slice 的一部分。它不使用 xref，也不把
任意 load 视为全局纯。
