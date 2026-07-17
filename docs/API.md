# Provider API

公开 API 由 `DispatchThis` 包导出，当前版本为 `CORE_API_VERSION = 4`。provider 必须在 `SampleSemantics.api_version` 中写入该精确值；不匹配会被拒绝。

## 注册

```python
from DispatchThis import CORE_API_VERSION, SampleSemantics, register_provider

register_provider(
    SampleSemantics(
        provider_id="vendor-build-hash",
        name="Vendor build",
        api_version=CORE_API_VERSION,
    )
)
```

一个 provider 只能有一个 `SampleSemantics`。provider ID 是 BinaryView 中持久化的选择值，因此不能依赖导入顺序、显示名称或“只有一个 provider”的隐式回退。

## 槽位

| `SampleSemantics` 字段 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `branch_targets` | `BranchTargetQuery(view, function, llil)` | `BranchTargetFact` | 完整 jump 目标集；只有有方向条件才携带 `condition`。 |
| `call_targets` | `CallTargetQuery(view, function, mlil)` | `CallTargetFact` | 完整 call callee 集。核心目前只改写单目标事实。 |
| `global_data` | `GlobalDataQuery(view, function, mlil)` | `GlobalDataFact` | 精确槽位与完整 Binary Ninja `Type`。 |
| `correlated_stores` | `CorrelatedStoreQuery(view, function, mlil)` | `CorrelatedStorePlan` | 当前 MLIL 的两臂 STORE 计划。 |
| `string_recovery` | `StringRecoveryQuery(view, function, mlil, deflattened_function_starts)` | `StringRecoveryFact` | 调用点、源、目的地和 `bytes` 明文；最后一项是可为空的快照，不是执行前置条件。 |
| `deflatten` | `DeflattenQuery(view, function, mlil)` | `DeflattenPlan` | 原子 dispatcher 重定向计划。 |

每个 slot 的返回类型都是 `CompleteBatch[Fact] | Inconclusive`：

```python
return CompleteBatch((fact_a, fact_b))
return CompleteBatch(())
return Inconclusive("required current-IL definition is unavailable")
```

字符串 provider 必须能在 `deflattened_function_starts == frozenset()` 时扫描当前 MLIL；它不能依赖其他 DispatchThis callback 已运行。

不要返回 list、dict 或插件私有结果类型。批次可省略未支持/未证明站点，但返回站点绝不能带部分目标或部分语义；扫描本身无法完成时才返回 `Inconclusive`，它会使核心保持该阶段开放。

## 事实要求

- 地址必须是非负 `int`；目标元组必须非空、排序且去重。
- `BranchTargetFact` 的条件形式必须携带两个不同的有方向目标；否则 `condition=None`。
- `CallTargetFact` 必须保留全部已证明 callee，不能挑一个“最优”值。
- `GlobalDataFact.data_type` 是原生 `Type`，不是字符串。
- 所有 IL 见证必须来自当前 Query 的 IL。

`DeflattenPlan`、`CorrelatedStorePlan` 和其 witness 类型较严格。先复用核心 helper 证明现有形态；不要构造缺少当前 IL/CFG 证据的计划。

## 可复用功能 API

样本 provider 可以导入：

```python
from DispatchThis.helpers import llil, memory, mlil, values
```

这些模块提供操作查询、表达式/SSA 遍历、内存边界检查和完整值求解。它们不包含样本规则，也不修改 Binary Ninja。`values.evaluate_values(...)` 要么返回完整值证据，要么返回 `Inconclusive`；可选 `ValuePolicy` 只处理样本特有运算或受控 load，且必须保持纯函数。

对已初始化静态数据的纯 load，可用 `memory.initialized_data_policy(view)` 创建不可变快照并直接作为 `ValuePolicy`；`memory.byte_order(view)` 只提供视图字节序，不能推断样本的 pointer 模型。需要按当前 MLIL 指令顺序、去重遍历表达式时，使用 `mlil.iter_expressions(mlil)`。

函数名和参数以对应模块的 docstring/`__all__` 为准；不要为文档复制一份会过期的长函数清单。最小真实用法见 [sample-providers.md](sample-providers.md) 和 `sample/valorant/__init__.py`。

## 禁止行为

- 修改 BinaryView、Function、IL、Settings、session data 或 workflow；
- 注册 activity 或依赖 callback 顺序；
- 把缓存的旧 IL、字符串化变量名或旧 instruction index 作为证明；
- 用地址表、固定 key 或明文表替代当前 IL 的数据流证明。

API 破坏性变更必须递增 `CORE_API_VERSION` 并同步更新 provider；兼容 shim 不改变旧 provider 的事实语义。
