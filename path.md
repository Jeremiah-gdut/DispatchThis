# ppsp c0714 适配交接记录

本文件记录从零开始为 `ppsp` 样本补齐 DispatchThis 外部 provider 的全过程，以及切换设备后继续验证所需的事实、边界和命令。记录截至 2026-07-18。

## 1. 当前结论与范围

`sample/ppsp` 现在包含只读的 `ppsp-c0714` provider、对应的模式记录和回归测试。`__init__.py` 是与其他外部样本一致的薄加载器，实际恢复逻辑位于 `_recovery.py`。它只实现 `branch_targets`，不修改 `plugins/DispatchThis` 核心工作流，不自行调用会触发重分析的 API。

已由单元测试和对当前 Binary Ninja 数据库的只读 LLIL 探针证明的内容：

- 入口单目标 trampoline、局部字面量表 trampoline、布尔两项表和五个冻结 frame 表尾部都可以构造出受限的目标事实。
- 五个原先未恢复的 raw jump 都被当前源码的冻结 frame matcher 解析到 `0x9858c4`。
- 冻结前缀布尔 selector 支持本样本实测的 `LLIL_CMP_SLE` 与 `LLIL_CMP_E`；在标准唯一路由证明不存在时，它以独立稳定 frame-base、精确 selector slot 和静态表 pointer 证明完整目标集合。
- 同一 selector 表的两个 slot 指向同一可执行地址时，事实安全地保留为一个去重后的 target，而不是虚构两个不同后继。
- 路由重放已避免对每个候选都从 LLIL 指令 0 重放；它使用本次查询的指令快照、CFG 索引、反向可达缓存和首个路由跳转前的状态快照。

真实 GUI 验证已完成：完整重启后，`main` 的 Branch Targets 以自然 UIDF 重分析提交 `1, 1, 2, 4, 8, 16, 32, 57, 65, 128, 256, 512, 452, 0` 批次，并记录 `All of main's indirect jumps have been resolved`。最终 state 为 `branch.stable=True`、1534 个 receipt、1534 个用户分支目标、1534 个 unresolved source、0 个 unmapped source。Branch Condition Translation 与 Deflatten 保持关闭；没有强制推进任何下游改写。

## 2. 文件、路径和安装形态

| 项目 | 路径 / 值 |
| --- | --- |
| 仓库 | `C:\\Users\\magnusjiang\\AppData\\Roaming\\Binary Ninja\\plugins\\DispatchThis` |
| 当前分支 | `feature/extern_plugin`，上游为 `origin/feature/extern_plugin` |
| provider 加载器 | `sample/ppsp/__init__.py` |
| provider 恢复逻辑 | `sample/ppsp/_recovery.py` |
| 样本模式 | `sample/ppsp/patterns.md` |
| 回归测试 | `sample/ppsp/tests/test_branch_targets.py` |
| 交接记录 | 根目录 `path.md`（本文件） |
| provider ID | `ppsp-c0714` |
| provider 名称 | `ppsp c0714 entry trampoline` |
| GUI 插件软链接 | `C:\\Users\\magnusjiang\\AppData\\Roaming\\Binary Ninja\\plugins\\ppsp` → 本仓库的 `sample\\ppsp` |
| 目标函数 | AArch64 `main`，`0x978a44` |

若换机后软链接不存在，以管理员权限或已启用开发者模式的 PowerShell 创建它：

```powershell
New-Item -ItemType SymbolicLink `
  -Path 'C:\Users\magnusjiang\AppData\Roaming\Binary Ninja\plugins\ppsp' `
  -Target 'C:\Users\magnusjiang\AppData\Roaming\Binary Ninja\plugins\DispatchThis\sample\ppsp'
```

这里的 provider 是 Binary Ninja 进程启动时注册的外部插件。修改 `sample/ppsp/__init__.py` 或 `_recovery.py` 后，不能依赖热重载：注册表会拒绝重复 provider ID，已绑定 workflow activity callback 也不保证被替换。必须退出所有 Binary Ninja GUI 进程后再启动验证。

## 3. 适配时必须保持的架构边界

