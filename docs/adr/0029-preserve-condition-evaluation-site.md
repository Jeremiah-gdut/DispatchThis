# 在间接跳转站点原位恢复条件分支

deinbr 在 SSA 与路径关联 PHI 求值期间同时给出完整目标、可在对应跳转站点直接使用的原语义
condition 根见证以及 `true_target`、`false_target`。该根可以包含嵌套子表达式，也可以读取
定义位于其他指令的变量。Binary Ninja 根据无方向目标投影生成当前
switch-like `MLIL_JUMP_TO` 后，translator 只在同一站点验证目标并原位替换为 `MLIL_IF`。

translator 不重新计算目标、发现条件、搜索上游 IF、证明路径区域或重定向 CFG edge。若 deinbr
无法交付可在恢复点直接使用的条件见证，该站点保持未决，而不是让 translator 建立第二套 SSA
或 CFG 求解逻辑；同批其他已完整证明的站点仍可取得进展。样本语义提供者对 condition 在恢复点
的语义有效性负责；核心只重新绑定见证并验证当前 IL 形态，不重复证明副作用、支配关系或
路径安全。

`BranchTargetFact.condition=None` 不是恢复失败，而是 provider 明确声明该站点无需 if/else
翻译；这种事实仍提交完整目标，但不创建条件回执。恰有两个目标也不自动产生条件语义；若
provider 认定站点应为条件分支却无法提供 condition 根，必须把该站点报告为未决，不能用
`None` 静默降级。provider 不为它返回 fact，核心从当前 unresolved frontier 推导该未决站点；
这不会撤销同一 `CompleteBatch` 中其他站点的完整事实。

若路径关联求值最终得到 `true_target == false_target`，则已不存在需要恢复的控制流分歧。
provider 保留求值证据中的全部路径来源，但返回去重后的单目标事实及 `condition=None`；核心不
创建条件回执，也不要求 Binary Ninja 保留同目标 IF。该投影本身不授权删除条件计算，阶段
cleanup 仍只能移除已证明死亡且纯净的目标解码赋值。
