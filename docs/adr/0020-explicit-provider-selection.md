# 显式选择样本语义提供者

多个外部样本插件可以同时向 DispatchThis 核心注册提供者。核心必须从自己拥有的设置中读取
稳定 provider ID 并显式绑定对应 `SampleSemantics`；不自动识别样本，也不因当前只有一个
提供者而隐式选择。核心以接收当前 `BinaryView` 的插件菜单提供选择入口，并把选中的 ID 以
该视图为 resource 写入 `SettingsResourceScope`，使绑定随 Raw BinaryView/BNDB 持久化。
workflow callback 使用 `AnalysisContext.view` 读取绑定并在运行时查询 registry。provider 身份
不进入 Function Settings、session data 或样本插件私有状态。核心只注册一个
`DispatchThis\\Select Provider…` 命令，调用时动态枚举 registry；不为每个 provider 注册独立
菜单项，因此外部样本插件的加载顺序不要求重建菜单。
