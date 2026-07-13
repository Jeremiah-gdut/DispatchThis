# 辅助 API

本文说明解析 profile 与 pass 使用的公开 helper 模块：

```python
from DispatchThis.helpers import facts, llil, memory, mlil
```

这里只记录各模块 `__all__` 导出的名称；以下划线开头的 helper 是实现细节。

## `llil`

LLIL helper 用于检查低层 IL 并恢复间接分支目标。

### 常量

| 名称 | 作用 |
| --- | --- |
| `U48` | 当前内置分支解析器的地址掩码：`0xffffffffffff`。 |
| `CONST_OPERATIONS` | 立即数常量的原生 BN enum。 |
| `INDIRECT_JUMP_OPERATIONS` | 间接 jump/tail-call 终结器的原生 BN enum。 |
| `LOAD_OPERATIONS` | 用于检查 stack spill/reload 常量的 LLIL load 原生 BN enum。 |
| `SET_REG_OPERATIONS` | 供寄存器 helper 跟随的 LLIL SSA 寄存器赋值原生 BN enum。 |
| `CONST_OPS` | 由 `CONST_OPERATIONS` 生成的兼容名称。 |
| `INDIRECT_JUMP_OPS` | 由 `INDIRECT_JUMP_OPERATIONS` 生成的兼容名称。 |
| `LOAD_OPS` | 由 `LOAD_OPERATIONS` 生成的兼容名称。 |
| `SET_REG_OPS` | 由 `SET_REG_OPERATIONS` 生成的兼容名称。 |

只处理 LLIL 的代码比较原生 `*_OPERATIONS` enum。生成的 `*_OPS` 名称仅供有意混合 LLIL 和
MLIL selector 的调用者使用。

### `iter_indirect_jumps`

**签名**

```python
iter_indirect_jumps(llil)
```

产生目标尚非常量的 LLIL 间接 jump 或 tail-call 终结器。`llil` 必须可按基本块和指令迭代；
若为 `None`，产生空迭代器。仅包含 `INDIRECT_JUMP_OPERATIONS`，跳过目标 operation 在
`CONST_OPERATIONS` 中的指令；不解析目标、不改写 IL，也不检查工作流状态。

### `peel_reg_definition`

**签名**

```python
peel_reg_definition(ssa, expr, trail=None, max_depth=32)
```

沿简单 SSA 寄存器定义跟随 `LLIL_REG_SSA`，直至非寄存器表达式或停止条件。`ssa` 需支持
`get_ssa_reg_definition(reg)`；`trail` 非空时按遍历顺序追加已跟随定义，`max_depth` 限制跳数。
返回剥离后的 LLIL 表达式；未解析/PHI/不支持定义或 BN API 异常时返回当前表达式而非抛出。
只跟随完整 `LLIL_SET_REG_SSA`，在 `LLIL_REG_PHI`、部分寄存器写入或其他定义形态停止；
不遍历 CFG 路径，也不选择 PHI 边。需要 PHI 候选时用 `const_values`。

### `const_values`

**签名**

```python
const_values(bv, ssa, expr, max_depth=32)
```

恢复一个 LLIL 表达式可导出的**全部**具体常量候选。`bv` 仅供内部路径经 `ssa` 查询 BN 值信息；
`ssa` 需支持寄存器/flag 定义查询，`max_depth` 限制递归表达式/定义深度。返回完整
`set[int]`，任一语义路径未知则返回 `None`；调用者必须先区分 `None` 再检查基数。

支持 LLIL 常量、零/符号/低位转换、布尔转整数、移位、基本算术/位运算、寄存器 SSA 定义、
部分寄存器、stack spill/reload 常量和受支持 PHI 候选集。谓词不能唯一折叠时
`LLIL_BOOL_TO_INT` 给出 `{0, 1}`。PHI 只有每个非回边臂完整时才给出候选；一个未知臂使
结果为 `None`，回边/环受限。它不做 CFG 路径消歧，也不证明哪个 PHI 边可达；需要唯一
key、base 或 offset 的 profile 必须要求非 `None` 且 `len(values) == 1`。算术与转换使用
各 LLIL 表达式自身位宽；内置分支 gadget 的 48 位掩码在二进制特定公式边界应用，而不在
通用值折叠器中应用。

### `correlated_const_values`

**签名**

```python
correlated_const_values(bv, ssa, expr, max_depth=32)
```

恢复 LLIL 常量候选，同时保留一个表达式中多个同级 `LLIL_REG_PHI` 的同臂关系。参数与
`const_values` 相同。返回完整 `set[int]`；相关性接缝返回 `None` 表示不存在多 PHI 情况，
可回退到文档规定的 `const_values`；空集表示发现多 PHI 关系但无法证明，调用者必须拒绝，
不能使用笛卡尔积回退。

