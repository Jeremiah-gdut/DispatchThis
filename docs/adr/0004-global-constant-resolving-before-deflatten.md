# 在 deflatten 前解析全局常量

> 事实 payload 与阶段名称已由 [ADR 0039](0039-provider-defines-complete-global-data-types.md)
> 扩展为完整全局数据语义；本文保留“该重新分析修改必须在 branch translation/deflatten 前
> 收敛”的顺序决定。

DispatchThis 会在 deflatten 之前立刻加入一个 MLIL global-constant resolving workflow
activity。活动 profile 识别被当作指针基址使用的窄全局常量 slot；workflow callback 独占
BinaryView 级 data-variable type mutation，将当前 data-variable type 作为权威 view state，
并记录每函数 phase receipt。不保留单独的 view-level receipt。

这样能保持间接分支与间接调用解析稳定，同时让 deflattener 和后续 HLIL generation 受益于
Binary Ninja 在将 slot 标为常量后的数据流。第一版有意不做 struct recovery、宽泛的
memory-constant inference 或全程序 write proof；只修改那些已知 direct-reference function
不会回写该 slot 的 slot。新 provider 契约不再把这种首个样本范围误写成核心类型能力上限。
