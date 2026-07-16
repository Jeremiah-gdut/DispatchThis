# ADR 索引

ADR 是历史架构决定，不是日常 API 手册。当前权威入口是 [../../CONTEXT.md](../../CONTEXT.md)、[../pipeline.md](../pipeline.md) 和 [../sample-providers.md](../sample-providers.md)。

## 基础与旧架构

- [0001 ARM64 ELF 范围](0001-arm64-elf-only.md)
- [0002 专用 ARM64 profile](0002-start-with-specialized-arm64-resolver-profile.md)
- [0003 函数 phase 状态](0003-function-phase-state-for-workflow.md)
- [0004 全局常量先于 deflatten](0004-global-constant-resolving-before-deflatten.md)
- [0005 纯 profile 契约](0005-pure-resolver-profile-contract.md)
- [0006 bundled profile 优先](0006-bundled-resolver-profiles-first.md)
- [0007 helper 基元优先](0007-profile-helper-primitives-before-resolver-framework.md)
- [0008 MLIL copy-transform](0008-copy-transform-mlil-control-flow.md)
- [0009 函数作用域设置](0009-scope-analysis-settings-to-enabled-functions.md)
- [0010 计划拥有原子 cleanup](0010-plan-owned-atomic-deflatten-cleanup.md)
- [0011 完整证据与当前 IL witness](0011-complete-evidence-and-current-il-witnesses.md)
- [0012 call-target slice cleanup](0012-call-target-slice-owned-load-cleanup.md)
- [0013 可选语义 hook](0013-optional-semantic-profile-hooks.md)

## 外部 provider 与 API

- [0014 实战恢复优先](0014-practical-recovery-first.md)
- [0015 外部样本插件](0015-external-sample-plugins.md)
- [0016 核心拥有 workflow](0016-core-owned-workflow.md)
- [0017 固定核心 phase 槽位](0017-fixed-core-phase-slots.md)
- [0018 六个样本语义槽位](0018-six-sample-semantics-slots.md)
- [0019 单一 `SampleSemantics` 注册](0019-register-sample-semantics-as-one-interface.md)
- [0020 显式 provider 选择](0020-explicit-provider-selection.md)
- [0021 分别控制每个 pass](0021-separate-switch-for-each-recovery-pass.md)
- [0022 槽位专属只读 Query](0022-slot-specific-read-only-query-inputs.md)
- [0023 核心强类型结果](0023-core-owned-typed-slot-results.md)
- [0024 明确完成或无法证明](0024-explicit-slot-completion-results.md)
- [0025 两层功能 API](0025-two-layer-functional-api.md)
- [0026 完整定义图与显式预算](0026-complete-definition-graphs-with-explicit-budgets.md)
- [0027 纯 `ValuePolicy`](0027-pure-value-policy-extension.md)

## PHI、条件和当前 IL

- [0028 CFG 入边关联 PHI](0028-correlate-phi-values-by-cfg-edge.md)
- [0029 在恢复点保留条件](0029-preserve-condition-evaluation-site.md)
- [0030 在 MLIL 翻译分支条件](0030-translate-branch-conditions-in-mlil.md)
- [0031 核心拥有条件交接](0031-core-owned-condition-handoff.md)
- [0032 条件 receipt 只属于会话](0032-keep-condition-receipts-session-scoped.md)
- [0033 以 source/operand path 重绑 IL](0033-rebind-il-by-source-and-operand-path.md)
- [0034 条件站点独立翻译](0034-translate-branch-condition-sites-independently.md)
- [0035 deflatten 前要求条件完备](0035-require-complete-condition-translation-before-deflatten.md)

## 完整目标与数据语义

- [0036 精确 provider API 版本](0036-require-exact-provider-api-version.md)
- [0037 保留完整 call target 集](0037-preserve-complete-call-target-sets.md)
- [0038 未支持 call 不阻塞下游](0038-do-not-gate-downstream-on-unsupported-calls.md)
- [0039 provider 定义完整 global type](0039-provider-defines-complete-global-data-types.md)

修改核心边界前，先从本页定位相关 ADR；不要把每份 ADR 的历史论证复制回 README 或 provider 指南。