同一 join 读取多个 PHI 时，按精确前驱基本块对齐 operands，并对每个已证明前驱臂计算一次。
例如 `phi(1, 2) + phi(10, 20)` 的不可能笛卡尔积是 `{11, 12, 21, 22}`，相关计算结果为
`{11, 22}`。它不替代 `const_values`，仅适用于 PHI operand 预期来自同一前驱 split 的 profile；
值保留 `const_values` 建立的表达式宽度。

### `correlated_phi_values`

**签名**

```python
correlated_phi_values(ssa, expr, value_func, max_depth=32)
```

供自有值折叠器 profile 使用的通用同臂 PHI 计算器。`value_func` 签名为
`value_func(operand, bindings=None)`，必须返回 `set[int]`；`bindings` 将真实 PHI register
对象映射到 helper 选择的臂值，显示名绝不是 identity key。`max_depth` 限制收集 PHI
register 的递归深度。

同臂计算成功返回 `set[int]`；无多 PHI 返回 `None`；发现的多 PHI 关系歧义或不完整返回空集，
只有 `None` 可回退。helper 仅拥有 PHI 臂相关性；算术、load、宽度掩码和二进制特定地址模型
由调用者 `value_func` 负责。它要求所有收集的 PHI 位于同一 join 块、对每个精确入前驱各有
一个 operand，且每个选中 operand 都能折叠为一个完整值。不同但同名的 register 对象仍保持
不同；不修改 IL 或工作流状态。

### `phi_registers`

```python
phi_registers(ssa, expr, max_depth=32)
```

返回 `expr` 读取、且定义链终止于 `LLIL_REG_PHI` 的 SSA register。结构遍历使用 Binary
Ninja 的 `traverse`；只有定义链 worklist 是项目逻辑。

### `stack_store_sources`

```python
stack_store_sources(ssa, load_expr)
```

返回可馈入 LLIL load 的全部精确宽度 stack-store source。stack slot 取自
`RegisterValueType.StackFrameOffset`，store 来源取自 `get_ssa_memory_definition`。call、
未知定义、重叠写入、环或未解析 PHI 臂按失败即关闭处理。已解析 PHI 臂仍是独立候选；
单值调用者仅当所有臂均折叠为同一值时才接纳。

## `mlil`

MLIL helper 用于调用目标、全局槽位分析、表达式遍历、operation 查询、具体调度器比较和
清理根收集。

### 常量

| 名称 | 作用 |
| --- | --- |
| `ADDRESS_OF_OPERATIONS` | 接受整变量或字段地址的原生 BN enum。 |
| `CALL_OPERATIONS` | typed、untyped、SSA 与 tail call 的原生 BN enum。 |
| `CONST_OPERATIONS` | 立即数常量的原生 BN enum。 |
| `LOAD_OPERATIONS` | 常量折叠与 load 识别使用的原生 BN enum。 |
| `LOAD_STRUCT_OPERATIONS` | struct load 的原生 BN enum。 |
| `SLOT_LOAD_OPERATIONS` | `load_slot_address` 接受的原生 BN enum。 |
| `SET_VAR_OPERATIONS` | 供剥离和清理 helper 跟随的变量赋值原生 BN enum。 |
| `STORE_OPERATIONS` | `mlil_stores_to_address` 检查的原生 BN enum。 |
| `ADDRESS_OF_OPS` | 由 `ADDRESS_OF_OPERATIONS` 生成的兼容名称。 |
| `CALL_OPS` | 由 `CALL_OPERATIONS` 生成的兼容名称。 |
| `CONST_OPS` | 由 `CONST_OPERATIONS` 生成的兼容名称。 |
| `LOAD_OPS` | 由 `LOAD_OPERATIONS` 生成的兼容名称。 |
| `LOAD_STRUCT_OPS` | 由 `LOAD_STRUCT_OPERATIONS` 生成的兼容名称。 |
| `SLOT_LOAD_OPS` | 由 `SLOT_LOAD_OPERATIONS` 生成的兼容名称。 |
| `SET_VAR_OPS` | 由 `SET_VAR_OPERATIONS` 生成的兼容名称。 |
| `STORE_OPS` | 由 `STORE_OPERATIONS` 生成的兼容名称。 |

`*_OPERATIONS` 是正常的单 MLIL API。对应的 `*_OPS` 刻意保存名称，以便兼容调用者混合
LLIL 与 MLIL operation（两层的 `IntEnum` 数值可能冲突）。所有名称均由 Binary Ninja enum
生成；生产代码不手写 operation 名称。

### `op_name`

**签名**

```python
op_name(expr)
```

返回 `expr.operation.name`；表达式不存在或不暴露 Binary Ninja operation 时返回 `None`。

### `same_var`

**签名**

```python
same_var(left, right)
```

