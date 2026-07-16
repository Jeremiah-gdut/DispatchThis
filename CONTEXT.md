# DispatchThis 架构契约

DispatchThis 的成功标准是：对当前样本给出可复核的恢复结果。宁可保留混淆 IL，也不提交未证明的猜测。

## 术语

| 术语 | 含义 |
| --- | --- |
| 核心 | `plugins/DispatchThis`；拥有 workflow、状态、验证与 Binary Ninja 修改。 |
| 样本 provider | 一个独立插件；只实现一个具体二进制的识别和证明。 |
| 槽位 | 固定的恢复位置：branch、call、global、STORE、string、deflatten。 |
| 事实/计划 | provider 返回的强类型证据；需要 IL 改写的事实/计划携带当前 IL 见证，global/string 使用地址与语义证据。 |
| 当前 IL | 正在执行的 activity 所见的 IL。重分析后旧 IL 对象、索引和 SSA 结论均不可复用。 |

不要把 provider 称作“通用反混淆器”或“样本家族”。一个 provider 只对其实际证明过的样本负责。

## 所有权

| 核心拥有 | provider 拥有 |
| --- | --- |
| activity 注册、顺序和依赖 | 样本特有的识别规则、解码算法和模式匹配 |
| `set_user_indirect_branches`、`set_call_type_adjustment`、data-var 类型与重分析时序 | 对当前 Query 的只读分析 |
| session receipt、失效、清理和原子 MLIL 替换 | `SampleSemantics` 的六个可选槽位 |
| 对含 IL witness 的事实/计划做当前 IL 验证 | 完整事实或明确的 `Inconclusive` |

provider 不得注册 workflow activity、读写 workflow state、调用会触发重新分析的 API，或通过地址表硬编码样本结果。

## Provider 契约

provider 以一次 `register_provider(SampleSemantics(...))` 注册，并且 `api_version` 必须精确等于 `CORE_API_VERSION`。每个槽位只接受其专属只读 Query，并只返回：

- `CompleteBatch(facts)`：已完成当前扫描；空元组表示没有匹配。批次可省略当前 provider 不支持或尚未证明的站点，但每个返回事实必须完整。
- `Inconclusive(reason)`：当前连扫描本身都无法完成；核心不接受该轮结果，也不关闭该阶段。

六个槽位是固定的：`branch_targets`、`call_targets`、`global_data`、`correlated_stores`、`string_recovery`、`deflatten`。provider 可以省略槽位，但不能新增、插入或重排阶段。

branch/call 事实必须保留完整目标集合；IL 改写计划必须保留精确当前 IL 见证；global/string 事实必须从当前 Query 导出其地址与语义证据。不得挑选“第一个”目标、用字符串显示名识别变量，或以旧 instruction index 重新发现清理点。

## 固定工作流

1. 间接分支在 **LLIL**、`generateMediumLevelIL` 之前运行。
2. 字符串恢复在核心 `findStringReferences` 之后的 **MLIL** 上独立运行；它只需要自己的开关和当前 MLIL，不等待 branch/call/global/deflatten，也不改变 cleanup receipt。`deflattened_function_starts` 只是可为空的当前快照。
3. 间接调用、全局数据、分支条件、关联 STORE 和 deflatten 在 **MLIL** 中、`generateHighLevelIL` 之前运行。分支和调用稳定后才允许其下游消费它们；全局数据在分支条件之前；deflatten 需要当前分支条件与 cleanup 证明。

顺序和插入点是核心 ABI。样本适配不得修改它们。完整图见 [docs/pipeline.md](docs/pipeline.md)。

## 证明规则

- LLIL 用于最早恢复间接分支并重建 CFG；MLIL 用于变量、SSA、调用与语义；HLIL 只用于展示验证。
- `replace_expr` 后需要 `finalize()`；依赖数据流的后续工作需要新的 SSA。安装新的 MLIL 使用当前 `AnalysisContext`。
- 由 pass/provider 事实驱动、会触发重新分析的分析修改只可在 `workflow.py` 的 callback 边界提交，并以 `Function.session_data["dispatchthis_workflow_state"]` 记录函数级 receipt；UI 仅在用户显式切换 pass 或 provider 后安排重分析。
- 清理只处理所属阶段、精确证明为死且纯的根。它不折叠控制流、不删除 call/STORE，也不扫描整函数寻找“相同 token”。
- 多入口或无有方向条件的 `MLIL_JUMP_TO` 可以保留为 switch；展示不够漂亮不是安全改写的理由。

## 新样本的默认路径

先识别混淆形态，再在样本 provider 中写受限模式匹配和小型回放。只有当样本遇到一个可复用、语义正确且测试能覆盖的核心 API 缺口时，才修改核心。详细流程见 [docs/sample-providers.md](docs/sample-providers.md)。

## 历史决定

ADR 记录保留为设计背景。开始涉及 workflow、provider、当前 IL、PHI、cleanup 或 deflatten 的改动前，阅读相关 ADR；日常使用以本文和链接的权威页面为准。
