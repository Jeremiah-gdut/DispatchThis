# 要求完整证据和当前 IL witness

DispatchThis 只从完整证据发布 recovery fact。一个完整值求解器要么返回每条语义路径支持的
全部 value，要么显式返回 `Inconclusive`。有界展开、不可读取的 load、不支持的
operation、歧义 PHI relation 或恢复 target set 中的无效成员，都不能授权保留其余成员。

PHI correlation 使用强类型结果显式区分“不适用”“完整关联”和“无法证明”。无法证明时返回
`Inconclusive` 并禁止 fallback；空集合只表示已完整求解且确实没有值，绝不再承担 rejection
sentinel。关联失败时不得保留未关联子集或回退笛卡尔积。

需要一个 value 的 consumer 必须先接收完整 set，再检查其 cardinality 是否为一。Helper 和
provider 不得提供用于 branch 或 call target 的“first”“best”或有效子集 convenience。当多个
witness 描述同一 site 时，所有 witness 必须在相同语义上一致，才能发布 fact。

后续会改写 IL 的 recovery plan 必须保留其 Binary Ninja instruction witness。在 mutation
boundary，backend 将每个 witness 映射到当前 `AnalysisContext` IL，并验证其 instruction
index、expression index、operation、address、相关 operand 和所属 IL function。过期或
malformed witness 必须以原子方式拒绝计划；绝不可通过扫描函数中相似 instruction 恢复它。

单 IL module 中，Binary Ninja 原生 operation enum 是实现词汇。仅在刻意保留的 mixed-LLIL/MLIL
compatibility seam 输出 operation-name tuple，因为相同的 `IntEnum` value 否则可能混淆
IL level。这些名称由 Binary Ninja enum 生成，不能手写。

Workflow receipt 是协调状态，不是分析真相。branch metadata、call type adjustment、global
data-variable type 与当前 IL witness 都必须从 Binary Ninja 读回，receipt 才算满足。函数
phase state 也记录 provider ID；BinaryView provider 绑定改变时，带有 recovery evidence 的
函数状态全部失效，不能跨 provider 复用。

此决策宁可错过优化，也不接受错误的 CFG edge、call prototype、state transition 或 cleanup
NOP。要支持新的混淆 shape，应为它增加完整证明，而不是削弱这些 mutation boundary。