1. `sample/ppsp` 只读取当前 `BranchTargetQuery` 的 LLIL、LLIL SSA 和初始化数据快照，并返回 `BranchTargetFact`。它没有 workflow phase 状态，也不保存跨次调用的索引、IL 或缓存。
2. `plugins/DispatchThis/workflow.py` 是唯一允许应用事实并调用 `Function.set_user_indirect_branches` 的位置。provider 绝不能绕过这个边界。
3. 模式匹配不能硬编码源地址或跳转表地址。本文中出现的地址仅作审计/复现证据；源码只匹配当前 LLIL 数据流、CFG、栈偏移、指针宽度和可执行目标性质。
4. 一个事实必须以当前 raw `LLIL_JUMP` 为 witness，目标必须来自当前初始化数据快照并通过 `memory.is_executable_target`。不完整、歧义、越界或不可执行的目标一律不发事实。
5. provider 的 invocation-local 快照只服务当前查询；UIDF 重分析后新的 LLIL 必须重新建立所有证明。不得把旧 `instr_index`、旧 block 或旧 frame 值带入下一轮。

这些约束很重要：该函数是平坦化 dispatcher。错误地“猜一个可能目标”会污染 CFG，之后 Branch Condition Translation、cleanup 和 Deflatten 都会建立在错误事实之上。

## 4. 初始现象：长时间的 IndirectPatcher 并非死锁

样本已在 GUI 中打开，启用了 `extension.DispatchThis.IndirectPatcher` / Branch Targets。用户提供的日志显示同一 `main` 的 `resolve_llil` 不断产生更大的 mutation batch，例如 `4`、`8`、`16`、`32`、`48`、`56`。这说明恢复正在扩张 flattened CFG，并不是 callback 完全停住。

一次较早的真实运行记录如下：

| 时间（2026-07-17） | 提交的间接分支更新 |
| --- | ---: |
| 11:46:18 | 49 |
| 11:48:15 | 98 |
| 11:51:15 | 196 |
| 11:56:23 | 392 |
| 12:05:35 | 784 |
| 12:10:52 | 0 |
| 12:15:58 | 0 |

首次大轮更新总共约 1869 秒。随后一次零事实确认轮日志从 `13:18:41` 的 `resolve_llil` 到 `13:18:44` 的 `Analysis update took 1249.876 seconds`。每批事实都由核心 workflow 调用 UIDF 的 `set_user_indirect_branches`，它会触发函数重新分析；因此 activity 的总耗时同时包含 provider 取证和每批之后的大规模 CFG/数据流重建。

期间偶见 `BN Agent Bridge client disconnected before response could be delivered`。它发生在长 activity 占用 bridge、CLI 非流式请求无法在响应前返回时；同一段日志仍持续提交 mutation，因此不能据此判定 provider 或 Binary Ninja 分析崩溃。运行耗时较长时不要反复用 bridge 同步查询来中断/挤占该 activity。

## 5. 从零开始的恢复路径

### 5.1 建立最小 provider 骨架

创建 `sample/ppsp/__init__.py`，注册 `SampleSemantics(provider_id="ppsp-c0714", ..., branch_targets=branch_targets)`。最开始只实现可证明的单目标分支，不添加 call/global/deflatten hooks。这样可以让核心的既有 `IndirectPatcher` 负责提交、receipt 和 phase 收敛，而样本代码只负责 recovery policy。

### 5.2 模式一：入口的 SSA 单目标 trampoline

在 `main` 的 `0x97e5e4`，当前 raw `LLIL_JUMP` 的 SSA destination 被 `evaluate_values` 解析为唯一且可执行的 `0x986a24`。匹配器要求：

- 当前 LLIL SSA 存在；
- 当前初始化数据快照存在；
- `CompleteValues` 恰有一个值；
- 该值是当前视图的可执行地址。

多值、非可执行值、缺少 SSA 或数据快照都会拒绝，而不是随意选第一项。该模式的意义是让早期 CFG 先有一个安全入口，再露出后续 table tail。

### 5.3 模式二：同 block 的字面量表 trampoline

入口后出现局部形态：当前 block 先通过 `SET_REG`/`STORE` 将表地址和 index 写入可精确追踪的栈局部，再读取 pointer-sized 表项并 `LLIL_JUMP`。匹配器：