仅按真实 equality/identity 比较 Binary Ninja variable-like 对象。显示名刻意不是回退，因为
不同变量可渲染为同名。调用前必须显式规范化 SSA/aliased wrapper。

### `var_from_expr`

**签名**

```python
var_from_expr(expr)
```

从完整、字段、SSA-field 或 aliased 变量读取形式返回底层 base variable，其他情况返回
`None`。它供 observer/may-alias 分析使用，不证明表达式包含完整值。

### `direct_var_from_expr`

**签名**

```python
direct_var_from_expr(expr)
```

仅对完整 `MLIL_VAR` 或 `MLIL_VAR_SSA` 读取返回底层 variable。调度器复制链和精确指针
证明使用此更窄 helper，字段、split 或 aliased 值不能代替完整状态。

### `addressed_var`

**签名**

```python
addressed_var(expr)
```

返回 `MLIL_ADDRESS_OF` 或 `MLIL_ADDRESS_OF_FIELD` 指向的 variable，其他情况返回 `None`。
显式检查是必要的，因为 Binary Ninja 不总会把字段地址 operation 放入通用 address-taken
metadata。

### `instruction_writes_variable`

**签名**

```python
instruction_writes_variable(instruction, variable)
```

保守检测对一个 variable 的完整、字段、split、SSA 或 aliased 写入。它结合 `vars_written`
和显式 operation 字段，确保 `SET_VAR_FIELD`、`SET_VAR_SPLIT` 与 `SET_VAR_ALIASED(_FIELD)`
不会被静默视作只读。

### `instruction_reads_variable`

**签名**

```python
instruction_reads_variable(instruction, variable)
```

保守检测对一个 variable 的完整、字段、split、SSA 或 aliased 读取。它结合显式表达式形态和
`vars_read`，避免 observer 证明遗漏 BN 只以 variable operand 暴露的读取。

### `expression_may_address_variable`

**签名**

```python
expression_may_address_variable(mlil, expression, variable)
```

保守跟随表达式树及所有可用的完整、字段、split、SSA 或 aliased variable 定义，判断一个
variable 的 `ADDRESS_OF` 或 `ADDRESS_OF_FIELD` 是否可到达表达式。遍历按真实 variable
equality 防环且无固定深度上限；不完整定义查询视作可能 alias。取得 holder 地址时也会跟随
holder 定义，故可识别 `holder = &state; call(&holder)`。这是 may-alias guard，不证明指针
是精确 store 目的地。

### `variable_address_escapes`

**签名**

```python
variable_address_escapes(mlil, variable)
```

返回显式 store 是否发布、或未知内存效果操作是否接收并可保留一个 variable 的直接/定义派生
地址。去平坦化规划器以此函数级事实避免后续无参数 call 或未解析 store 静默重新取得并修改
调度器状态。

### `address_escape_checker`

**签名**

```python
address_escape_checker(mlil)
```

构建当前 MLIL 作用域的逃逸谓词。首次查询在一个共享 alias worklist 中遍历所有显式 store
与未知内存效果根，再缓存语义 base-variable 答案。不完整定义查询使每个答案保守为真。MLIL
修改、finalize、copy 或重新分析后必须丢弃该谓词。

### `current_non_ssa_instruction`

**签名**

```python
current_non_ssa_instruction(mlil, ssa_instruction)
```

经 `non_ssa_form` 映射 SSA 指令，要求非负精确 instruction index，并针对当前非 SSA MLIL
验证 operation、expression identity 和地址。任一 identity 检查失败返回 `None`。

### `has_unknown_memory_effect`

**签名**

```python
has_unknown_memory_effect(instruction)
```

识别显式 `STORE` 处理之外可能修改内存的 call、tail-call、syscall、intrinsic、trap、
breakpoint 和未实现 memory operation。去平坦化规划器将其与
`expression_may_address_variable` 结合；向这些 operation 传入可能状态指针会使具体令牌
证明失效。

### `has_unmodeled_semantics`

**签名**

```python
has_unmodeled_semantics(instruction)
```

识别指令表达式树任意位置的 `MLIL_UNIMPL` 与 `MLIL_UNIMPL_MEM`。它们语义不可得，含任一
operation 的去平坦化转移不能证明稳定状态令牌；即使已知没有状态地址逃逸，也必须按失败
即关闭处理。

### `state_token`

**签名**

```python
state_token(const_expr, fallback_size=None)
```

从 MLIL 常量表达式返回 `(value, size_in_bytes)` 令牌。常量无 size 且未给 `fallback_size` 时，
负值或宽于 32 位的值用 size `8`，其余用 size `4`。

### `comparison_parts`

**签名**

```python
comparison_parts(condition)
```

