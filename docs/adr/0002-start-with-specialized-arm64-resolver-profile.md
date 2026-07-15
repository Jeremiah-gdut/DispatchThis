---
status: superseded by ADR-0015
---

# 从专用 ARM64 resolver profile 开始

第一阶段为当前 ARM64 ELF 样本族构建专用的间接分支 resolver profile，而不是通用规则引擎。
这样既能让插件复用于相似的日常分析样本，又能在多个具体变体证明实际变化的解码 gadget
参数前，避免过早抽象。
