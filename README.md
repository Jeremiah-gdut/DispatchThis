# DispatchThis

DispatchThis 是 Binary Ninja 5.3+ 的 ARM64 ELF 恢复核心。它需要一个已注册的样本 provider；provider 只提交当前 IL 能完整证明的事实，核心负责 workflow、重新分析和 IL 改写。

## 安装

Windows, 把插件软连接到BN的插件目录

```powershell
git clone https://github.com/Jeremiah-gdut/DispatchThis.git
cd DispatchThis

$bnPlugins = Join-Path $env:APPDATA 'Binary Ninja\plugins'
$repo = (Get-Location).Path
New-Item -ItemType Directory -Force -Path $bnPlugins | Out-Null
New-Item -ItemType SymbolicLink -Path "$bnPlugins\DispatchThis" -Target "$repo\plugins\DispatchThis"
New-Item -ItemType SymbolicLink -Path "$bnPlugins\valorant" -Target "$repo\sample\valorant"
```

安装后的目录应为：

```text
%APPDATA%\Binary Ninja\plugins\
├── DispatchThis\    # <repo>\plugins\DispatchThis
└── valorant\        # <repo>\sample\valorant；替换为你的 provider 即可
```

无法创建软链接时，直接复制这两个目录到同一位置。macOS/Linux 也是同一规则：把 `plugins/DispatchThis` 和目标 provider 目录放入 Binary Ninja 的用户插件目录。

## 使用

1. 完整重启 Binary Ninja；安装、provider 或 workflow callback 有改动时也要重启。
2. 打开 ARM64 ELF，在目标函数中选择 **DispatchThis ▸ Select Provider…**，再选择 provider。
3. 在同一菜单选择 **DispatchThis ▸ Toggle <pass>**。provider 绑定到当前 BinaryView；pass 开关只作用于当前函数。
4. 查看日志、LLIL、MLIL 和最终 HLIL。无法完整证明时保留原 IL 是正确结果，不要只看 switch 是否消失。

启用有依赖的 pass 会自动启用它的前置 pass。要关闭当前函数所有 pass，选择 **DispatchThis ▸ Disable All**。

## 当前 pass

| Pass | IL | 做什么 |
| --- | --- | --- |
| Indirect Branch Targets | LLIL | 提交已证明的间接跳转目标，重建 CFG。 |
| Indirect Call Targets | MLIL | 将已证明的单目标间接调用改为 direct call。 |
| Global Data Semantics | MLIL | 应用 provider 已证明的全局数据类型。 |
| Branch Condition Translation | MLIL | 将有方向证据的二目标跳转翻译为 IF。 |
| Correlated STORE Recovery | MLIL | 恢复已证明的路径关联 STORE。 |
| String Recovery | MLIL | 写入已证明的字符串调用点注释。 |
| Deflatten | MLIL | 原子恢复已证明的 dispatcher 后继。 |

完整顺序和 cleanup 规则见 [工作流](docs/pipeline.md)。

## 开发 provider

1. 创建 `sample/<sample-name>/__init__.py`，或从 `sample/valorant/` 复制一个最小目录结构。
2. 用稳定的 `provider_id`、精确的 `CORE_API_VERSION` 和实际支持的槽位注册一次 provider：

```python
from DispatchThis import CORE_API_VERSION, CompleteBatch, SampleSemantics, register_provider


def branch_targets(query):
    return CompleteBatch(())


register_provider(
    SampleSemantics(
        provider_id="vendor-build-hash",
        name="Vendor build",
        api_version=CORE_API_VERSION,
        branch_targets=branch_targets,
    )
)
```

3. 只实现需要的槽位：`branch_targets`、`call_targets`、`global_data`、`correlated_stores`、`string_recovery`、`deflatten`。
4. provider 只读 Query；不得注册 activity、修改 Binary Ninja、读写 workflow state，或保存旧 IL/索引。
5. 完整扫描后返回 `CompleteBatch(facts)`；扫描本身无法完成时返回 `Inconclusive(reason)`。每个事实必须来自当前 Query，且 target 集合必须完整。

从 [样本 provider 指南](docs/sample-providers.md) 开始；所有 Query、事实和计划类型见 [Provider API](docs/API.md)。

## 开发 core 插件

1. 先判断问题是否可复用：样本特有的识别和解码留在 provider，只有可复用且可测试的 API 缺口才进入 core。
2. 改动前阅读 [架构契约](CONTEXT.md)、[工作流](docs/pipeline.md) 和相关 ADR。
3. 按职责定位代码：

| 要改什么 | 从哪里开始 |
| --- | --- |
| workflow 编排、重新分析和当前 IL 安装 | `plugins/DispatchThis/workflow.py` |
| provider API、Query、事实或计划 | `plugins/DispatchThis/semantics.py`、`providers.py` |
| 恢复、翻译或 cleanup 后端 | `plugins/DispatchThis/passes/low/`、`passes/medium/` |
| 菜单和函数级 pass 开关 | `plugins/DispatchThis/ui.py`、`settings.py` |

4. pass 可以分析、构建计划或改写当前 IL；只有 `workflow.py` callback 可以调用会触发重新分析的 API，例如 `set_user_indirect_branches` 和 `set_call_type_adjustment`。
5. 只使用 `AnalysisContext` 的当前 IL；函数级 receipt 放在 `Function.session_data["dispatchthis_workflow_state"]`。改写 IL 后按需要 `finalize()`、重建 SSA，并通过 `AnalysisContext.set_mlil_function(...)` 安装替换。

## 验证

```powershell
pytest -q
ruff check .
```

随后完整重启 Binary Ninja，在真实函数上选择 provider、启用 pass，并读回日志、IL 和最终 HLIL。`bn py exec` 适合验证纯 Python 逻辑，但不能证明 GUI workflow 已重新绑定。