解析一个受支持的变量/常量 MLIL 比较，不丢失操作数顺序、令牌宽度或有符号性。返回含
`op`、`var`、`bound`、`var_on_left` 的字典；形态或 operation 不支持时返回 `None`。
`bound` 是 `state_token` 规范化的 `(value, size_in_bytes)`；`var_on_left` 表示原式是否为
`var op constant` 而非 `constant op var`。

支持 `MLIL_CMP_E`、`MLIL_CMP_NE` 及有符号/无符号 `MLIL_CMP_SLT`、`ULT`、`SLE`、`ULE`、
`SGE`、`UGE`、`SGT`、`UGT`。要求一个由 `direct_var_from_expr` 接受的精确整变量表达式和
一个 `MLIL_CONST`；不跟随 variable 定义，不接受变量/变量比较。保留原操作数顺序，不反转或
规范化比较 operation；不推断范围，也不修改 IL。

### `evaluate_comparison`

**签名**

```python
evaluate_comparison(parts, token)
```

以比较的 bitvector 语义，计算一个具体规范化状态令牌与已解析调度器比较部分。`parts` 为
`comparison_parts` 返回的字典，`token` 为规范化 `(value, size_in_bytes)`。支持且同宽时返回
`True`/`False`；令牌与 bound 宽度不同返回 `None`。

使用 `var_on_left` 保留原操作数顺序。`SLT`、`SLE`、`SGE`、`SGT` 在令牌宽度下按二补码有
符号整数计算；无符号与相等比较用规范化掩码值。仅计算给定具体令牌，不求解符号区间、
不选择 CFG 边，也不接纳 `comparison_parts` 外部传入的畸形 `parts`。

### `all_paths_reach_stops`

**签名**

```python
all_paths_reach_stops(basic_blocks, scope, stop_starts)
```

证明所选块作用域内每条 CFG 路径均终止于给定 stop block 之一。使用最小不动点：只有所有
后继均为 stop 或已证明块时，作用域块才被证明。拒绝 terminal block、两个集合外的边，以及
允许无限路径的环，即使另一路可达 stop。仅证明终止，不选择或计算调度器目标。

### `row_local_copy_chain`

**签名**

```python
row_local_copy_chain(mlil, variable, row, use)
```

返回从调度器比较 variable 回溯到比较行共享输入 variable 的直接、等宽 variable copy 元组。
行内 alias 有多定义、不纯 source、环，或定义位于比较处/之后时返回 `None`。最终 variable
是调度器状态通道；其定义可位于原始块区域，是转移规划器的令牌证据，不属于调度器行 copy
chain。

### `all_paths_hit_blocks`

**签名**

```python
all_paths_hit_blocks(basic_blocks, starts, scope, hit_starts)
```

证明从所选入口块出发、位于作用域内的每条 CFG 路径都在离开作用域前执行指定 hit block。
进入 hit block 即满足，因为 MLIL 指令在终结器前按块顺序执行。使用最小不动点，故未命中就
到达 stop 或环的路径不被证明；不检查 hit block 写入的值。

### `dependency_variables`

**签名**

```python
dependency_variables(mlil, expressions, scope)
```

收集以 expressions 为根的定义链变量，只跟随所选基本块作用域内的定义。它跟随 BN 返回的
全部作用域内定义并防环；定义在作用域外的 variable 作为输入记录但不继续跟随。它不判定
到达定义可行性，也不证明表达式纯净。

### `region_until`

```python
region_until(start_bb, stop_starts)
```

返回从 `start_bb` 可达且不进入任何 stop block 的基本块起始地址。这个共享 CFG helper 取代
重复的 generic/driver walker。

### `variables_are_scope_local`

**签名**

```python
variables_are_scope_local(mlil, variables, scope)
```

检查所选 variable 在一个基本块作用域外没有读取或地址逃逸。它扫描作用域外表达式树中的
variable 读取和 `MLIL_ADDRESS_OF` / `MLIL_ADDRESS_OF_FIELD` 使用；对有无关用途的非 SSA
variable 刻意保守；不证明 dominance 或 memory alias。

### `scope_locality_checker`

**签名**

```python
scope_locality_checker(mlil)
```

构建当前 MLIL 作用域谓词，惰性索引每个语义 base variable 被读取或取地址的基本块。重复的
diamond 所有权检查由集合包含关系完成，而非重扫整函数。索引仅限本次调用，不得跨 MLIL
修改或重新分析。

### `definitions_cover_all_paths`

**签名**

```python
definitions_cover_all_paths(mlil, starts, scope, expressions)
```

证明每个有作用域内定义的依赖，都在从所选入口到每个作用域内使用的每条路径上先被定义。
它以 predecessor intersection 计算前向 must-defined 集；只在作用域外定义的 variable 作为
输入；有作用域内定义的 variable 必须在每条相关路径上建立。它补充令牌值一致性与终止检查，
不自行折叠值或选择到达定义。

### `walk_expr`

**签名**

