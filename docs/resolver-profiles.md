# 解析配置（profile）

> [!WARNING]
> 本页是当前 bundled profile 实现的迁移参考，不再定义项目方向。ADR-0015 至 ADR-0020 已
> 取代内置 profile 架构：每个具体样本应成为独立外部插件，通过唯一的 `SampleSemantics`
> provider 接口注册；不得新增内置 profile、默认别名、样本家族或自动识别逻辑。本页中的
> 现有算法和证据要求可用于拆分样本插件，但不能作为继续扩展旧 registry 的依据。

解析 profile 让 DispatchThis 适配一个二进制，同时不把工作流所有权移入 profile 特定代码。

通过 `analysis.plugins.dispatchThis.resolverProfile` 按 BinaryView 选择 profile。函数工作流
启用状态仍按函数控制；选择 profile 不会为视图中每个函数启用 DispatchThis。

函数阶段状态记录产生其恢复证据的 profile ID。只要视图内任一函数仍含分支、调用、清理或
全局恢复证据，UI 就拒绝切换 profile。工作流也会对没有来源的旧证据或绑定到其他 profile
的证据按失败即关闭处理。空函数状态没有可复用的分析断言，因此可重新绑定。

## 代理工作流

不要先改代码。先收集以下二进制事实；二进制未使用的字段填写 `none`。

```text
binary:
  file:
  architecture:
  platform:
  profile_id:
  profile_name:

capabilities:
  branch_gadget:
  call_gadget:
  global_constants:
  correlated_stores:
  deflatten:
  string_decrypt:

branch_gadget:
  unresolved_jump_addr:
  llil_excerpt:
  notes:
  decoded_targets:

call_gadget:
  indirect_call_addr:
  mlil_excerpt:
  notes:
  decoded_callee:

global_constants:
  slot_addr:
  mlil_excerpt:
  notes:
  resolved_addr:

correlated_stores:
  join_store_addr:
  mlil_excerpt:
  notes:
  predecessor_arms:

deflatten:
  dispatcher_state:
  mlil_excerpt:
  notes:
  redirection_count:

string_decrypt:
  call_addr:
  callee_addr:
  source_blob_addr:
  expected_plaintext:

validation:
  pytest:
  bn_commands:
  raw_binary_checks:
```

然后实现满足这些事实的最小 profile：

1. 新增 `plugins/DispatchThis/profiles/<profile_id>.py`。
2. 定义元数据，并且只定义该二进制支持的语义 hook。
3. 省略不支持的 hook；注册表会将其规范为空结果。
4. 在 `plugins/DispatchThis/profiles/__init__.py` 注册模块。
5. 完成下方的完成定义。

`llil_excerpt` 和 `mlil_excerpt` 必须是从目标复制的原始 Binary Ninja IL。notes 可以说明
解释，但不能替代原始 IL。片段应短，但必须包括可复现形态所需的地址、指令索引、变量和
关键表达式。

二进制特定匹配应保留在 profile 文件中，直到两个 profile 都需要相同 helper 或 profile
难以阅读。共享 profile 代码必须移至 `helpers/` 下稳定 helper 模块，或显式委托给另一
具名 profile。不得添加 `profiles/_shared.py`、`profiles/<family>_shared.py`，或任何解析
引擎/DSL/base class。共享 `passes/` 代码服务于稳定的工作流级能力，而非单一二进制或推测
性的复用。

## 命名

默认每个二进制一个 profile。`PROFILE_ID` 必须是稳定的小写 snake_case，例如
`dy_libdyzznb_202607` 或 `dyzznb_main`。

不得使用 `sample1`、`new_profile`、`current` 或 `default2` 等模糊名称。不得包含完整本地
路径、用户名、客户名或其他敏感项目标签。`PROFILE_NAME` 可以面向人类阅读，
`PROFILE_DESCRIPTION` 应说明二进制身份和支持能力。

## 敏感信息

