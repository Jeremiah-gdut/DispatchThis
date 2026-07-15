---
status: superseded by ADR-0016
---

# 保持 resolver profile 纯净

Resolver profile 为间接分支、间接调用、全局常量 slot 与字符串解密调用返回标准 recovery
fact，但不得直接调用 Binary Ninja mutation API。workflow callback 仍是唯一提交会触发重新分析
mutation 的层，因此 bundled profile 的变更可以增加 binary 支持，而不会绕过 phase receipt、
stability gate 与 cleanup invalidation 规则。