- 只使用 raw jump 前同一 current block 的写入；
- 只解释常量、寄存器、栈局部、`ADD/SUB/MUL/AND/OR`、符号扩展和栈 load；
- 目标寄存器最后一次写必须正好是 pointer-sized `LOAD`；
- 该寄存器在 load 和 jump 之间不能被改写；
- 直达且无参数的 `CALL` 仅清空易失寄存器，但保留已证明的栈局部；其他 call/未知内存均不作为正向证明。

最后以初始化数据中的一个 pointer 读取值作为目标，并再次验证它是否可执行。

### 5.4 模式三：布尔 selector 的两项表

随后多个 dispatch block 的尾部形态为：

```text
BOOL_TO_INT(predicate) → AND 1 → STORE selector → LOAD selector
→ SX → table_base + selector * pointer_size → LOAD → JUMP
```

selector 的方向是运行时条件，provider 不猜哪一臂会发生；它只在完整形态、唯一 current CFG 路由和两个连续 pointer-sized 表项都可执行时，返回两个 target。核心随后用用户提供的间接分支信息恢复 CFG。

这里引入了 LLIL 路由重放：表地址经常在第一个 `LLIL_JUMP_TO` 之前建立，而当前 tail 在后面。每个 `LLIL_JUMP_TO.targets` 项必须是当前 basic block 的精确 start index；即使 Binary Ninja 对应的 machine address 位于该 block 第一条可见 LLIL 之前，也不能用 address 代替 index。若到 table-load block 有零个或多个候选路由，一律拒绝。

### 5.5 性能定位与修复

直接在当前 LLIL 上对 provider 做的定时显示：

```text
[ppsp-profile] elapsed=29.493996s raw_jumps=146 blocks=380 facts=0
ssa=0.050153s/50 literal=0.105644s/50 selector=29.233043s/50
[ppsp-path-profile] elapsed=28.755748s facts=0 paths=49 success=49 path_total=28.460686s
```

这排除了“SSA 求值本身很慢”的主因。热点是 selector 的 `_path_state_before`：每个候选从 LLIL 指令 0 重放到自己的 table-load 位置；第一次 route 跳转已经在 index `5871`，因此 49 条路由各自重跑了很长前缀。旧实现还会重复扫描 `llil.basic_blocks`，并对共享 dispatcher DAG 反复做可达性搜索。

为此在 provider 内做了仅当前查询有效的四项缓存/规范化：

1. 一次性建立 current instruction snapshot，避免反复 `llil[index]`；
2. 建立 instruction-to-block 映射；
3. 从 `LLIL_JUMP_TO` 建立 predecessor 图，对同一 stop block 缓存反向可达集合；
4. 在第一个 route jump 前把完整 literal state 保存为 `(index, registers, stack_values)`，每个唯一后续路径从其副本继续解释，而不是重新解释前缀。

这不是跨 analysis 的缓存，也不放入 `Function.session_data`；它仅降低同一 `branch_targets(query)` 内部的重复工作。关于模式和拒绝边界见 `sample/ppsp/patterns.md` 的 “Route-normalized dispatcher prefix”。

### 5.6 冻结前缀布尔 selector：歧义路由不是猜测许可证

在此前 186 个 receipt 已安装的 current LLIL 中，仍有 128 个 raw jump 共享如下十条连续 tail（代表审计点 `0x980f74`）：

```text
w9 = BOOL_TO_INT(w8 s<= 0xb)
w9 = w9 & 1
[x19 + selector_offset].d = w9
x9 = [x25 + table_pointer_offset].q
w10 = [x19 + selector_offset].d
x10 = sx.q(w10)
x11 = 8
x9 = x9 + x10 * x11
x9 = [x9].q
jump(x9)
```

旧 `_path_state_before` 正确拒绝它：第一个 current `LLIL_JUMP_TO` 的两个 target 都可达该 tail，因此不存在唯一 route。新 matcher 没有放宽该函数，也不从歧义路径挑一条；仅当它返回 `None` 时才使用以下独立证明：