```python
walk_expr(expr)
```

返回以 `expr` 为根的表达式树。`expr` 可为 Binary Ninja MLIL 表达式或指令；测试 double 必须
暴露兼容 `traverse` 方法。`expr` 为 `None` 时返回 `[]`。结构遍历直接委托给 Binary Ninja 的
`expr.traverse(...)`。

### `expression_has_operation`

**签名**

```python
expression_has_operation(expr, ops)
```

判断以 `expr` 为根的表达式树是否含任一所选 operation。`ops` 可为 Binary Ninja MLIL
operation enum、operation 名称字符串或两者的 iterable。任一访问节点匹配返回 `True`，否则
`False`。它使用 `walk_expr`，但不跟随 variable 定义；形态可能位于 `MLIL_VAR` 后时用
`expression_or_definitions_have_operation`。

### `expression_or_definitions_have_operation`

**签名**

```python
expression_or_definitions_have_operation(mlil, expr, ops, max_depth=16)
```

判断 `expr` 或任一跟随的 `MLIL_VAR` 定义是否含所选 operation。`mlil` 需支持
`get_var_definitions(var)`；`ops` 可为 enum、名称或 iterable；`max_depth` 限制递归 variable
定义深度。匹配返回 `True`，否则 `False`。它使用 `walk_expr_with_defs`，因此同样不做 CFG
路径推理或可行性过滤。

### `walk_expr_with_defs`

**签名**

```python
walk_expr_with_defs(mlil, expr, max_depth=16)
```

产生以 `expr` 为根的表达式树，并递归包含 `MLIL_VAR` 定义后的表达式。它对每棵访问树使用
`walk_expr`，以 expression object identity 与 variable identity 防环；跟随一个 variable
返回的每个定义，但每个 variable 仅跟随一次。不做 CFG 路径推理，也不判定哪个定义可行。

### `constant_value`

**签名**

```python
constant_value(mlil, expr)
```

在剥离单定义 variable 后恢复直接 MLIL 常量值。`mlil` 需支持 `get_var_definitions(var)`；
返回表达式 `constant`，若剥离表达式不在 `CONST_OPERATIONS` 中则返回 `None`。调用
`peel_var_definitions(...)`，因此已经要求恰好一个完整 variable 定义；零/多定义时停止。
不计算算术、load 或 value-set；需要更广折叠时用 `fold_constant_value`。

### `expression_scalar_value`

**签名**

```python
expression_scalar_value(mlil, expr)
```

在剥离单定义 variable 后恢复直接 MLIL 常量或 Binary Ninja 单值结果。返回整数；表达式既非
直接常量、也不暴露 `ConstantValue`、`ConstantPointerValue` 或 `ImportedAddressValue` 时返回
`None`。它不计算算术、load、PHI 候选或内存；需要这些语义时用 `fold_constant_value` 或
profile 私有值引擎。返回 BN 报告的值，不应用 U48/U64 掩码。

### `constant_address`

**签名**

```python
constant_address(mlil, expr, depth=0, max_depth=32, address_mask=None)
```

恢复常量地址表达式，可选应用调用者给出的地址掩码。`depth` 是当前递归深度（通常保留 `0`），
`max_depth` 限制递归。返回整数地址或 `None`。仅恰好一个定义时剥离 variable；支持直接常量
和常量 `MLIL_ADD` / `MLIL_SUB`。不会隐式应用内置样本的 U48 模型；该模型属于二进制公式时，
调用点必须传 `address_mask=U48`。

### `load_slot_address`

**签名**

```python
load_slot_address(mlil, expr, width=8, address_mask=None)
```

恢复 MLIL 槽位 load 读取的常量地址。`expr` 可为 load 表达式或可剥离到 load 的 variable；
`width` 是要求的字节宽度，`address_mask` 可选。返回恢复出的槽位地址（含存在时的
`MLIL_LOAD_STRUCT` offset），形态不匹配返回 `None`。仅接受 `SLOT_LOAD_OPERATIONS`、要求
`expr.size == width`，struct load 的 `offset` 必须为整数；不会隐式应用 U48，需时显式传
`address_mask=U48`。

### `load_slot_offsets`

**签名**

```python
load_slot_offsets(mlil, expr, width=8, address_mask=None, max_depth=32)
```

恢复常量 slot-load 地址及其周围常量 `MLIL_ADD` / `MLIL_SUB` offset。返回
`(slot_addr, offset)` 元组列表；空列表表示未找到匹配形态。通过
`peel_var_definitions(...)` 剥离 variable，以 `load_slot_address` 找 base slot load，仅折叠
其周围常量加/减 offset；不判定一个槽位是否应成为 const，也不校验已解析地址。

### `iter_load_slot_offsets`

**签名**

```python
iter_load_slot_offsets(mlil, width=8, address_mask=None)
```