profile 代码、元数据、测试、注释和能力矩阵不得包含本地绝对路径、用户名、客户名、私有
样本来源或其他敏感项目标签。需要可追溯性时，使用文件 basename、日期、hash prefix 或
其他非敏感标识。

## 能力矩阵

每个二进制 profile 都必须声明哪些语义 hook 是自定义、别名或刻意省略。将下列注释放在
profile 模块顶部附近：

```text
Supported:
- branch gadget: custom
- indirect call gadget: alias valorant_2_6
- global constants: custom
- correlated stores: omitted
- deflatten: alias dyzznb
- string decrypt: omitted

Validation:
- branch: 0x...
- call: 0x...
```

这样可区分“该二进制不需要”与“尚未实现”。

## 复用

二进制 profile 只能通过稳定 helper 模块复用行为，或从另一个具名 profile 显式别名 hook：

```python
from . import dyzznb

resolve_branch_gadget = dyzznb.resolve_branch_gadget
```

只有 profile 有意改变参数或行为时才使用 wrapper。

profile 不得导入会应用 IL 改写、提交 workflow context 或触发重新分析的后端，也不得直接
调用这些 mutation API。具名 profile 可以调用既有只读 planner，将该样本的 gadget 形态、公式
和验证条件绑定为 recovery fact 或 plan；真正的 apply 仍留在 pass/workflow。

不得添加 profile base class、factory、mixin、共享 profile 模块或自动继承。profile 模块必须
明确 hook 所有权：记录哪些 hook 复用另一个 profile、哪些 hook 是二进制特定的。

新增二进制 profile 时不得扩大 `profiles/default.py` 的行为；它只保留历史设置的兼容别名。
应通过具名 profile 或稳定 helper 复用 hook。为了新二进制扩大兼容 profile 会有回归已有视图
设置的风险。

## 辅助函数编写路径

Profile helper 是检查 primitive，不是解析引擎。解析 profile 仍拥有二进制特定识别、目标
公式和它返回的恢复事实；恢复后端拥有 CFG 恢复、调用目标应用、全局槽位类型设置、分支
条件翻译、IL 改写、阶段回执及清理应用。

内置 profile 应在模块级导入 helper 模块，并通过模块名调用：

```python
from ..helpers import facts, llil, memory, mlil
```

这是稳定导入面。不要导入私有 helper 实现细节，也不要围绕 helper 构建 profile base class、
模式 DSL、自动解析引擎或外部 profile loader。详细 helper API 签名与行为见
[`API.md`](API.md)。

按 IL 层和用途使用 helper 模块：

- `llil`：间接跳转遍历、寄存器定义剥离，以及对 PHI 感知的具体常量候选集合
  `const_values`。
- `mlil`：直接/间接调用遍历、变量定义剥离、常量/值提取、单值常量折叠、表达式遍历和
  operation 查询、变量/状态令牌规范化、具体调度器比较解析/计算、地址/槽位提取、store
  检查。
- `memory`：显式宽度的小端读取、section 检查、目标或地址校验。
- `facts`：分支、调用、全局常量和字符串解密恢复事实构造器。

hook 代码应聚焦二进制形态。例如间接调用 hook 可用 MLIL helper 找到候选、用 facts helper
构造结果，但调用类型调整和清理留给工作流：

```python
def resolve_call_gadget(bv, mlil_func):
    out = []
    for call_il in mlil.iter_indirect_calls(mlil_func):
        target = mlil.fold_constant_value(bv, mlil_func, call_il.dest)
        if target is None or not memory.is_known_callee(bv, target):
            continue
        out.append(facts.call_fact(call_il, target))
    return out
```

profile 不提供 cleanup 指令索引。分支翻译器和调用后端各自在同一次当前 MLIL callback 中
构造精确 slice；表达式索引仅是后端替换细节，绝不跨重新分析写入 fact 或 session state。

