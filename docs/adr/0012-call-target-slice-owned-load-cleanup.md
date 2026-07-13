# ADR 0012：call-target slice 持有 dead-load cleanup

## 状态

已接受。

## 背景

间接调用 destination 被替换为具体 callee 后，普通 phase cleanup 会移除算术解码赋值，却有意
保留 load。因此 HLIL 中每个已解析调用会显示一条未使用的全局读取。若把所有 load 都视为纯，
则无关内存访问也可能被移除。通过 BinaryView xref 证明不可变性也不合适，因为混淆或不完整
CFG 会导致 xref 不完整。

## 决策

间接调用计划计算输入 `call.dest` 的完整当前 **SSA reaching-definition** slice。它跟随精确的
`SSAVariable` version 及所有 `MLIL_VAR_PHI` input。只有映射回精确当前非 SSA
`MLIL_SET_VAR` instruction 的完整 `MLIL_SET_VAR_SSA` definition 才可成为 cleanup root。
字段、拆分与别名 definition 是证明边界。slice 中 source 含有 load 的 assignment 还会被记录为
`cleanup_load_roots`。

在 mutation boundary，backend 从当前 call 重新计算两个 root set；profile fact 不携带这些
index。
若没有精确 SSA slice，call resolution 仍可进行，但 cleanup 没有 root。call destination 改写后，
phase cleanup 仅当一个带 witness 的 load assignment 的 value 在过时 target computation 之外
没有 consumer 时，才利用当前 SSA use 移除它。call、STORE、intrinsic、unimplemented IL、partial
write 与无关 load 仍不具备资格。call receipt 或其 address 前连续的 assignment 不能重新构造
cleanup ownership。该 ownership proof 中不使用 xref。

## 后果

- 间接调用解码留下的死全局读取从 HLIL 中消失。
- 被 callback argument 或普通程序逻辑复用的 target-decode value 保持 live，不会被删除。
- profile 提供的 root index 在 IL 重新生成后不能授权 cleanup；当前 call-site SSA 是
  mutation-time 的唯一权威。
- profile 提供的 `decode_def` 只是描述性 evidence。rewrite 改变 call destination，而重新
  计算的 SSA slice 独占 decode cleanup。
- Call cleanup 是 MLIL overlay；自然重新分析后从当前调用计划重新证明，绝不以旧 root index
  重放。
- 仅将旧回执重新绑定为 direct call 不提供 cleanup ownership；当没有 fresh indirect-call
  plan 时，cleanup receipt 必须保持开放，不能以空 root 集合关闭。
- fresh plan 的当前 SSA slice 无法证明时同样不提供 cleanup ownership；后端明确标记该状态，
  保持 receipt 开放，而不是把未证明误作空 slice。
