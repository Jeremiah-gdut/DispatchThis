# 条件去平坦化

条件去平坦化是 deflatten 的一个严格子集，不是通用控制流简化器。

核心只接受已经证明的 dispatcher 状态转移：两臂各自沿所有路径建立一个具体、等宽 token，token 通过当前 dispatcher CFG 重放到不同目标，且被改写区域没有外部入口或未建模状态副作用。

可接受的改写形态只有三种：

- 重写各臂私有的 dispatcher 出口；
- 保留共享语义尾部，只改写最终私有出口；
- 仅在整个被跳过状态通道已证明私有时，捷径化原 IF。

所有边改写与精确、已证明过时的状态写 NOP 在一次 MLIL copy-transform 中提交；任一 witness 失效就丢弃整批。字段/split/alias 读取、地址逃逸、未知 call/STORE、`UNIMPL`、token 宽度或路径歧义都会拒绝计划。

这也是为什么残留 switch、state 写入或 token 比较不一定是 bug：在没有完整证明时保留它们才是正确结果。工作流约束见 [pipeline.md](pipeline.md)，样本侧职责见 [sample-providers.md](sample-providers.md)。
