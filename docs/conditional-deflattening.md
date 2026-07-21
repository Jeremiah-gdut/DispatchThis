# 条件去平坦化

条件去平坦化是 deflatten 的一个严格子集，不是通用控制流简化器。

## 接受前必须全部成立

- 两臂各自沿**所有**路径建立同一个具体、等宽 token。
- token 能通过当前 dispatcher CFG 重放到不同目标。
- 被改写区域没有外部入口。
- 没有未建模状态副作用、地址逃逸或路径歧义。

## 仅接受三种改写

- 重写各臂私有的 dispatcher 出口；
- 保留共享语义尾部，只改写最终私有出口；
- 仅在整个被跳过状态通道已证明私有时，捷径化原 IF。

所有边改写与精确、已证明过时的状态写 NOP 在一次 MLIL copy-transform 中提交；任一 witness 失效就丢弃整批。字段/split/alias 读取、地址逃逸、未知 call/STORE、`UNIMPL`、token 宽度或路径歧义都会拒绝计划。

这也是为什么残留 switch、state 写入或 token 比较不一定是 bug：在没有完整证明时保留它们才是正确结果。工作流约束见 [pipeline.md](pipeline.md)，样本侧职责见 [sample-providers.md](sample-providers.md)。