`llil.const_values(bv, ssa, expr)` 返回完整具体候选集合；任一语义路径未知时返回 `None`。
多个值表示表达式有多个可行候选，常由 PHI 合并或循环携带值产生。profile 公式需要恰好
一个表基址、key 或 offset 时，调用点必须同时检查完整性和基数：

```python
offsets = llil.const_values(bv, ssa, offset_expr)
if offsets is None or len(offsets) != 1:
    return []
offset = next(iter(offsets))
```

`const_values` 不做 CFG 路径消歧。profile 只可根据完整的二进制特定路径证据缩小 PHI；
绝不能保留其他未知结果中已知的臂。

全局常量 helper 提供检查 primitive：遍历 MLIL、提取常量槽位地址、读取 qword 槽位、检查
section、检测 store 和构造全局常量事实；仍由 profile 决定哪些槽位使用形态、offset、section
和已解析地址有效。不得将自动全局常量规划器移入 `helpers`。

字符串解密和去平坦化算法不属于稳定 helper 表面。profile 可使用可复用 helper primitive
实现 `plan_string_decrypt_calls` 和 `plan_deflatten_redirections`，但注释写入和 MLIL 改写
仍是后端职责。

## 适配失败时

按以下顺序升级：

1. 重新检查二进制事实。缺失或概述性 IL 不足以安全实现形态。
2. 为漏识别该形态的 hook 添加或收紧一个失败测试。
3. 扩展该二进制 profile 的私有 helper。
4. 两个二进制 profile 都需要相同扩展时，将它移至稳定 helper 模块，或显式将 hook 委托
   给拥有该形态的 profile。
5. 若阻碍是工作流回执、清理重放、阶段顺序或 BN 修改边界，停止 profile 工作，先诊断或
   重构该共享契约。

不得为了让一个二进制 profile 通过就修改 `workflow.py`，除非这是一项有意的通用契约变更。

## 契约

每个 profile 模块必须暴露元数据：

```python
PROFILE_ID = "dyzznb_main_202607"
PROFILE_NAME = "DYZZNB main 2026-07"
PROFILE_DESCRIPTION = "Rules for dyzznb_main_202607 branch and call gadgets."
```

可暴露下列六个语义能力 hook 中任意一个：

```python

def resolve_branch_gadget(bv, llil, known_targets=None):
    return []

def resolve_call_gadget(bv, mlil):
    return []

def plan_global_constant_slots(bv, mlil):
    return []

def correlated_stores(query):
    return CompleteBatch(())

def plan_deflatten_redirections(bv, func, mlil):
    return []

def plan_string_decrypt_calls(bv, func, mlil, mlil_stable):
    return []
```

缺失 hook 表示 profile 不支持该能力；`resolver_profile_from_module()` 为面向工作流的
`ResolverProfile` 提供一个共享无结果函数。存在但不可调用的 hook 属性会作为 profile
错误拒绝。这样 hook 名保持语义化，同时不要求样板 no-op 函数、profile base class 或
dispatch DSL。

## 恢复事实

`resolve_branch_gadget(bv, llil, known_targets=None)` 返回分支事实：

```python
{
    "source": 0x1000,
    "dest_expr_index": 42,
    "targets": (0x2000, 0x3000),
    "jump_il": jump_il,
}
```

必须从精确当前 LLIL 见证构造事实，避免重复坐标产生不一致：

```python
return facts.branch_fact(jump_il, targets)
```

`source` 从 `jump_il.address` 得出，`dest_expr_index` 从 `jump_il.dest.expr_index` 得出。
`targets` 必须包含有效目标地址。`dest_expr_index` 仅用于当前 LLIL 展示改写；工作流拥有
`Function.set_user_indirect_branches`。内置解析器还保留精确当前 `jump_il` 见证；任一改写或元数据提交前，后端拒绝过期、
缺失或同源冲突见证。工作流仅向完整目标元组与 Binary Ninja 当前非自动用户分支元数据精确
一致的回执提供 `known_targets`。解析器可将这些源作为已验证前沿跳过，但必须重新识别其他
所有源。调用者不得传入未验证缓存：仅回执、缺失、自动、子集、超集或已变更元数据不是
`known_targets`，也绝不是当前解码失败的回退。

