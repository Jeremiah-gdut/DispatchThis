# 限制与调试

## 先收集五项证据

```powershell
$env:PYTHONIOENCODING = 'utf-8'
bn target list
bn workflow state --target active --function <function>
bn il --target active --view llil <function>
bn il --target active --view mlil <function>
bn log --target active --limit 100
```

报告应包含：二进制/函数地址、当前 provider ID、启用的 pass、LLIL/MLIL 片段、日志、期望与实际 GUI 效果。先确认属于样本模式、通用 API 缺口还是安全拒绝，再改代码。

## 正确但不完整的输出

- 只有整轮扫描本身不可信时才返回 `Inconclusive`；完整扫描可省略未支持/未证明站点，完全没有匹配时返回 `CompleteBatch(())`，不能返回裸空结果。核心不会猜测。
- 多目标 call、无方向 branch、残留 switch、未清理的纯解码和仍可观察的状态写都可能是正确的保守结果。
- MLIL 是 overlay；真正的重新分析可能抹掉此前的 NOP/IF/call-destination 改写。每轮都从当前 IL 重算。

## GUI 与重载

修改已加载的核心 workflow、provider 或样本代码后，默认完整重启 Binary Ninja。workflow 注册不可变，provider registry 不会安全地覆盖同 ID。`bn py exec` 能验证函数逻辑，不等于 GUI activity 已加载新代码。

不要为确认 cleanup 主动安排重新分析，不要未经用户许可保存/覆盖 BNDB。