扫描 MLIL 函数内每个含可恢复 slot-load 加 offset 的表达式，产生
`(expr, use_addr, slot_addr, offset)`。`mlil` 需暴露 `instructions`，`width` 为要求槽位 load
宽度，`address_mask` 可选。它以 `walk_expr` 遍历每条指令；有表达式地址时 `use_addr` 是该地址，
否则是所在指令地址；识别委托给 `load_slot_offsets`。同一槽位可能产生多个表达式，规划器必须
在自己的语义层去重。

### `iter_calls`

**签名**

```python
iter_calls(mlil, ops=CALL_OPERATIONS)
```

从 MLIL 函数产生 call-like 指令。`mlil` 需暴露 `instructions`；`ops` 可为 MLIL operation
enum/name 或其 iterable，默认 `CALL_OPERATIONS`。仅扫描顶层 MLIL 指令，不扫描嵌套表达式，
不解析目标，也不将调用分类为 direct/indirect。

### `iter_direct_calls`

**签名**

```python
iter_direct_calls(mlil)
```

产生目标具有可恢复标量值的 MLIL call-like 指令。`mlil` 需暴露 `instructions`，variable
目标还需 `get_var_definitions(var)`。它用 `iter_calls` 遍历，再用
`expression_scalar_value(mlil, call.dest)` 分类目标；不校验目标是否可执行、是否有函数类型或
是否在特定 BinaryView section，二进制特定检查仍属 profile。

### `mlil_stores_to_address`

**签名**

```python
mlil_stores_to_address(mlil, addr, address_mask=None)
```

检测 MLIL 函数是否向常量目的地址 store。`addr` 是待匹配整数地址，`address_mask` 会传给
`constant_address`。找到匹配 store 返回 `True`，否则 `False`。它以 `walk_expr` 遍历每条指令
下的全部表达式，检查 `STORE_OPERATIONS`，并用 `constant_address` 恢复每个 store 目的地，
因此 variable 剥离仅限单定义。它不判定槽位是否应成为 const，二进制特定规则仍属规划器。

### `slot_has_no_stores`

**签名**

```python
slot_has_no_stores(bv, current_mlil, slot_addr, address_mask=None)
```

按失败即关闭方式证明当前 MLIL 和该槽位引用的每个已分析函数均未向槽位 store。仅当每个当前
code reference 都能解析到有可用 MLIL 的函数，且没有任何函数向槽位 store 时返回 `True`；
缺失引用所有权、MLIL 不可用或引用查询失败均返回 `False`。

### `iter_indirect_calls`

**签名**

```python
iter_indirect_calls(mlil)
```

产生目标尚非常量的 MLIL call 指令。`mlil` 需暴露 `instructions`；为 `None` 时产生空迭代器。
仅包含 `CALL_OPERATIONS` 中精确 operation，跳过目标 operation 在 `CONST_OPERATIONS` 中的调用；
不解析 callee，也不修改调用目标。

### `peel_var_definitions`

**签名**

```python
peel_var_definitions(
    mlil,
    expr,
    trail=None,
    max_depth=64,
)
```

沿 MLIL variable 定义跟随 `MLIL_VAR` 表达式。`mlil` 需支持 `get_var_definitions(var)`；
`trail` 非空时按遍历顺序追加定义，`max_depth` 限制跳数。返回剥离后的表达式；缺失、多重、
部分/字段或不支持定义、BN API 异常时返回当前表达式。只跟随 operation 为 `MLIL_VAR` 的表达式，
每跳要求恰好一个完整 `MLIL_SET_VAR` 定义；不做 PHI/路径推理。

### `fold_constant_value`

**签名**

```python
fold_constant_value(bv, mlil, expr, depth=0, max_depth=32, load_address_mask=None)
```

为当前调用目标式恢复尽力而为的单一 MLIL 整数值。`bv` 需支持 `read(addr, size)` 读取镜像内存，
`mlil` 需支持 `get_var_definitions(var)`；`depth` 通常为 `0`，`max_depth` 限制递归，
`load_address_mask` 可在内存读取前掩码地址。成功返回整数，失败返回 `None`。

支持常量、variable 定义、`MLIL_ADD`、`MLIL_SUB`、`MLIL_MUL`、零/符号/低位转换、MLIL load，
以及 `ConstantValue`、`ConstantPointerValue`、`ImportedAddressValue`。多条完整
`MLIL_SET_VAR` 定义仅在每条均完整折叠为同一值时接纳，否则返回 `None`。算术和转换按当前
表达式 BN 宽度掩码；`MLIL_LOAD_STRUCT` 读取前包含字段 offset。load 地址不自动掩码；profile
或 pass 应用内置 48 位地址公式时必须传 `load_address_mask=U48`。经
`memory.read_uint_le` 的无效或短读取返回 `None`。

