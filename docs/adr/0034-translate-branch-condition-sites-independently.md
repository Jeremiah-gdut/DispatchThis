# 按站点独立翻译分支条件

核心对每个条件回执分别重新绑定当前跳转站点、恢复点条件和两个目标，并计算
`REWRITE_READY`、`ALREADY_SATISFIED` 或 `FAILED`。目标与回执一致的同源 switch-like
`MLIL_JUMP_TO` 才是 `REWRITE_READY`；精确使用重绑条件及有方向目标的同源 `MLIL_IF` 才是
`ALREADY_SATISFIED`；其他形态、目标不一致或映射缺失/歧义均为 `FAILED`，且不从 HLIL 或其他
CFG 形态回退。单个站点失败时保留该处原分支，在源地址维护自动标签，并只于失败首次出现或
原因变化时输出 warning；其他站点的有效计划仍由一次原子 MLIL copy-transform 共同安装。
只有实际安装成功或已由当前 MLIL 精确满足的站点才计入完备性，只有实际安装成功的站点才能
提供目标解码清理根。

站点失败以源地址、`ConditionFailureReason` 枚举和显示 detail 组成的冻结结果表达；detail 不
参与去重，会话只保存原因枚举的稳定值。若共同执行的 copy-transform 或 MLIL 安装失败，核心
只报告一次函数级 error，不给每个源地址重复打标签；三个站点状态只属于本轮 `ctx.mlil`，函数
会话不得保存 `translation_done` 或复用旧 overlay 状态。站点绑定、形态、映射或安装失败只
产生当前失败状态，不删除条件回执；成功翻译同样保留回执，直到其底层事实按生命周期规则
真正失效。
