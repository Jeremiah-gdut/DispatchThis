# Legacy resolver profiles

`plugins/DispatchThis/profiles/` 仍为历史 bundled profiles 提供兼容适配，但不是新开发入口。

## 新样本：两步开始

1. 创建独立 `SampleSemantics` provider，见 [sample-providers.md](sample-providers.md)。
2. 将样本规则、测试和模式匹配留在该插件中。

核心只保留对 legacy profile 的私有迁移 adapter；不要向该目录增加样本规则、workflow hook 或新的 profile framework。