### `cleanup_roots_for_expr`

**签名**

```python
cleanup_roots_for_expr(mlil, expr)
```

收集一个表达式读取的 variable 定义指令索引。`mlil` 需支持 `get_var_definitions(var)`；返回
MLIL instruction index 的 `set[int]`。它以 `walk_expr` 遍历，对每个 `MLIL_VAR` 加入其定义
operation 位于 `SET_VAR_OPERATIONS` 的 `instr_index`。返回的是 instruction index 而非
expression index；清理后端会在最终 `replace_expr` 前映射 SSA/非 SSA 形式。它不判定根是否
可安全 NOP；该存活性检查由 `phase_cleanup.settle_cleanup_decode` 在同一份当前 MLIL overlay
中重规划到空负责。重复计划、应用失败或按当前 IL 指令数计算的收敛上界都会保持 receipt
打开；即使本轮局部收敛，只要实际
NOP 过，receipt 仍必须留给下一工作流轮次重新证明。结果只能
在同一次当前 MLIL 规划中使用，不能写入 recovery fact 或跨重新分析保存。

### `set_roots_before`

**签名**

```python
set_roots_before(mlil, site_addrs)
```

收集阶段自有站点紧前方连续纯赋值的 instruction index。`mlil` 需暴露 `basic_blocks` 和按
index 的指令访问，`site_addrs` 是当前阶段拥有的指令地址 iterable。返回赋值 index 的
`set[int]`；`mlil` 或 `site_addrs` 为空时为空集。它独立扫描每个基本块，对 `address` 在
`site_addrs` 的每条指令在同块反向收集 operation 属于 `SET_VAR_OPERATIONS` 的连续赋值，
遇到第一条非赋值停止；不检查连续块前缀外的数据流。

### `set_roots_before_instruction`

**签名**

```python
set_roots_before_instruction(mlil, instruction)
```

为一条精确当前 MLIL 指令收集相同的连续赋值前缀。不同于按地址的 receipt helper，此版本
绝不扫描其他块或共享同一机器地址的指令；指令无法在当前基本块中唯一映射时返回空集。分支
条件翻译在证明源 `MLIL_IF` 后使用这一精确形式；阶段清理仍决定收集定义中哪些实际死亡。

## `memory`

Memory helper 执行小型 BinaryView 读取和地址检查。

### `read_uint_le`

**签名**

```python
read_uint_le(bv, addr, width)
```

从 BinaryView 读取无符号小端整数。`bv` 需支持 `read(addr, width)`；`addr` 为读取地址，
`width` 为正字节宽度。返回解码整数；无效读取、`None` 数据或短读取等普通未命中返回 `None`。
`width <= 0` 抛出 `ValueError`，错误 helper 用法必须显性失败；捕获 BinaryView 读取异常并返回
`None`；读取必须恰好返回 `width` 字节。

### `read_u8`

**签名**

```python
read_u8(bv, addr)
```

读取一个无符号字节，返回整数或 `None`；委托给 `read_uint_le(bv, addr, 1)`。

### `read_u16le`

**签名**

```python
read_u16le(bv, addr)
```

读取 16 位无符号小端整数，返回整数或 `None`；委托给 `read_uint_le(bv, addr, 2)`。

### `read_u32le`

**签名**

```python
read_u32le(bv, addr)
```

读取 32 位无符号小端整数，返回整数或 `None`；委托给 `read_uint_le(bv, addr, 4)`。

### `read_u64le`

**签名**

```python
read_u64le(bv, addr)
```

读取 64 位无符号小端整数，返回整数或 `None`；委托给 `read_uint_le(bv, addr, 8)`。

### `read_qword_slot`

**签名**

```python
read_qword_slot(bv, addr)
```

读取 8 字节 slot 值，返回整数 qword 或 `None`。它是 `read_u64le` 的别名；不校验 section
归属或 qword 是否为 pointer。

### `is_mapped_address`

**签名**

```python
is_mapped_address(bv, addr)
```

检查地址是否属于 BinaryView 地址空间。`bv` 需支持 `is_valid_offset(addr)`；`addr` 非 `None`
且该调用为 truthy 时返回 `True`，否则 `False`。捕获 BinaryView 异常并返回 `False`；不要求
地址处有 symbol 或 function。

### `is_executable_target`

**签名**

```python
is_executable_target(bv, addr)
```

检查地址是否按当前架构对齐且被 Binary Ninja 标为可执行。`bv` 需支持
`is_offset_executable(addr)` 及可选 `arch.instr_alignment`。返回 `True`/`False`；捕获 BinaryView
异常并返回 `False`，不会接纳仅映射的数据地址。

### `is_known_callee`

**签名**

```python
is_known_callee(bv, addr)
```

