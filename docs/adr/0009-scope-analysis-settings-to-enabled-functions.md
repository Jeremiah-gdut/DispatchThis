# 将分析设置限定到已启用的函数

DispatchThis 不再在导入插件时修改 Binary Ninja 的分析上限和内置 outlining 设置。最早的
resolver workflow callback 会在 workflow eligibility 已确认 DispatchThis 启用后，通过
`SettingsResourceScope` 将所需值应用于当前 `Function`。插件固定值优先于该函数继承或
用户设置，因为它们属于预期分析环境：无限制函数大小、expression-value depth 99999、分析时间
1800000 ms（30 分钟）、update count 1024，以及禁用 builtin outlining。callback 只写入不匹配
的值，经 Function resource 验证全部五项；若无法建立所需环境，本轮跳过 recovery。函数
override 在 DispatchThis 禁用后仍保留；插件不维护旧值状态，也不实现不可靠的部分 reset
路径。这样可分析大型混淆函数，又不会改变无关 Function 或 BinaryView 的分析行为。

此 ownership model 的兼容性基线是 Binary Ninja 5.3 或更新版本。
