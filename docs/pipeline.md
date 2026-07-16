# 工作流

DispatchThis 克隆 `core.function.metaAnalysis`，并注册固定 activity DAG。该顺序是核心契约；样本 provider 不能插入、移动或绕过它。

## 顺序

| 位置 | 设置 activity | 执行 activity | IL | 作用 |
| --- | --- | --- | --- | --- |
| `generateMediumLevelIL` 前 | `analysis.plugins.dispatchThis.branchTargets` | `extension.DispatchThis.IndirectPatcher` | LLIL | 提交已证明的间接分支目标，供 BN 重建 CFG。 |
| `generateHighLevelIL` 前 | `analysis.plugins.dispatchThis.callTargets` | `extension.DispatchThis.IndirectCallPatcher` | MLIL | 改写已证明的单目标 call。 |
| 同上 | `analysis.plugins.dispatchThis.globalData` | `extension.DispatchThis.GlobalConstantResolver` | MLIL | 应用 provider 的全局数据类型。 |
| 同上 | `analysis.plugins.dispatchThis.branchConditions` | `extension.DispatchThis.BranchConditionTranslator` | MLIL | 仅把有方向条件事实还原为 IF。 |
| 同上 | `analysis.plugins.dispatchThis.correlatedStores` | `extension.DispatchThis.CorrelatedStoreRecovery` | MLIL | 应用路径关联 STORE 计划。 |
| 同上 | `analysis.plugins.dispatchThis.stringRecovery` | `extension.DispatchThis.StringRecovery` | MLIL | 写字符串恢复注释。 |
| 同上 | `analysis.plugins.dispatchThis.deflatten` | `extension.DispatchThis.Deflatten` | MLIL | 原子重写已证明 dispatcher 边。 |

阶段依赖为：`branch → call → global → branchConditions → deflatten`；`correlatedStores` 和 `stringRecovery` 均依赖 `global`，但不彼此依赖。菜单启用下游时自动启用前置阶段。

## 每层的职责

- **LLIL**：最早、最接近 lift 后机器语义。间接分支恢复在这里完成。
- **MLIL**：变量、SSA、调用参数、数据流与类型语义。provider 的 call/global/correlated-store/string/deflatten 槽位在这里工作。
- **HLIL**：最终展示和人工语义检查。它不提供 branch/call 事实。

## 重新分析与当前 IL

`set_user_indirect_branches`、`set_call_type_adjustment` 和 `BinaryView.add_analysis_completion_event` 会触发或安排重新分析。只有 `workflow.py` 的 callback 可调用它们。

每轮改写只对 `AnalysisContext` 的当前 IL 有效。`replace_expr` 后需 `finalize()`；如果下一步依赖数据流，需重新生成 SSA。新 MLIL copy-transform 使用 `AnalysisContext.set_mlil_function(...)` 安装。重分析后必须重新取得事实、计划和清理根，不得复用旧 IL 对象或索引。

branch/call/global 等函数级 receipt 只放在 `Function.session_data["dispatchthis_workflow_state"]`。`BinaryView.session_data` 只保存视图级时序与跨函数 gate，例如 tag-cleanup 回调和 `dispatchthis_mlil_stable`；不得把 branch/call receipt 放入其中。

## 收敛与 cleanup

provider 的 `CompleteBatch` 只表示本轮扫描完成；它不等于后端已收敛。核心读取当前事实、验证见证、应用修改并读回状态，随后决定 receipt 是否稳定。

cleanup 只会 NOP 从所属 branch/call 事实出发、已证明死亡且纯的目标解码赋值。它不会清理控制流、call、STORE 或 deflatten 状态写。任何重复计划、应用失败或不完整 current-IL 证明都使 receipt 保持开放。

分支条件翻译在当前 MLIL 上独立处理每个已有条件 receipt。成功站点可以安装；没有确切条件、目标不一致或重新绑定失败的站点保留原形。多入口 switch 不是可接受的 IF 回退。

## Provider 与核心的边界

provider 只返回 `CompleteBatch` 或 `Inconclusive`。核心负责：

- 按 provider ID 绑定当前 BinaryView；
- 验证带 IL witness 的事实/计划与当前 IL，并验证 global/string 的地址与语义边界；
- 所有 Binary Ninja mutation、receipt、失效传播与 cleanup；
- call type adjustment、全局 data-var 类型、注释和原子 MLIL 安装。

详细的 provider 开发流程见 [sample-providers.md](sample-providers.md)。
