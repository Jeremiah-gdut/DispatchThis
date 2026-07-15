# 用 SampleSemantics 统一注册样本语义

外部样本插件只通过 `register_provider(SampleSemantics(...))` 接入 DispatchThis 核心。
`SampleSemantics` 集中携带稳定的 provider 身份、写死的整数核心 API 版本和六个具名可选
callable；缺少 callable 表示不支持对应槽位。核心在注册边界拒绝 API 版本不精确匹配的整个
提供者。它不提供通用 tagged-request `provide(...)` 分派器，也不允许逐槽位注册 callback，
以保持槽位类型清晰、接口局部且注册原子。