1. 当前 tail 必须完全位于同一个 basic block，且是 `BOOL_TO_INT(CMP_E|CMP_SLE) → AND 1 → STORE → LOAD → SX → pointer stride/load → JUMP`；
2. selector store/load 是同一直接稳定 frame base 的同一精确 `_StackOffset`，且 prefix 后只有这一个精确宽度的 selector store；
3. table pointer load 是另一个可相同也可不同的直接稳定 frame base；它从第一次 routing jump 前的 current literal prefix 读出，之后不能有重叠写、未知 call、未知 intrinsic 或 base 改写；
4. 两个 pointer-sized 静态 table slot 必须都来自 initialized-data snapshot 且为可执行地址。返回值是完整去重 target 集合：不同 slot 返回两个 targets，相同 slot 只返回一个 target；从不填写 condition/true/false arm。

这条规则的 direct current-source probe 先恢复 128 个双目标 `CMP_SLE` sites；自然重新分析后又恢复 56 个双目标 `CMP_E` sites；再后续前沿中恢复 896 个同表项单目标 `CMP_E` sites。所有数字都来自当前 LLIL，源码未匹配任何地址。

### 5.7 五个残余跳转：冻结 frame 表尾部

旧 runtime 在 branch phase 还剩以下 raw jump：

```text
0x98cc94  0x98219c  0x996270  0x99588c  0x98998c
```

它们的 workflow-time `evaluate_values` 都返回 `required SSA definition is unavailable`，不是 analysis budget 耗尽。它们共享的是从 `x22` frame 的两个独立 slot 读取 32-bit signed index 和 pointer-sized table base、再按 `base + sign_extend(index) * 8` load 后 jump 的尾部。四个直接 tail 在这之前还有 SIMD/XOR 计算和无关的 decoy store；`0x98cc94` 没有本地 SIMD tail，而是经 current `LLIL_JUMP_TO` 路由共享同一冻结 frame 语义。因此 SIMD 不是 matcher 的必需条件，也不能作为 selector。

对应的真实 slot 对如下（地址仅供审计，源码未匹配它们）：

| raw jump | selector slot | table-base slot |
| --- | --- | --- |
| `0x98cc94` | `x22 + 0x354c` | `x22 + 0x3550` |
| `0x98219c` | `x22 + 0x30dc` | `x22 + 0x30e0` |
| `0x996270` | `x22 + 0x30bc` | `x22 + 0x30c0` |
| `0x99588c` | `x22 + 0x31bc` | `x22 + 0x31c0` |
| `0x98998c` | `x22 + 0x311c` | `x22 + 0x3120` |

所有这些值都在第一个 current `LLIL_JUMP_TO` 前一次性初始化。捕获时的 prefix state 为 index `5871`、9 个已知寄存器、1964 个可读 stack value。之后 1534 个 current store 的地址都可静态重建，且没有一个与每个目标 jump 所需的两个 slot 重叠；`x22` 只在前缀初始化，不在后续路径中被改写。路径中的 4 个 call 都是直达无参数 call，intrinsic 也落在精确 allowlist 内。

从这些 frame 值恢复得到：

```text
signed index = 0x3320
table base   = 0x17221f0
entry        = 0x173baf0
target       = 0x9858c4
```

新的 `_frozen_frame_table_target` 放在 selector matcher 之前。它要求：

- tail 的指令顺序、寄存器等价关系、`SX`、stride 和 pointer load 全部精确匹配；
- selector/table load 都是同一个直接稳定 frame base 的 `base ± constant` 地址；
- prefix 中存在精确宽度的值，之后没有重叠写，base 也没有被改写；
- 后缀每个 store 都可解析为确定的 stack offset；任何不确定 store、未知 call 或不在 allowlist 的 intrinsic 都拒绝；
- signed index 计算不越过 pointer 宽度边界，读出的静态 pointer 必须可执行。

这避免把 SIMD/XOR decoy 当 selector，也避免“看到一个 32 项表就全提交”的不安全行为。只要未证明具体 index，该 matcher 就不返回目标。

对 live 当前 LLIL 的只读 probe，五个 source site 都返回十进制 `9984196`，即 `0x9858c4`。后续完整 GUI restart 已让 workflow 真实消费这些事实，并最终令 branch phase 收敛。

## 6. Branch Translation 与 Deflatten 的状态

曾专门检查过用户提出的“是否已经到 deflatten”问题，结论是：当前没有执行 Branch Condition Translation，也没有实际执行 Deflatten。

