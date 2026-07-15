# 核心拥有条件事实交接

样本插件只返回当前 IL 上的条件分支目标事实；DispatchThis 核心在提交
`set_user_indirect_branches` 前提取稳定语义定位信息，并在重新分析后重新绑定当前 LLIL/MLIL
见证。跨阶段状态、失效传播和重新分析属于核心 workflow 边界，若交给样本插件，每个样本都
必须复制时序与回执逻辑；若让 translator 重跑 deinbr，又会重新混合目标求解和控制流翻译。
合法的 `BranchTargetFact.condition=None` 仍可提交目标，但不形成条件回执，也不进入 translator
或条件翻译完备门。某站点未形成应有的条件事实时，核心保留该处 Binary Ninja 的未解析间接
控制流标记并让 branch phase 保持未稳定，但继续提交同批其他完整站点事实；translator 不处理
该未决站点，也不把它重新归类为条件翻译失败。

条件回执只为两个不同的 `true_target`、`false_target` 建立。同目标结果必须由 provider 作为
`condition=None` 的去重目标事实交付；非空 condition 与相同有向目标的组合违反结果契约，
不能靠创建一个必然无法匹配当前 `GOTO` 的回执继续运行。

增量扫描省略已有且仍与 Binary Ninja 当前用户目标一致的 source，不会使其条件回执失效；省略
只是避免重复求解。只有同源新鲜事实明确替换 condition 或方向、所属分支目标回执改变，或者
provider/相关 pass 生命周期改变时，核心才撤销旧条件交接。
