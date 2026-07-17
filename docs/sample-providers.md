# 样本 Provider 指南

本页是新样本适配的默认流程。目标是把样本知识限制在独立插件中，并让核心保持可复用。

## 1. 创建最小 provider

在 `sample/<sample-name>/__init__.py` 中注册一次 provider。开发时把该目录软链接到 Binary Ninja 插件目录；不要把样本代码塞进 `plugins/DispatchThis/profiles/`。

```python
from DispatchThis import (
    CORE_API_VERSION,
    CompleteBatch,
    SampleSemantics,
    register_provider,
)


def branch_targets(query):
    return CompleteBatch(())


provider = SampleSemantics(
    provider_id="vendor-build-hash",
    name="Vendor build",
    api_version=CORE_API_VERSION,
    branch_targets=branch_targets,
)

register_provider(provider)
```

ID 必须稳定且唯一。provider 只读取 Query；所有 Binary Ninja 修改、receipt 和重分析仍属于核心。

## 2. 先分析，后写规则

对一个目标函数按层检查：

1. **LLIL**：确认间接 `jump(reg)`、目标数据流和真实 CFG 入口。
2. **MLIL/SSA**：确认变量、全局 load、call 参数和循环结构。
3. **HLIL**：只用来确认最终语义是否更可读；它不是恢复事实来源。
4. **日志**：确认 provider 何时返回 `Inconclusive`、实际提交了什么事实。

PowerShell 中可用：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
bn target list
bn il --target active --view llil <function>
bn il --target active --view mlil <function>
bn log --target active --limit 100
```

在 GUI 中已经打开样本时，`bn py exec --target active` 适合调用纯 provider 函数并打印结果。它不是 workflow 已绑定的证明。

## 3. 使用受限模式，而非地址表

匹配以 IL 形状、数据流和当前 CFG 为边界。各类 jump、global 与三种字符串模式的证据清单见 [obfuscation.md](obfuscation.md)。回放器只接受该模式所需的 operation、字节宽度、控制流和最大步数；未知副作用、指针不确定性、无终止符或非文本输出一律不产出事实。

## 4. 三个常见坑

### 嵌套全局 load

MLIL 中的静态 load 常嵌在算术或字段表达式下。遍历完整表达式树，并排除当前函数直接写入的重叠槽位，之后才产生 `GlobalDataFact`。provider 应返回完整的原生 `Type`，而不是类型字符串。
该事实只恢复槽位类型；不能仅因文件初始字节可读就把运行时 LOAD 折叠成立即数。

### SSA 字段读取

`MLIL_VAR_SSA_FIELD` 不是完整变量读。若样本确实需要它，先确认通用值求解器能从当前 SSA 定义按字段 offset/width 取值；这是适合修进核心的通用能力。给它加一个小的回归测试后再让样本使用。

### 受控 load 与 PHI

若 Binary Ninja 的 VSA 已证明一个 controlled load 为单一常量，或纯 `ValuePolicy.resolve_load` 给出完整值，核心值求解器可以使用它；否则失败即关闭。PHI operand 只能在唯一匹配 incoming CFG edge，或沿无重定义路径可唯一转发到该 edge 时关联；无法唯一匹配就返回 `Inconclusive`，不能按 operand 位置猜测。

### 剩余 switch

只有 provider 同时证明恢复点条件和两个不同、有方向的目标时，才填写 `BranchTargetFact.condition`、`true_target`、`false_target`。多入口 dispatcher 或只有无方向目标集的站点必须返回 `condition=None`。保留 switch 是正确结果，不是条件翻译器的失败。

多个 arm 见证只能在每个 parent 都完整覆盖同一 true/false 目标、并且恢复出的条件是同一直接 LLIL 寄存器/常量比较时合并；不能按 CFG 或列表顺序挑一个候选。

## 5. 何时修改核心

默认不改。只有下列条件同时满足才改：

- 缺口对多个样本有意义，而不是当前样本的公式或模式；
- 能写成纯、当前 IL 驱动的通用功能；
- 有最小回归测试；
- 不需要增加 activity、改变顺序或把 Binary Ninja 修改移出 workflow。

例如，SSA 字段、受控 load 和带 CFG 见证的 PHI 值关联是通用值求解能力；某个字符串 decoder 的 key 轮换不是。

## 6. 验证闭环

1. 用最小 fake IL 测试模式的接受和拒绝条件。
2. 在真实函数上直接调用 provider，检查事实数量、来源、目的地和明文。
3. 完整重启 Binary Ninja，选择 provider，启用相应 pass。
4. 用 `bn log` 确认 workflow 实际应用事实；再读取 GUI 注释、data-var 类型或最终 HLIL。
5. 执行 `pytest -q` 与 `ruff check .`。

修改 workflow 注册或回调后必须完整重启：已注册 workflow 不可变。仅改 provider 或其纯辅助函数时可以在开发中热重载，但必须读回实际 registry/bound callback 并用 `bn log` 验证 GUI workflow；无法证明绑定已更新时仍应完整重启。不要保存或覆盖用户的 BNDB，除非用户明确要求。

## 7. 完成标准

一个样本适配完成时：

- 没有样本地址、明文或 key 表硬编码在识别规则中；
- 每个事实可从当前 IL 重现；
- 不可证明的站点保持原样并留有可读日志；
- 在等价的当前 IL、且上游 gate 已稳定时，GUI workflow 与直接 provider 调用给出一致结果；
- 代码和最小测试在独立 sample 插件中，通用缺口才进入核心。