- 最新 runtime state：`branch.stable=True`，1534 个 receipt 与用户目标精确对应，`conditions={}`；所有 unresolved source 都已映射。
- 调用和全局常量 phase 的状态不绕过 branch gate。
- `workflow.py` 的 Branch Condition Translation 只等待 `branch_stable`；现在它的前置条件已满足，但用户开关仍为 disabled，因而没有被强行触发。
- Deflatten 需要 branch/call/global、condition 翻译和 cleanup 证据全都成立；它从未应被当作 branch recovery 的下一步捷径。

排查 activity 顺序时 Deflatten 曾被临时启用以查看依赖，但没有产生 deflatten 改写，并已在 GUI 中关闭；最后已知日志为 `12:31:54 [ui] main: disabled Deflatten`。换机后的验证应继续保持 Deflatten 关闭，直到新的 Branch Targets 运行收敛、条件翻译确实出现、并且 cleanup receipt 在同一 current MLIL 上被重新证明。不要为确认 cleanup 主动调度重分析。

## 7. 测试与已记录的反证

新增/覆盖的测试都在 `sample/ppsp/tests/test_branch_targets.py`：

- 唯一 SSA 目标、multi-target 与非可执行值拒绝；
- 缺少初始化数据快照时返回 `Inconclusive`；
- 线性局部字面量表；
- 布尔 selector 的两项表、`CMP_E`/`CMP_SLE` 白名单、歧义路由上的冻结 prefix proof、额外 selector-slot 写入拒绝，以及重复 table entry 的安全去重；
- CFG target index 的 block-start normalization；
- 指令 snapshot、共享不可达 DAG 的反向可达缓存、prefix state 复用；
- `vdupq_laneq_s32` 仅保留 stack proof，其他 intrinsic 清空它；
- 冻结 frame tail 成功恢复，以及 slot overwrite 的拒绝。

冻结 frame 模式先以失败测试驱动。修复前的定向运行结果是：

```text
FAILED ...test_branch_targets_recovers_a_frozen_frame_table_after_the_first_route_jump
AssertionError: _CompleteBatch(facts=()) != _CompleteBatch(... targets=(0x4200,))
1 failed in 0.19s
```

实现后用 monkeypatch 做了 toggle proof：启用 `_frozen_frame_table_target` 时 fixture 返回 `0x4200`；临时令 matcher 返回 `None` 时 batch 为空；恢复 matcher 后再次返回 `0x4200`。冻结布尔 selector 的新正向测试先在 matcher 缺失时返回空 batch，重复 table entry 的测试也先以空 batch 失败；实现后两者分别返回完整双目标和精确单目标集合。最终代码验证结果：

```text
uv run --with ruff ruff format sample/ppsp
3 files already formatted

uv run --with ruff ruff check sample/ppsp
All checks passed!

uv run --with pytest pytest --rootdir=sample/ppsp/tests --import-mode=importlib -q sample/ppsp/tests/test_branch_targets.py
20 passed in 0.28s
```

`basedpyright` 在本机未安装，且已明确不安装；这不是一个隐藏的失败结果。推送前应再运行上面的 ruff/pytest 命令，并可运行 `git diff --check`。

## 8. 换机后的准确验证步骤

1. 拉取并 checkout 本提交所在的 `feature/extern_plugin`；确认 `sample/ppsp/__init__.py`、`sample/ppsp/_recovery.py`、`patterns.md`、测试和本交接文件均存在。
2. 准备原始样本和 Binary Ninja 数据库。GUI 的临时分析状态、日志和未保存的 `.bndb` 不会随着本仓库提交移动，需单独迁移或重新打开样本。
3. 确认 `plugins/ppsp` 到本仓库 `sample/ppsp` 的软链接正确；若不是，按第 2 节重建。
4. 完全退出所有 Binary Ninja GUI 进程，再启动 GUI，重新打开样本。这一步是 provider 注册/工作流 callback 刷新的必需条件。
5. 在 `main` (`0x978a44`) 只启用 Indirect Branch Targets / `extension.DispatchThis.IndirectPatcher`。确认 Deflatten 仍是 disabled。
6. 允许 activity 运行。分支数成倍增长和长时间 Activity view 耗时是该 flattened function 的已知行为；不要仅因短时间没有 UI 更新就取消。重点观察 `resolve_llil invoked`、`submitted N branch mutation(s)` 和之后的 `submitted 0` 收敛轮。
7. 用本地 `bn` CLI 在 activity 空闲时查看 log/workflow，而不是在长 callback 期间反复请求 bridge。仓库规则中可用的只读检查包括：

   ```text
   bn workflow active
   bn workflow show core.function.metaAnalysis --depth immediate
   ```

   如需查看 phase state，在已有 Binary Ninja CLI session 中读取 `main` 的 `Function.session_data["dispatchthis_workflow_state"]`；不要手动修改它。

