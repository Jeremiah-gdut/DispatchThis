# 领域文档

工程技能在探索代码库时应按本文使用领域文档。

## 探索前先阅读

- 仓库根目录的 **`CONTEXT.md`**；或
- 若根目录存在 **`CONTEXT-MAP.md`**，先阅读它。它指向每个上下文的
  `CONTEXT.md`，只读取与当前主题相关的文件。
- **`docs/adr/`**：阅读与即将处理区域有关的 ADR。在多上下文仓库中，还应检查
  `src/<context>/docs/adr/` 内的上下文级决策。

这些文件缺失时，**静默继续**。不要报告缺失，也不要预先建议创建它们。通过
`/grill-with-docs` 或 `/improve-codebase-architecture` 进入的
`/domain-modeling` skill，会在术语或决策确实需要确定时再创建它们。

## 文件结构

单上下文仓库（多数仓库）：

```
/
|-- CONTEXT.md
|-- docs/adr/
|   |-- 0001-event-sourced-orders.md
|   `-- 0002-postgres-for-write-model.md
`-- src/
```

多上下文仓库（根目录存在 `CONTEXT-MAP.md`）：

```
/
|-- CONTEXT-MAP.md
|-- docs/adr/                          <- 系统级决策
`-- src/
    |-- ordering/
    |   |-- CONTEXT.md
    |   `-- docs/adr/                  <- 上下文级决策
    `-- billing/
        |-- CONTEXT.md
        `-- docs/adr/
```

## 使用词汇表中的术语

输出中出现领域概念时（issue 标题、重构建议、假设、测试名等），使用
`CONTEXT.md` 中定义的术语。不要改用词汇表明确避免的同义词。

若所需概念尚未进入词汇表，这是一项信号：要么正在创造项目不用的语言（应重新考虑），
要么确有空缺（记录给 `/domain-modeling`）。

## 标出 ADR 冲突

若输出与已有 ADR 矛盾，必须明确指出，不能静默覆盖：

> _与 ADR-0007（事件溯源订单）冲突——但值得重新讨论，因为……_
