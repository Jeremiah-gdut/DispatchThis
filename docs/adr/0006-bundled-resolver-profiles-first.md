# 先使用 bundled resolver profile

DispatchThis 从一个小型内置 `profiles` 包加载 resolver profile，而不是从外部插件或
hot-reload 系统加载。新的 binary 支持应增加具名 bundled profile 并显式注册；只有当 profile
变动证明编辑插件包才是真正瓶颈时，才考虑外部 profile discovery。