8. 已完成的成功证据是：完整 GUI restart 后日志记录 `All of main's indirect jumps have been resolved`；`branch.stable=True`；1534 个 receipt 与用户目标精确对应；unmapped unresolved source 为 0；代表 `0x980f74` 在 HLIL 中展示为保留谓词的 `switch (*(&data_1763ad8 + sx.q(x8_1371 s<= 0xb ? 1 : 0) * 8))`。不要仅为继续下一 phase 而强制启用 Branch Condition Translation 或 Deflatten。
9. 只有在上述条件成立、翻译及 cleanup 证据都正确时，才考虑打开 Deflatten。Deflatten 的测试目标不是“能运行”，而是 current MLIL 上的精确 cleanup proof 和原子改写都成立。

## 9. 如仍未收敛，按此顺序定位

1. 首先确认 GUI 已完整重启且实际加载的是 `ppsp-c0714` 新源码。仅编辑软链接目标但不重启，通常仍是旧 callback。
2. 对五个 source site 截取当前 raw LLIL tail，并确认它仍符合 `LOAD index → SX → base + index * 8 → LOAD → JUMP`。如果 Binary Ninja UIDF 已将其改写为 `LLIL_JUMP_TO`/switch-like 形态，则不要按“raw jump 缺失”误报失败。
3. 检查新的日志是否至少出现针对这些 sites 的 branch facts。若没有，比较 tail 是否有未知 call/intrinsic、非静态 store、frame base 写入或 slot 重叠；这些都是冻结 matcher 有意拒绝的条件。
4. 若 provider 已快速返回但 Activity 仍很慢，分别记录 provider 直接探针时间和 workflow `Analysis update` 时间。前者代表 pattern/path 取证，后者主要包含 UIDF 重新分析；不要在没有区分这两者前改动 core。
5. 如果遇到 provider duplicate registration 或 workflow callback 明显为旧版本，停止热重载尝试并再次完整重启 GUI。

## 10. 当前限制与不可放宽的安全规则

- 不枚举未知跳转表的所有可能项；selector 只有在被证明为一 bit 时才读取恰好两个 entry，并返回其完整去重 target 集合；冻结 frame 只有在精确 index 被证明时才返回一个 entry。
- 不根据表地址、source address、函数名或样本版本字符串选择模式。地址在此文件中只用于复核。
- 不将任何未知 memory effect、非精确 `STORE`、带参数/间接 call 或未知 intrinsic 解释为“无副作用”。它们应清空或拒绝 frame/stack proof。
- 允许的 `vdupq_laneq_s32(reg, const)` 只是本样本的狭窄 register-only 例外；任何不同 intrinsic 或不同参数形状都不是等价模式。
- 不将本 provider 的性能缓存升格为全局缓存或 session state；当前 IL 的 identity 是证明的一部分。
- 不在 provider 中调用 `set_user_indirect_branches`、`set_call_type_adjustment` 或 `add_analysis_completion_event`。

## 11. 本次提交应包含与不应包含的内容

应提交：

- `sample/ppsp/__init__.py`
- `sample/ppsp/_recovery.py`
- `sample/ppsp/patterns.md`
- `sample/ppsp/tests/test_branch_targets.py`
- 根目录 `path.md`

不应提交：

- `.debug-journal.md` 及其 `.git/info/exclude` 条目；它们只是本轮性能诊断产物；
- Python `__pycache__`、pytest cache、Binary Ninja log 或 `.bndb` 临时状态；
- 对 `plugins/DispatchThis/workflow.py` 的猜测性改动。

本文件的作用是让下一台设备从“Branch Targets 已在 GUI 中收敛、下游 pass 仍保持关闭”的准确状态继续，而不是重新做一轮模式猜测。
