# 源码布局

> [!NOTE]
> 本页描述当前尚未迁移的源码树，因此仍列出 `profiles/`、旧 fact builder 和旧 activity 名称。
> 目标边界以 `CONTEXT.md` 和 ADR-0014 至 ADR-0039 为准：核心保留 workflow、状态、验证与
> 修改后端；具体样本代码迁入各自的外部插件。实现新架构时应同步重写本页，而不是把这里的
> legacy 布局当作新增样本的模板。

```
DispatchThis/
├── __init__.py                 插件入口：注册工作流和 activities、profile 设置及
│                               Function Analysis 设置 activity。
├── workflow.py                 工作流 activity 回调（LLIL 跳转解析、MLIL 调用/全局解析、
│                               分支翻译、关联存储、字符串解密、去平坦化、阶段清理和
│                               Function 作用域分析门控）。
├── workflow_state.py           Function 作用域的工作流阶段回执和稳定性。
├── ui.py                       选择 profile 及切换工作流设置的函数右键菜单命令和快捷键。
├── profiles/
│   ├── __init__.py             内置解析 profile 注册表和契约校验。
│   ├── default.py              兼容已有设置的 dyzznb profile 别名。
│   ├── driver_2_6.py           driver 2.6 样本的内置 profile。
│   ├── dyzznb.py               dyzznb 样本的内置 profile。
│   └── valorant_2_6.py         Valorant 2.6 样本的内置 profile。
├── helpers/
│   ├── __init__.py             稳定的 profile-helper 导入面。
│   ├── llil.py                 LLIL 间接跳转、定义和常量 helper。
│   ├── mlil.py                 MLIL 调用目标、槽位、存储、去平坦化规划器和清理根 helper。
│   ├── memory.py               BinaryView 内存、section 和目标校验 helper。
│   └── facts.py                供解析 profile 与 pass 使用的恢复事实构造器。
├── utils/
│   └── log.py                  共享的 "DispatchThis" logger。
├── passes/
│   ├── low/
│   │   └── gadget_llil.py      LLIL 解码 gadget 解析器：jump(reg) -> jump(const)，
│   │                           包含不透明谓词偏移选择。
│   └── medium/
│       ├── indirect_calls.py   MLIL 间接调用解码折叠和当前 IL 改写。
│       ├── branch_conditions.py 已解析 switch 到 if 的重建。
│       ├── correlated_stores.py 原子化按路径关联的存储重建。
│       ├── string_decrypt.py   MLIL 直接调用字符串解密识别器/注释器。
│       ├── phase_cleanup.py    在当前 MLIL 上收敛的分支/调用目标解码清理。
│       ├── rewrite.py          控制流改写的原子 MLIL copy-transform 后端。
│       └── deflatten.py        计算调度器计划并原子改写出口/状态写入。
├── docs/                       本文档。
│   ├── API.md                  解析 profile 的 helper API 参考。
│   ├── conditional-deflattening.md
│   ├── files.md                本源码地图。
│   ├── known-issues.md
│   ├── obfuscation.md
│   ├── pipeline.md
│   ├── resolver-profiles.md    如何添加内置二进制解析 profile。
│   ├── adr/                    架构决策记录。
│   └── agents/                 Agent 工作流说明。
├── README.md
└── LICENSE
```

## 模块职责

### `__init__.py`

克隆 `core.function.metaAnalysis`，注册 activities 及其插入点，暴露
`analysis.plugins.dispatchThis.indirectJumpsCalls`、
`analysis.plugins.dispatchThis.stringDecrypt` 和
`analysis.plugins.dispatchThis.deflatten` Function Analysis 设置。导入时不会改动
Binary Ninja 分析设置。

### `workflow.py`

由工作流按函数调用的 activity 回调。每个回调从 `AnalysisContext` 读取相关 IL，调用
pass 模块，并负责会触发重新分析的 Binary Ninja 修改以及用于门控的阶段/session 回执。
最早的解析回调还会在恢复前检查 DispatchThis 所需的 Function 作用域分析环境。

### `workflow_state.py`

管理 `Function.session_data["dispatchthis_workflow_state"]`：间接分支、间接调用和
全局常量工作流阶段的稳定性、修改回执和下游失效。见
[`adr/0003-function-phase-state-for-workflow.md`](adr/0003-function-phase-state-for-workflow.md)。

### `profiles/`

管理内置解析 profile 注册表。元数据必需；每个语义能力 hook 可选，缺失 hook 会产生
空结果。内置 `default` profile 是 dyzznb 的兼容别名，保留已有 BinaryView 设置和 profile 来源；
当前样本规则归属具名的 `dyzznb` profile。添加二进制 profile 前请阅读
[`resolver-profiles.md`](resolver-profiles.md)。

### `helpers/`

为可复用的 BNIL 和 BinaryView 检查提供稳定 profile-helper 模块：`llil`、`mlil`、
`memory` 和 `facts`。helper 减少 profile 重复代码，但二进制特定识别仍归 profile；
恢复后端负责 Binary Ninja 修改、阶段回执、IL 改写和清理应用。

### `passes/low/gadget_llil.py`

解析解码 gadget 的 `jump(reg)` 和 tail-call 形态，恢复表槽位、表基址 key、解码 key 及
entry offset，然后返回分支计划。它可改写当前 LLIL，但用户分支元数据和分析完成回调
调度由工作流负责。

### `passes/medium/indirect_calls.py`

构造调用目标计划，折叠 call-gadget 解码表达式，把当前 MLIL 调用目标改写为常量指针，
并返回清理根。调用类型调整、回执和调用目标阶段清理由工作流负责。

### `passes/medium/correlated_stores.py`

在一个 MLIL copy-transform 中应用 profile 已证明的按路径关联全局存储计划：在各自前驱
goto 前插入具体存储，并 NOP 合并后的存储。工作流只在全局常量恢复稳定后运行它。

### `passes/medium/string_decrypt.py`

扫描当前函数的 MLIL 直接调用，识别已去平坦化的样本家族字符串解密函数，解码源 blob，
并写入函数级调用点注释，同时保留手工注释行。

### `passes/medium/phase_cleanup.py`

执行分支目标和调用目标阶段清理。它只 NOP 以所属工作流阶段已解析站点为根的、死亡且纯
的目标解码赋值，并在当前 MLIL 上收敛到空计划；不会折叠控制流或删除去平坦化状态写入。

### `passes/medium/deflatten.py`

`compute_redirections` 识别主导调度器比较簇，通过具体 CFG 重放将状态令牌映射到目标块，
并返回终结器重定向及精确 `obsolete_state_writes` 指令索引。`rewrite_redirections_mlil`
将每个已选出口或明确的条件臂出口/共享出口/条件改写，以及精确状态写入 NOP，合并为
一次原子替换 MLIL 函数。它通过失败即关闭的具体重放支持相等、不等和有符号/无符号
有序调度器，而非符号区间求解。处理无条件和简单条件转移；见
[`conditional-deflattening.md`](conditional-deflattening.md)。
