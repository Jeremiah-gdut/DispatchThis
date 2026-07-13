# 先增加 profile helper 基元，再考虑 resolver framework

DispatchThis 通过增加小型 profile helper 模块，降低每个 binary 的 resolver profile 开发成本。
这些模块处理重复的 BNIL 和 BinaryView 检查：LLIL 定义追溯、MLIL 定义追溯、常量折叠、
内存读取、目标校验、cleanup-root 收集和 recovery-fact 构建。

Helper 是 resolver profile 与 pass 的共享构件。resolver profile 仍负责 binary 特定的识别和
目标公式；workflow callback 仍负责 Binary Ninja mutation、phase receipt、IL translation 和
cleanup application。

Profile helper API 应清晰且有文档，因为未来可能支持 bundled profile 包之外的 profile
plugin。在该外部插件 surface 出现前，不需要兼容 shim；helper 发生破坏性变更时，更新
helper 文档和 bundled profile 即可。这些 API 可以直接暴露 Binary Ninja IL object；不要为了
隐藏 Binary Ninja API 而引入 wrapper type。

稳定的 import surface 是 helper package module：
`DispatchThis.helpers.llil`、`DispatchThis.helpers.mlil`、
`DispatchThis.helpers.memory` 和 `DispatchThis.helpers.facts`。profile 应优先使用
`from DispatchThis.helpers import llil, facts` 这类 module import，而非导入私有 helper
实现细节。首批 helper pass 面向以下 surface：

- `helpers.llil`：间接跳转迭代、寄存器定义剥离、`const_values`、支持 PHI 的常量
  candidate 和 branch fact 支持。
- `helpers.mlil`：间接调用迭代、变量定义剥离、单值常量折叠、expression walk、
  const-address extraction 与 cleanup-root discovery。
- `helpers.memory`：显式宽度读取、target/address validation、section 检查与 qword slot
  读取。
- `helpers.facts`：branch、call、global constant 与 string decrypt recovery-fact builder。

首批 helper module 应从现有 pass 中迁移可复用基元，而不是包装旧的 pass-local function。
Helper function 应持有共同边界条件：IL 缺失、SSA/non-SSA 映射、未解析定义、变量后的常量、
无效地址和带 live use 的 cleanup root。调用方不应重复这些防御性检查。PHI 处理是该 helper
contract 的一部分：LLIL 常量 helper 应在可能时处理 loop-carried PHI candidate value；MLIL
cleanup-root helper 应把 PHI node 视为 slice/liveness connector，但不能把 PHI 本身当作 NOP
target。在具体共享范围证明之前，CFG path 或 live-edge 消歧仍属于 profile 或 pass。

LLIL 常量折叠应优先使用类似 `const_values(...)` 的多值 API，返回所有具体 candidate 的
set。调用方若需要单一 key、base 或 slot，可检查该集合恰有一个值，而不是使用单值 helper。

MLIL call-target helper 起步时只做单值常量折叠，因为现有 call-target backend 期望每个
call fact 只有一个具体 callee。在具体样本提出需求前，不设计额外的 PHI 或多 candidate
call-target 行为。

Helper 可以跨 basic block 跟随 SSA definition，包括已支持的 PHI；但第一版不得进行任意 CFG
backward walk 或 path enumeration。优先使用 Binary Ninja 的 SSA def-use 信息，不要重建第二套
控制流分析。

迁移现有 pass 使用 helper，同时保持 `default` resolver profile 作为具名样本 profile 的兼容
delegate。这保持已有 BinaryView 设置的 workflow surface 稳定，并让样本规则归属明确，避免
只写文档却留下未使用的 scaffolding。

Helper function 将识别失败作为数据而非异常：shape mismatch、未解析常量、无效 target 或候选
instruction 缺失时返回 `None` 或空 collection。异常只用于错误 API usage，例如无效参数
类型或 malformed recovery fact。

低层 helper 不得记录正常的识别 miss。resolver profile 和 pass 决定何时一个跳过 site 值得
记录。Helper 可以 raise 或返回失败值，但不能让宽泛 candidate scan 产生大量日志。

保持 LLIL 与 MLIL helper 独立：`helpers.llil` 不得依赖 `helpers.mlil`，
`helpers.mlil` 不得依赖 `helpers.llil`；共享的 BinaryView 或 recovery-fact helper
放入 `helpers.memory` 或 `helpers.facts`。

首个 helper surface 聚焦目标恢复和 cleanup-root 收集，不涉及 IL translation 或通用 IL
改写。branch translation、call target application、deflatten rewrite 和 cleanup application
仍属 workflow/pass backend，因为 profile 给出具体 target 和 decode-garbage root 后，这些
mutation site 相对稳定。分支条件翻译属于 recovery backend：它在 branch target 已知后改写
稳定的 MLIL shape，profile author 不应为每个 binary 定制该改写。

Global constant recovery 可以更直接地使用 profile helper。通用 MLIL walk、const-address
extraction、内存读取、STORE 检查与 global-constant fact 构建可移入 helper，使 profile 能
表达 binary 的 slot rule，而不用重写底层检查代码。但暂时不把高层自动 global-constant planner
移入 helper；profile 和 pass 仍决定哪些 expression 是 slot use、哪些 offset/section rule
适用，以及哪些 slot 应成为 const fact。

具体 target 是 recovery-fact 信息；decode-garbage root 不是。profile 只给出当前 IL 的 target
与描述性见证，workflow/pass backend 在 mutation boundary 从当前 SSA 或同一 callback 的翻译计划
推导 cleanup root。Binary Ninja 区分 instruction index 与 expression index：前者只在当前 IL
generation 标识顶层 instruction，后者用于 `replace_expr`。两者都不得跨重新分析放入 profile
fact 或 session state。

首批 helper pass 不引入 backward-slice class 或 dataclass。在多个 profile 证明专用 slice
object 能减少真实复杂度之前，返回简单的 tuple、dict、set 和 Binary Ninja IL object。

Memory helper 应优先采用显式宽度与字节序。提供 `read_u8`、`read_u16le`、`read_u32le`
和 `read_u64le` 等 helper；任何 pointer helper 都必须接受显式宽度或 architecture/endian
参数，不能隐藏样本特定的指针模型。

Recovery-fact builder 是推荐 helper，而不是新的 contract layer。它们可以减少常见 branch、
call、global constant 和 string decrypt fact 的 dict-field 错误，但 profile hook 在特殊场景
更清晰时仍可返回普通 dict fact。

暂不引入通用 resolver engine、pattern DSL 或高层 `resolve_all_*` framework。只有多个
binary profile 证明相同的高层 resolver shape 后，才考虑这些抽象。
