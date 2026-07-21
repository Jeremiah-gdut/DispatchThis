# 混淆诊断

先把当前函数归类，再决定 provider 是否需要新规则。这里描述的是证据形态，不是可复制到新样本的公式。

## 先定位，再写规则

| 看到什么 | 先检查 | 何时提交 |
| --- | --- | --- |
| `jump(reg)` | LLIL 定义、静态内存与真实 CFG 入口 | 所有具体目标已证明；条件还需方向证据 |
| 间接 `call` | MLIL call destination 的完整值 | 事实保留全部 callee；仅单目标可改写 |
| 全局 `load` | 表达式树、映射范围和重叠 STORE | 类型、地址和边界均精确 |
| dispatcher/state token | 路由、私有性与当前 IL witness | 每条路径和每个出口都能重放 |

## 控制流

### 间接跳转

在 LLIL 找到 `jump(reg)`，沿当前定义和静态内存读取证明所有具体目标。目标集合可以是单个或多个；只有原始语义条件和 true/false 方向也被证明时，才向 `BranchTargetFact` 提供条件。

Binary Ninja 应用 user-informed branch dataflow 后，MLIL/HLIL 可能显示 switch 或 `JUMP_TO`。这是 CFG 恢复后的正常形态，不自动意味着条件翻译缺失。

### 平坦化 dispatcher

平坦化通常表现为状态写入、返回 dispatcher、比较状态 token、再进入原始块。deflatten 需要完整 state-token 路由、私有区域和当前 IL witness；无法证明的状态写入或多入口路径必须保留。

## 数据与调用

### 间接调用

在 MLIL 检查 call destination 的完整值。多 callee 仍是有价值的事实，但当前后端不会把它猜成一个 direct call。

### 全局数据

全局 load 可能嵌在算术、字段或条件表达式中。递归遍历表达式，并拒绝有重叠本地 STORE 的槽位；只有初始化、映射范围和精确类型都成立时才提交全局数据事实。

## 字符串

同一个样本可并存多种解密形态：

| 形态 | 可证明的模式 |
| --- | --- |
| decoder 调用 | 静态 source/destination 参数、固定调用约束、受限 callee 回放、文本输出。 |
| 内联循环 | 私有 preheader、递增计数器、明确 latch、反馈 state、字节 STORE 与直接 consumer。 |
| 简单 XOR/初始化 | 静态 byte STORE、短的可解释局部路径、同地址 LLIL 块中的源 load、零终止文本。 |

模式匹配应明确拒绝未知 operation、未知内存效果、非确定路径、缺失终止符和非文本数据。不要把大函数的整个 SCC、特定地址或特定 key 当作模式。

## “还剩 switch” 的判断

确认下面三点后再认为是 bug：

1. 核心为该 branch fact 维护的 receipt 中确有该源；
2. provider 为该源返回了当前、有方向、不同的 true/false 目标和条件；
3. translator 的当前 MLIL 重绑定失败日志能归因到该站点。

缺少任一点时，保留 switch。多入口 dispatcher/state-routing 站点通常属于 deflatten 候选，而不是 IF 还原候选。

## 输出

每次诊断应记录函数地址、LLIL/MLIL 片段、provider 返回类型、日志和最终 GUI 效果。这样才能把“未识别”区分为样本模式缺失、核心通用缺口或安全拒绝。
