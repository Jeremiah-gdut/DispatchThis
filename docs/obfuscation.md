# 混淆形态

> [!NOTE]
> 本页保留当前样本及旧 profile 所处理形态的技术说明。目标架构不把这些公式提升为核心规则：
> 跳转/调用目标算法、加解密算法和平坦化具体结构由每个外部单样本 provider 负责；核心只提供
> 完整证明所需的通用 BNIL、SSA、PHI、CFG 和改写后端能力。

本文说明 DispatchThis 当前面向的 ARM64 ELF 混淆形态。具体二进制差异属于解析 profile；
这里只说明共享恢复模型。

## 高层结构

控制流被平坦化：原始基本块不再直接彼此跳转。调度器将状态变量与不透明状态令牌比较，
并路由到下一个原始基本块。

一次转移的运行时流程：

```text
原始块 -> 设置 state = <下一个令牌> -> 解码 gadget -> 跳转调度器
       -> 比较树调度器 -> 下一个原始块
```

DispatchThis 在 IL 层重建该结构：恢复状态令牌目标，将受调度器控制的出口改为直接 MLIL
边，不触碰底层字节。

## 状态变量与调度器

调度器是围绕一个状态变量的比较树。每次比较将一个状态令牌映射到该令牌选择的原始基本
块。状态令牌可能宽于 32 位，因此宽度是令牌身份的一部分。

去平坦化器识别主导调度器比较簇，将 `(state_token, width)` 映射为目标块，再跟踪各原始
基本块的状态写入来恢复直接后继。

## 解码片段（gadget）间接跳转

原始基本块不是直接跳回调度器，而是通过解码 gadget。ARM64 解析 profile 解析计算分支
目标的 LLIL 数据流，并向工作流返回分支恢复事实。

当前内置分支公式为：

```text
table_base = (*slot + table_base_key) mod 2^48
entry      = *(table_base + entry_offset)
target     = (entry + key) mod 2^48
```

`slot`、`table_base_key`、`entry_offset` 和 `key` 从 LLIL 定义与镜像内存恢复。profile
可以识别不同指令形态，但仍返回标准分支事实；工作流负责 Binary Ninja 分支元数据和重新
分析回执。

## 间接调用片段（gadget）

间接调用恢复在分支解析稳定后于 MLIL 运行。当前形态将编码后的 callee 值与一个 key
折叠：

```text
target = (encoded + key) mod 2^48
```

结果是有效 callee 时，pass 会将当前 MLIL 调用目标改写为常量指针。工作流负责调用类型
调整与回执门控，避免反复分析形成循环。

## 全局常量槽位

部分样本将类似指针的常量放在可写全局槽位。全局常量阶段识别狭窄的只读槽位使用形态，
并请求工作流把槽位类型设为 `uint8_t const* const`。这使后续 MLIL 数据流可将该槽位
视为稳定，而不必把 BinaryView 修改放入解析 profile。

## 字符串解密调用

字符串解密恢复为可选项。它扫描当前函数的直接 MLIL 调用，要求解密 callee 已完成去
平坦化且稳定，然后由活动 profile 返回明文恢复事实。后端写入函数级注释并保留已有手工
注释行。

## 条件转移

多数转移在回到调度器前写入一个状态令牌；少数根据程序控制流写入两个令牌之一。
DispatchThis 处理每个纯分支臂都恰好写入一个已知状态令牌的狭窄 MLIL 形态；不支持或
不纯的形态保持不变。见
[`conditional-deflattening.md`](conditional-deflattening.md)。
