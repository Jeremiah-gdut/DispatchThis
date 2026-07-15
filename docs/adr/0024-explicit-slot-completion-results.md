# 显式区分槽位完成与无法证明

每个已实现的样本语义 callable 返回 `CompleteBatch[T] | Inconclusive`。缺少 callable 表示
provider 不支持该槽位；`CompleteBatch(())` 表示规则完整执行但没有恢复结果；
`Inconclusive(reason)` 不携带可应用的部分结果，也不关闭 receipt 或阶段门。

间接分支目标仍使用 `CompleteBatch[BranchTargetFact]`，但“完整”表示 provider 完整扫描了本轮
候选，且每个返回 fact 包含所属站点的完整目标集合，不表示所有候选都已证明。核心从当前
unresolved metadata、CFG 和已有 receipt 推导未返回站点，立即应用已证明事实，并在任何站点
仍未决时保持 branch phase 未稳定。只有扫描本身不可信时才返回批次级 `Inconclusive`；不新增
`BranchTargetBatch` 或第二份 unresolved 列表。

间接调用 callable 若完整证明一个站点具有多个 callee，仍把该 `CallTargetFact` 放入
`CompleteBatch`。`Inconclusive` 只表示证明过程不完整；首版后端不能应用多目标事实属于独立的
应用能力限制。核心保留该间接调用及其活目标解码，绝不选择其中一个目标，但它不构成待提交
修改，也不阻止受支持单目标工作完成后调用阶段收敛。

调用批次的“完整”只要求 provider 可信地完成本轮候选扫描，并要求每个已返回事实包含该站点
的完整 callee 集合；它不要求每个间接调用都产生事实。未匹配或未支持站点可以省略并保持原样，
不会像未恢复间接分支那样造成 CFG 缺边或阻止后续阶段。预算耗尽、遍历失败等整轮扫描不可信
仍必须返回批次级 `Inconclusive`，不能用省略掩盖。
