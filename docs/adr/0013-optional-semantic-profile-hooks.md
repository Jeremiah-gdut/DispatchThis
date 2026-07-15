---
status: superseded by ADR-0017
---

# ADR 0013：保持语义 profile hook 为可选

## 状态

已接受。

## 背景

Profile 将已知的样本特定识别和公式适配到稳定的 workflow backend。若强制所有六个 hook 都存在，
只使用一种能力的样本也必须重复编写 no-op function 和 forwarding wrapper。branch fact 中重复
branch coordinate 还会使提供的 address 与其 LLIL witness 的 expression index 不一致。

## 决策

保留六个按 operation 区分的 hook 名称，因为它们的 LLIL/MLIL input 和 recovery fact 表达不同
语义。profile metadata 是必需的，capability hook 则是可选的。缺失 hook 表示不支持，registry
暴露一个共享 empty-result function；存在但不可调用的 attribute 是错误。相同行为使用直接
function alias；只有行为或参数改变时才使用 wrapper。

Fact builder 从精确的当前 IL witness 推导重复 coordinate。特别是
`branch_fact(jump_il, targets)` 从 `jump_il` 推导 `source` 和
`dest_expr_index`。

不要将这些 hook 替换为 `recover(request)`、profile base class、inheritance、dynamic profile
detection 或 pattern DSL。profile 仍是纯 recognizer，workflow callback 保留全部会触发重新分析的
mutation，现有 fact/plan backend 仍是编排边界。

## 后果

- Profile 只实现样本特定能力，adapter code 更少。
- Capability matrix 必须将 hook 标为 custom、aliased 或 omitted，使 review 时不会把拼写错误
  误认为有意 omission。
- Direct alias 在 import 时绑定；更改 profile registration 或 callback 后，必须完整重启
  Binary Ninja。
- 在多个真实 profile 证明缺少同一 semantic operation，而不只是共享语法之前，延后引入新的
  abstraction。
