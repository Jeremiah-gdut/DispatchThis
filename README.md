# DispatchThis

DispatchThis is a Binary Ninja workflow plugin for ARM64 ELF deobfuscation. It
recovers indirect branch targets, indirect call targets, selected global
constants, decrypted string comments, and flattened dispatcher edges at the IL
level. It does not patch bytes.

![license: MIT](https://img.shields.io/badge/license-MIT-green)
![Binary Ninja 5.3.9757](https://img.shields.io/badge/Binary%20Ninja-5.3.9757-black)

## What it does

The target obfuscator flattens control flow with a compare-tree dispatcher keyed
on a state variable. Original basic blocks set opaque state tokens, route through
decode gadgets, and return to the dispatcher.

For the current ARM64 obfuscation breakdown including indirect branches, control
flow flattening, global constants, and indirect call gadgets, see
[`docs/obfuscation.md`](docs/obfuscation.md).

## Installation

### Prerequisites

- Binary Ninja (see [Compatibility](#compatibility)).

### Install the plugin

Copy `plugins/DispatchThis` into your Binary Ninja user plugins directory and
restart Binary Ninja.

For example: `~/.binaryninja/plugins/DispatchThis`

| OS | Plugins path |
| --- | --- |
| **macOS** | `~/Library/Application Support/Binary Ninja/plugins/` |
| **Linux** | `~/.binaryninja/plugins/` |
| **Windows** | `%APPDATA%\Binary Ninja\plugins` |

## Usage

The fastest way to test one function is the function context menu. With the
target function open, right-click inside the function and use:

- **DispatchThis ▸ Profile ▸ Use default** or **Use dyzznb** - selects the active
  resolver profile for the current BinaryView.
- **DispatchThis ▸ Toggle Resolver** - toggles only indirect jump/call resolving
  for the current function.
- **DispatchThis ▸ Toggle Deflatten** - toggles only deflattening for the current
  function.
- **DispatchThis ▸ Toggle String Decrypt** - toggles only string decrypt for the
  current function.
- **DispatchThis ▸ Disable All** - disables all DispatchThis function toggles for
  the current function.

Default shortcuts are `Alt+Q` for Resolver, `Alt+W` for Deflatten,
`Alt+E` for String Decrypt, and `Alt+R` for Disable All.

The same toggles are also available from the **Function Settings** context menu.
If Binary Ninja does not reanalyze automatically after changing a setting, run
*Analysis ▸ Reanalyze All Functions*.

**Deflatten depends on indirect branch resolving.** The Deflatten setting also enables the
indirect branch and indirect call resolvers, so the full CFG can become visible before the
deflattener reconstructs dispatcher edges. Deflatten cleanup only runs after the
deflattener has rewritten the dispatcher exits, so unresolved indirect branches usually
leave the deflatten workflow phase idle.

## Pipeline at a glance

Eight workflow activities are inserted per function. One is the no-op
`Indirect Jumps/Calls` setting activity; the remaining seven are recovery workflow phases:

1. **Indirect Jumps/Calls toggle** (LLIL insertion point) - surfaces the per-function
   resolver setting.
2. **Indirect branch resolver** (LLIL) - rewrites each decode-gadget `jump(reg)` into
   `jump(const)` in the current IL. The workflow callback owns user branch metadata and
   analysis-completion tag cleanup scheduling. Re-runs to a fixpoint as the function grows.
3. **Indirect call resolver** (MLIL) - folds each import call's decode and rewrites the
   call destination to a constant pointer. The workflow callback owns call type adjustments
   and call-target phase cleanup.
4. **Branch condition translator** (MLIL) - turns resolved two-target indirect branch
   switches back into `if` expressions, then runs branch-target phase cleanup.
5. **Global constant resolver** (MLIL) - types read-only global pointer slots as constants.
6. **String decrypt** (MLIL, *opt-in*) - waits for branch, call, and global phases to
   stabilize for the current function, then annotates recognized direct decrypt calls.
7. **Deflattener** (MLIL, *opt-in*) - recovers the dispatcher cluster and rewrites each
   original basic block's dispatcher jump into a direct `goto` to the real successor.
   Conditional transitions are reconstructed when each branch arm selects one dispatcher
   state token.
8. **Deflatten cleanup / NOP pass** (MLIL, *opt-in*) - NOPs dispatcher state writes
   recorded by deflattening.

Full details, ordering rationale, and the `session_data` contract are in
[`docs/pipeline.md`](docs/pipeline.md); workflow phase coordination rules live in
[`docs/adr/0003-function-phase-state-for-workflow.md`](docs/adr/0003-function-phase-state-for-workflow.md).
Conditional deflattening has its own write-up in
[`docs/conditional-deflattening.md`](docs/conditional-deflattening.md). A file-by-file map
of the source is in [`docs/files.md`](docs/files.md).

## Scope

DispatchThis is scoped to ARM64 ELF samples handled by explicit resolver
profiles. Legacy non-ARM64 sample support is out of scope; add new binary support
as a named resolver profile instead of widening `default`.

## Compatibility

Built and tested on **Binary Ninja 5.3.9757 (a99f2380)**. The workflow and IL-rewriting
features it depends on were introduced in **3.3.3996 (2023-01-18)**, which is effectively
the minimum version required to support IL re-writes. It has only been exercised on 5.3.9757,
however, so earlier releases may behave differently.

## License

Released under the [MIT License](LICENSE).
