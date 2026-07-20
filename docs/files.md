# 源码地图

| 路径 | 职责 |
| --- | --- |
| `plugins/DispatchThis/__init__.py` | 注册固定 workflow、公开 provider API。 |
| `plugins/DispatchThis/workflow.py` | callback 编排、当前 IL 安装、核心 mutation 和阶段门控。 |
| `plugins/DispatchThis/state.py` | 函数级 receipt、稳定性和失效。 |
| `plugins/DispatchThis/semantics.py` | `SampleSemantics`、Query、事实和计划的公开强类型契约。 |
| `plugins/DispatchThis/providers.py` | provider registry 与 BinaryView 选择绑定。 |
| `plugins/DispatchThis/settings.py` | 七个 pass 开关及依赖闭包。 |
| `plugins/DispatchThis/helpers/` | 可复用、纯的 LLIL/MLIL/memory/value API。 |
| `plugins/DispatchThis/passes/low/deinbr.py` | 核心 LLIL branch 后端。 |
| `plugins/DispatchThis/passes/medium/` | call、condition、cleanup、STORE、string 与 deflatten 后端。 |
| `sample/valorant/` | 独立的 Valorant provider；新样本的结构参考。 |
| `tests/test_valorant_sample.py` | provider 模式匹配的最小回归覆盖。 |

`profiles/` 是旧 bundled profile 的兼容层。不要为新样本扩展它；写一个外部 provider，并按 [sample-providers.md](sample-providers.md) 验证。

若改动 workflow 注册、callback、session state 或 IL 后端，先读 [pipeline.md](pipeline.md) 和相关 ADR。若只改样本识别，优先限制在 `sample/<name>/` 与其测试中。