`resolve_call_gadget(bv, mlil)` 返回调用事实：

```python
{
    "call_il": call_il,
    "call_addr": call_il.address,
    "target": 0x5000,
    "decode_def": decode_def,
}
```

工作流拥有调用类型调整和清理回执处理。后端将 `call_il` 和描述性 `decode_def` 重新绑定到
精确当前非 SSA MLIL，但仅改写 `call_il.dest`。清理前，后端从当前调用的完整 SSA 到达定义
切片推导 root 和可移除 load；部分、split 或 aliased 定义禁用清理并保持 cleanup receipt
开放。这样 profile 无需重复扫描、也不能用过期指令索引授权修改。

`plan_global_constant_slots(bv, mlil)` 返回全局常量事实：

```python
{
    "slot_addr": 0xA43D70,
    "type": "uint8_t const* const",
}
```

与二进制形态相关的证据（观察值、已解析地址或使用点等）保留在 profile 私有范围内。工作流
拥有 `BinaryView.define_user_data_var` 和函数全局阶段回执。

`correlated_stores(query)` 返回将 join-block store 移回拥有其关联值的前驱臂的
`CompleteBatch[CorrelatedStorePlan]`：

```python
CorrelatedStorePlan(
    store_il=store_il,
    join_block=join_block,
    size=4,
    arms=(
        CorrelatedStoreArm(
            predecessor=true_block,
            incoming_edge=true_edge,
            goto_il=true_goto,
            dest_expr=true_dest_expr,
            dest_addr=0xA000,
            src_expr=true_src_expr,
            src_addr=0xB000,
        ),
        ...,
    ),
)
```

provider 必须按精确 CFG incoming edge 配对 target/source PHI，不能依据 operand 位置；
core 在当前 MLIL 上重新验证所有 witness，并拥有原子 copy-transform。

`plan_deflatten_redirections(bv, func, mlil)` 返回去平坦化重定向计划：

```python
{
    "kind": "uncond",
    "exit_jumps": (jump_il,),
    "target_bb": target_block,
    "obb": original_block,
    "state_token": (0x1234, 4),
    "obsolete_state_writes": {123},
}
```

profile 识别二进制特定的调度器/状态写入形态。每个无条件计划必须包含该原始区域所有私有
调度器出口，且从每个出口进行的具体令牌重放都必须证明同一目标。条件计划中，每条臂内
每条路径都必须终止于调度器入口并建立同一令牌。被改写绕过的工作必须留在状态选择依赖链
上；已建模语义可保留在私有共享出口区域中，因为整个区域仍会执行。多个有效候选必须被
拒绝，而非隐式排序。支持的变量/常量调度器比较为 `E`、`NE` 及有符号/无符号
`LT`、`LE`、`GT`、`GE`；比较操作数顺序和令牌宽度属于证据。profile 无需求解符号区间。

`obsolete_state_writes` 是 `set[int]`，包含因该计划重定向而证明冗余的精确当前 MLIL
指令索引。目标证明和清理证明相互独立：目标不确定不产生计划；清理不确定产生带空集合的
有效计划。后端在一个原子 copy-transform 中校验并应用所有已选出口/条件改写和每个精确
NOP。工作流只在替换安装后发布 `dispatchthis_mlil_stable`；不发布令牌/变量清理 map。所选
条件臂的外部入口会拒绝计划，因为出口修改会影响没有证明的外来路径。通过指针识别状态
store 的 profile 必须要求一条完整、唯一、通向 `&state` 的定义链，且每个定义支配其使用；
指针变量的历史赋值不足以证明。

当 `exit_targets` 能直接从状态选择尾部重定向不同的臂 GOTO 且保留其执行时，条件计划将
`rewrite_mode` 设为 `arm_exits`。`rewrite_mode: condition` 捷径化 `if_il`，仅在完整私有
清理及被跳过状态通道不存在非调度器观察者的证明下有效。缺少该更强证明时，profile 必须
拒绝条件捷径。

