# DispatchThis

DispatchThis 是用于 ARM64 ELF 反混淆的 Binary Ninja 工作流插件。它在 IL
层恢复间接跳转目标、间接调用目标、部分全局常量、解密字符串注释和控制流
平坦化调度器边；不会修改二进制字节。

![license: MIT](https://img.shields.io/badge/license-MIT-green)
![Binary Ninja 5.3+](https://img.shields.io/badge/Binary%20Ninja-5.3%2B-black)

## 功能

目标混淆器以状态变量为键，用比较树调度器平坦化控制流。原始基本块写入不透明
状态令牌，经解码 gadget 回到调度器。

当前 ARM64 混淆形态（间接跳转、控制流平坦化、全局常量和间接调用 gadget）见
[`docs/obfuscation.md`](docs/obfuscation.md)。

## 安装

### 前提条件

- Binary Ninja（见[兼容性](#兼容性)）。

### 安装插件

将 `plugins/DispatchThis` 复制到 Binary Ninja 用户插件目录后，重启 Binary
Ninja。

例如：`~/.binaryninja/plugins/DispatchThis`

| 操作系统 | 插件目录 |
| --- | --- |
| **macOS** | `~/Library/Application Support/Binary Ninja/plugins/` |
| **Linux** | `~/.binaryninja/plugins/` |
| **Windows** | `%APPDATA%\Binary Ninja\plugins` |

## 使用方法

测试单个函数最快的方法是函数右键菜单。打开目标函数后，在函数内右键并选择：

- **DispatchThis ▸ Profile ▸ Use default** 或 **Use dyzznb**：为当前
  BinaryView 选择解析 profile。`default` 为历史设置保留的 dyzznb 兼容入口；新适配请选用
  具名 profile。
- **DispatchThis ▸ Toggle Resolver**：仅切换当前函数的间接跳转/调用解析。
- **DispatchThis ▸ Toggle Deflatten**：仅切换当前函数的去平坦化。
- **DispatchThis ▸ Toggle String Decrypt**：仅切换当前函数的字符串解密。
- **DispatchThis ▸ Disable All**：关闭当前函数的全部 DispatchThis 开关。

默认快捷键：Resolver 为 `Alt+Q`，Deflatten 为 `Alt+W`，String Decrypt 为
`Alt+E`，Disable All 为 `Alt+R`。

相同开关也在 **Function Settings** 右键菜单中。若改设置后 Binary Ninja 没有
自动重新分析，执行 *Analysis ▸ Reanalyze All Functions*。

**去平坦化依赖间接跳转解析。** Deflatten 设置也会启用间接跳转和间接调用解析，
以便在重建调度器边前获得完整 CFG。已严格证明过时的状态写入会与边改写在同一次
原子替换中 NOP；未解析的间接跳转通常会使去平坦化阶段保持空闲。

## 流水线概览

每个函数插入八个工作流 activity。其中 `Indirect Jumps/Calls` 是无操作的设置
activity，其余七个为恢复阶段：

1. **Indirect Jumps/Calls 开关**（LLIL 插入点）：暴露按函数生效的解析器设置。
2. **间接跳转解析器**（LLIL）：将每个解码 gadget `jump(reg)` 改写为当前 IL 中的
   `jump(const)`。工作流回调负责用户跳转元数据和分析完成后的标签清理调度；随着
   函数扩展会反复运行直至不再变化。
3. **间接调用解析器**（MLIL）：折叠每个导入调用的解码，并将调用目标改写为常量
   指针。工作流回调负责调用类型调整和调用目标阶段清理。
4. **分支条件翻译器**（MLIL）：将已解析的双目标间接跳转 switch 还原为 `if`
   表达式，然后执行分支目标阶段清理。
5. **全局常量解析器**（MLIL）：将只读全局指针槽位标注为常量。
6. **关联存储恢复**（MLIL）：当合并丢失同级 PHI 值之间的对应关系时，恢复按路径
   区分的全局存储。
7. **字符串解密**（MLIL，*可选*）：等待当前函数的分支、调用和全局阶段稳定后，
   为已识别的直接解密调用添加注释。
8. **去平坦化器**（MLIL，*可选*）：恢复调度器比较簇，并构造原子替换 MLIL，
   将每个原始基本块的调度器跳转改为真实后继的直接 `goto`。条件转移会改写私有臂
   出口、私有共享语义尾部出口，或只在被跳过的状态通道已证明私有时捷径化原始条件。
   相等、不等及有符号/无符号有序调度器均通过重放具体状态令牌路由。所有私有调度器
   出口和每一条被精确证明过时的状态写入，都在同一全有或全无的 copy-transform 中
   改写。比较别名必须是其各自调度器行内建立的整变量、等宽复制；未解决的字段、
   split、aliased、地址逃逸或指针状态修改会保留受影响的转移。改写前会拒绝过期的
   当前 MLIL 计划对象。

完整细节、排序原因及 `session_data` 契约见
[`docs/pipeline.md`](docs/pipeline.md)；工作流阶段协调规则见
[`docs/adr/0003-function-phase-state-for-workflow.md`](docs/adr/0003-function-phase-state-for-workflow.md)。
条件去平坦化另见 [`docs/conditional-deflattening.md`](docs/conditional-deflattening.md)。
源码逐文件说明见 [`docs/files.md`](docs/files.md)。

## 范围

DispatchThis 面向由显式解析 profile 处理的 ARM64 ELF 样本。旧版非 ARM64 样本不在
范围内；应为新二进制添加具名解析 profile，而不是扩大 `default` 兼容入口的适用范围。

## 兼容性

设计目标为 **Binary Ninja 5.3 或更高版本**，已在 **5.3.9757 (a99f2380)** 上测试。
不强制具体 patch/build 版本；5.3 以前不受支持。控制流改写使用 Binary Ninja 的
copy-transform API 和 `AnalysisContext.set_mlil_function`，不支持旧式 MLIL 赋值回退。
每个启用函数最早的解析回调都会在 Function 作用域检查 DispatchThis 所需的分析环境；
这些覆盖设置在插件禁用后仍会保留。

## 许可证

按 [MIT License](LICENSE) 发布。
