# 在显式重分析前准备分析配置

DispatchThis 不在导入插件时修改 Binary Ninja 的分析上限和内置 outlining 设置。用户显式
切换 pass 或 provider 并请求重分析时，UI 先委托核心准备当前 `Function` 的
`SettingsResourceScope` 环境：无限制函数大小、expression-value depth 99999、分析时间
3600000 ms（60 分钟）、update count 1024、禁用 invalid instruction 触发的 guided analysis，
以及禁用 builtin outlining。随后才调用已有的显式 `func.reanalyze()` 路径；workflow callback
在当前 activity 中重复验证函数设置，并只在仍有 guided source block 时清除它。

`maxFunctionSize`、`maxFunctionAnalysisTime` 和 `maxFunctionUpdateCount` 的实际 core budget
来自 `BinaryView.parameters_for_analysis`，不是 Function resource setting。因此同一个 UI
preflight 还必须在重分析前写入并读回当前 BinaryView 的这三个 live 参数。这个 budget 是
view 级运行时资源，可能影响同一 view 中同时进行的分析；它不再被错误地描述为仅影响一个
Function。若任何函数设置或 live 参数无法验证，UI 不安排该次重分析，workflow 也不执行
recovery。

函数 override 在 DispatchThis 禁用后仍保留；插件不维护旧值状态，也不实现不可靠的部分
reset 路径。这样既避免导入时污染无关 BinaryView，又确保用户实际触发的重分析从一开始就
拥有与恢复工作流一致的预算和 guided-analysis 语义。

此 ownership model 的兼容性基线是 Binary Ninja 5.3 或更新版本。