profile 不得将任意赋值块归为调度器路由。只有 NOP/GOTO 路由和已证明状态依赖变量之间的
直接复制可作为保留令牌的操作重放。所选比较行遵守同一限制，且调度器派生临时变量不得在
调度器外被观察。

比较变量必须具有一条在其自身调度器行中更早的、唯一等宽整变量
`MLIL_VAR`/`MLIL_VAR_SSA` 直接复制链。所有所选行必须以同一状态输入结束这些链。仅在其他
位置追溯到状态的定义不足以证明，因为比较可能消费过期值。`SET_VAR_FIELD`、
`SET_VAR_SPLIT`、aliased 写入和 `STORE_STRUCT` 应视为可能状态修改；无法精确证明所得令牌
时，拒绝转移。`ADDRESS_OF_FIELD` 应与 `ADDRESS_OF` 一样视为地址逃逸。

IF 条件为谓词变量时，将其 SSA 定义经 `non_ssa_form` 映射，验证同一行更早的精确当前非
SSA 指令，并将该比较指令作为复制链使用点。比较之后执行的状态复制不能证明重放。收到
可能状态指针的 call、syscall 和 intrinsic 即使先前写入为常量，也会使令牌失效。状态地址
存入内存后，即便后续未知调用没有显式指针参数，也都是可能状态修改。唯一且保宽的定义链
支配 store 时可支持精确零偏移指针复制；字段值、截断复制及其他指针算术仍是可能修改，
必须拒绝转移。

profile 和共享 helper 必须使用 Binary Ninja 真实 identity/equality，而非 `str` 或 `repr`
显示名来键控变量/register。辅助比较行只有完整前缀通过路由纯度校验后，才可视为调度器
块；否则它们仍可见于观察者证明。地址逃逸包括指针存入内存或经取地址 holder 保留。逃逸
后未知效果和非精确 store 会拒绝转移；`MLIL_UNIMPL` 和 `MLIL_UNIMPL_MEM` 无条件拒绝。

`plan_string_decrypt_calls(bv, func, mlil, mlil_stable)` 返回字符串事实：

```python
{
    "call_addr": 0x9000,
    "src_addr": 0xA00000,
    "dst_addr": 0xB00000,
    "plaintext": b"hello",
}
```

工作流通过字符串解密 pass 写注释。hook 可使用 `mlil_stable`，要求解密 callee 已先完成去
平坦化。

## 边界

profile 是纯识别器，不得调用：

- `Function.set_user_indirect_branches`
- `Function.set_call_type_adjustment`
- `BinaryView.add_analysis_completion_event`
- `BinaryView.define_user_data_var`
- `replace_expr`、`finalize` 或 `generate_ssa_form` 等 MLIL 改写 API
- 注释写入 API

这些修改保留在工作流回调或既有 apply 函数中，使阶段回执、重新分析门控和清理失效保持
集中管理。

profile 不得自动检测或切换活动 profile。二进制需要其他 profile 时，应显式设置
`analysis.plugins.dispatchThis.resolverProfile`。

## 完成定义

- 二进制事实模板完整，未使用能力标为 `none`。
- profile 有稳定、非敏感的 ID、名称和说明。
- 能力矩阵标明自定义 hook、别名和省略能力。
- 每个声明的 hook 都可调用；不支持能力被省略。
- profile 已注册并通过解析器契约校验。
- 每个真实 hook 都有聚焦测试。
- `pytest -q` 通过。
- 完整重启 Binary Ninja 后，已在新打开的原始二进制上记录验证。
- 除非修改明确更新共享契约，`workflow.py` 与 mutation pass 未改动；`profiles/default.py`
  只允许同步既有具名 profile 的兼容别名。
- profile 代码、测试和文档不含敏感本地/项目信息。
