# Legacy resolver profiles

`plugins/DispatchThis/profiles/` 仍为历史 bundled profiles 提供兼容适配，但不是新开发入口。

新样本请创建独立 `SampleSemantics` provider，见 [sample-providers.md](sample-providers.md)。核心只保留对 legacy profile 的私有迁移 adapter；不要向该目录增加样本规则、workflow hook 或新的 profile framework。
