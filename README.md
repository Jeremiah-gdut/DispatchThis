# DispatchThis

DispatchThis 是面向单个 ARM64 ELF 样本的 Binary Ninja 工作流核心。它恢复已证明的控制流和语义信息，不修改二进制字节。

核心负责阶段、重新分析和 IL 改写；每个样本由独立 provider 提供识别规则。新样本从 `sample/` 中的独立插件开始，而不是向核心增加 profile。

## 使用

1. 将 `plugins/DispatchThis` 和目标样本插件放入 Binary Ninja 插件目录。开发时可将样本目录软链接到该目录。
2. 完整重启 Binary Ninja，打开二进制并在函数菜单中选择 **DispatchThis ▸ Select Provider…**。
3. 从 **DispatchThis** 菜单启用所需阶段；有依赖的下游阶段会自动启用其前置阶段。
4. 用 LLIL/MLIL、日志和最终 HLIL 验证结果，不要只以 switch 或注释数量判断成功。

代码变更后应重启 GUI。`bn py exec` 适合验证纯 Python 逻辑，但不能证明已注册 workflow callback 已重新绑定。

## 阶段

| 阶段 | IL | 结果 |
| --- | --- | --- |
| Indirect Branch Targets | LLIL | 提交已证明的间接跳转目标，重建 CFG |
| Indirect Call Targets | MLIL | 改写已证明的单目标调用 |
| Global Data Semantics | MLIL | 标注已证明的全局数据类型 |
| Branch Condition Translation | MLIL | 将有方向证据的二目标跳转还原为 IF |
| Correlated STORE Recovery | MLIL | 恢复已证明的路径关联 STORE |
| String Recovery | MLIL | 写入已证明的字符串调用点注释 |
| Deflatten | MLIL | 原子恢复已证明的 dispatcher 后继 |

精确顺序、稳定性和修改所有权见 [workflow](docs/pipeline.md)。

## 开发文档

- [架构与术语](CONTEXT.md)：唯一的核心/provider 边界。
- [样本 provider 指南](docs/sample-providers.md)：从分析、模式匹配到 GUI 验证的标准流程。
- [公开 API](docs/API.md)：六个 provider 槽位及结果契约。
- [混淆诊断](docs/obfuscation.md)：如何识别跳转、全局、字符串和残留 switch。
- [条件去平坦化](docs/conditional-deflattening.md)：严格的 deflatten 接受条件。
- [源码地图](docs/files.md)：应修改哪个模块。
- [限制与调试](docs/known-issues.md)：失败时应保留什么、检查什么。
- [ADR 索引](docs/adr/README.md)：历史架构决定；它们是背景，不是重复的使用手册。

## 验证

```powershell
pytest -q
ruff check .
```

## 范围

DispatchThis 不自动识别样本，也不把“相似二进制”当作已支持。provider 只能提交完整证明；无法证明时保持原 IL，而不是猜测目标、条件或字符串。

## 兼容性

目标为 Binary Ninja 5.3+ 的 ARM64 ELF 分析。旧 bundled profiles 仅为兼容路径；新工作应使用外部样本 provider。

## 许可证

[MIT](LICENSE)