检查 Binary Ninja 是否有具体 callee 的代码证据。`bv` 需支持地址/可执行查询、function 查询及
symbol 查询。地址已映射且有 function、可执行 mapping 或显式 function-like `SymbolType` 时
返回 `True`，否则 `False`。普通 `DataSymbol` 或 `ExternalSymbol` 不是 callee 证据；捕获
BinaryView 异常并返回 `False`。

### `sections_at`

**签名**

```python
sections_at(bv, addr)
```

返回覆盖地址的 section。`bv` 需支持 `get_sections_at(addr)`；返回 section 对象元组，未命中或
BinaryView 异常时为 `()`，并将 falsey BN 结果规范为空元组。

### `in_section`

**签名**

```python
in_section(bv, addr, names)
```

检查地址是否属于给定名称之一的 section。`names` 可为一个 section 名称字符串或名称
iterable。任一覆盖 `addr` 的 section 其 `name` 匹配时返回 `True`，否则 `False`；单字符串会
转为单元素集合。它使用 `sections_at`，因此 BinaryView 异常视为不匹配。

## `facts`

Fact helper 构造工作流与 pass 后端消费的 dict 形态。

### `MalformedRecoveryFact`

**签名**

```python
class MalformedRecoveryFact(ValueError)
```

构造恢复事实时报告错误 helper 用法。使用标准 `ValueError` 构造参数，返回异常实例。仅由
fact builder 在必需字段畸形或 iterable 输入无效时抛出；profile 识别中的形态未命中通常应在
调用 fact builder 前返回 `[]` 或 `None`。

### `branch_fact`

**签名**

```python
branch_fact(
    jump_il,
    targets,
)
```

构造间接分支恢复事实。`jump_il` 是必需的当前 LLIL 指令见证，保留以供精确修改边界校验；
`source` 从 `jump_il.address`、`dest_expr_index` 从 `jump_il.dest.expr_index` 得出。`targets`
是目标地址 iterable。

返回 dict：`source`（整数分支地址）、`dest_expr_index`（整数 LLIL expression index）、
`targets`（已排序且唯一的整数目标 tuple）和 `jump_il`（传入的当前指令见证）。缺失见证、
派生坐标不是精确非负整数、`targets` 不可迭代或为空时抛出 `MalformedRecoveryFact`；地址/index
字段拒绝 `bool` 和负整数。它不在 BinaryView 中校验目标地址，也不调用
`Function.set_user_indirect_branches`；后者由工作流拥有。分支 cleanup 根只能在当前 MLIL
翻译 callback 中生成，不能作为 profile fact 保存。

### `call_fact`

**签名**

```python
call_fact(
    call_il,
    target,
    decode_def=None,
    call_addr=None,
)
```

构造间接调用恢复事实。`call_il` 为 MLIL call 指令，`target` 为具体 callee 地址；`decode_def`
是可选的、计算目标的描述性 MLIL 定义见证，不是改写目标。`call_addr` 可选覆盖调用点地址，
省略时使用 `call_il.address`。

返回含原 `call_il`、整数 `call_addr`、整数 `target` 与给定 `decode_def` 或 `None` 的 dict。
`call_il` 为 `None`、`call_addr` 缺失/为负/非整数或 `target` 非非负整数时抛出
`MalformedRecoveryFact`。它不校验 `target` 确为调用目标，也不授予清理权限；后端在 mutation
boundary 从当前 call 的精确 SSA 到达定义独占推导 cleanup slice。调用类型调整和 MLIL 改写也
属于工作流和 pass 后端。

### `global_constant_fact`

**签名**

```python
global_constant_fact(slot_addr, type_name)
```

构造全局常量槽位恢复事实。`slot_addr` 为全局槽位地址，`type_name` 为待应用的非空类型名
字符串。返回含整数 `slot_addr` 与类型名字符串 `type` 的 dict。槽位地址无效或
`type_name` 为空/非字符串时抛出 `MalformedRecoveryFact`。它不定义 data variable；BinaryView
修改和函数全局阶段回执归工作流。它不校验 section、store 行为或地址有效性；与 profile
有关的原始值、已解析地址或使用点等识别证据不属于该事实。

### `string_decrypt_fact`

**签名**

```python
string_decrypt_fact(call_addr, src_addr, dst_addr, plaintext)
```

构造字符串解密恢复事实。`call_addr` 为解密调用点地址，`src_addr` 为源加密 blob 地址，
`dst_addr` 为目标 buffer 地址，`plaintext` 为 `bytes` 或 `bytearray` 恢复结果。返回含整数
`call_addr`、`src_addr`、`dst_addr` 和不可变 `bytes` `plaintext` 的 dict。地址字段非非负
整数或 `plaintext` 非 `bytes`/`bytearray` 时抛出 `MalformedRecoveryFact`；`bytearray` 会转为
`bytes`。它不写注释，注释由字符串解密后端负责。
