# Reference: conditional OBB chain @ 0x140082b3e

Function: `detect_browsers` @ `0x14006f570`
Binary: `FortiEndpoint_Patch.exe.bndb`

This is the canonical example for handling **conditional** dispatcher transitions
(compound `||` / `&&` cmov chains). Saved so we don't need MCP continuously.

## Disassembly (address-ordered)

```
# Block 385 at 0x140082b3e
140082b3e  48 8b 85 18 01 00 00  mov    rax, qword [rbp+0x118]   ; rax = var_420 (bool*)
140082b45  80 38 00              cmp    byte [rax], 0x0
140082b48  b8 78 08 61 db        mov    eax, 0xdb610878          ; STATE A  (default / "neither")
140082b4d  ba a6 13 b3 e7        mov    edx, 0xe7b313a6          ; STATE B  (alternate)
140082b52  0f 45 c2              cmovne eax, edx                 ; if (*var_420 != 0) eax = B
140082b55  48 8b 8d 20 01 00 00  mov    rcx, qword [rbp+0x120]   ; rcx = var_418 (bool*)
140082b5c  80 39 00              cmp    byte [rcx], 0x0
140082b5f  0f 45 c2              cmovne eax, edx                 ; if (*var_418 != 0) eax = B
140082b69  48 8b 8d d8 04 00 00  mov    rcx, qword [rbp+0x4d8]   ; rcx = &state_var (var_60)
140082b69  89 01                 mov    dword [rcx], eax         ; STATE WRITE: state = eax
# --- decode gadget (reload state, compute jump target) ---
140082b6b  48 8b 05 1e 4f 28 00  mov    rax, qword [rel 0x140307a90]
140082b72  8b 04 30              mov    eax, dword [rax+rsi]
140082b75  48 8b 0d 1c 4f 28 00  mov    rcx, qword [rel 0x140307a98]
140082b7c  8d 50 ff              lea    edx, [rax-0x1]
140082b7f  0f af d0              imul   edx, eax
140082b82  83 3c 31 0a           cmp    dword [rcx+rsi], 0xa
140082b86  b8 c0 02 00 00        mov    eax, 0x2c0
140082b8b  b9 a0 0b 00 00        mov    ecx, 0xba0
140082b90  48 0f 4c c1          cmovl  rax, rcx
140082b94  f6 c2 01              test   dl, 0x1
140082b97  48 0f 44 c1          cmove  rax, rcx
140082b9b  48 03 05 fe 4e 28 00  add    rax, qword [rel 0x140307aa0]
140082ba2  48 8b 04 06          mov    rax, qword [rsi+rax]
140082ba6  4c 01 e8             add    rax, r13
140082ba9  ff e0                jmp    rax                       ; CHAIN-EXIT JUMP (gadget)
```

## MLIL (index-ordered, reassembled into program order)

```
# OBB entry block @ 0x140082b3e
140082b3e   rax_4601 = var_420
140082b45   cond:156_1 = [rax_4601].b != 0
140082b48   rax_4602 = -0x249ef788                 ; = 0xdb610878  (STATE A, default)
140082b52   if (cond:156_1) then 5867 else 5869

# idx 5867 (then of var_420 test) @ 0x140082b52
140082b52   rax_4602 = -0x184cec5a                 ; = 0xe7b313a6  (STATE B)
140082b52   goto 5869

# idx 5869 (var_418 test) @ 0x140082b55
140082b55   rcx_901 = var_418
140082b5f   if ([rcx_901].b != 0) then 7278 else 7280

# idx 7278 (then of var_418 test) @ 0x140082b5f
140082b5f   rax_4602 = -0x184cec5a                 ; = 0xe7b313a6  (STATE B)
140082b5f   goto 7280

# idx 7280 (converged: store + gadget) @ 0x140082b62
140082b69   [rcx_902].d = rax_4602                 ; STATE WRITE (rcx_902 = &state_var)
140082b72   rax_4604 = ...                          ; gadget begins
...
140082ba9   jump(... )                              ; CHAIN-EXIT JUMP
```

## Semantics

```
state = (*var_420 != 0 || *var_418 != 0) ? STATE_B : STATE_A
```

- STATE A = `0xdb610878` (signed `-0x249ef788`) - the **default** value, mov'd before any cmov.
- STATE B = `0xe7b313a6` (signed `-0x184cec5a`) - the **alternate**, selected by `cmovne`.
- Both cmovs select the SAME alternate => logical OR. (AND / mixed compounds would
  have cmovs writing different values; the override-by-last-true rule still applies.)

`resolve_to_constants` on the state write `[rcx_902].d = rax_4602` yields
`{0xdb610878, 0xe7b313a6}` => `CFGLink.is_reconstructable` (exactly 2 states).
Each state maps through the backbone to a real successor block:
  STATE_A -> succ_A, STATE_B -> succ_B.

## cmov -> MLIL_IF pattern (general)

Each `cmovne eax, edx` becomes a 2-block diamond:
  `if (cond) then <block: rax = ALT; goto join> else <join>`
The MLIL_IF condition is the real predicate. The then-block's only effect is the
cmov assignment. Diamonds are LINEAR (each reconverges to the next test), not a
full tree - so there are N+1 program paths sharing the final join, not 2^N leaves.

## Reconstruction goal

Keep the MLIL_IF predicates; re-point them so control flows to the real successor
directly instead of through the state write + gadget. Because later cmovs override
earlier ones, the routing decision depends on the FINAL value of `rax_4602`, which
is only known after the last diamond - so the natural decision point is the
converged block (where the gadget jump currently lives).

### Truth table for this example (c1 = *var_420!=0, c2 = *var_418!=0)

| c1 | c2 | final rax_4602 | state | successor |
|----|----|----------------|-------|-----------|
| F  | F  | 0xdb610878     | A     | succ_A    |
| F  | T  | 0xe7b313a6     | B     | succ_B    |
| T  | F  | 0xe7b313a6     | B     | succ_B    |
| T  | T  | 0xe7b313a6     | B     | succ_B    |

=> `if (*var_420 || *var_418) goto succ_B; else goto succ_A;`

---

## Other shapes in `detect_browsers`

Inventory of all 26 conditional (state-selection) blocks in this function:
- 24 **simple** (single cmov): `cmovne` (15), `cmove` (10), `cmovb` (1).
- 2 **two-condition OR** (`0x140082b3e`, `0x1400835e4`): both cmovs select the
  same alternate, same sense.
- 0 literal `&&` *in this function* (see De Morgan note below).

### Simple conditional @ 0x1400833c1

```
1400833c1  cmp    byte [r8], 0x0
1400833c5  mov    eax, 0x615f1eca        ; STATE A (default)
1400833ca  cmovne eax, ecx               ; ecx = 0xf79803fa STATE B, from a predecessor
1400833cd  mov    dword [r10], eax       ; state store
# gadget + jmp
```
MLIL: single diamond `if ([r8].b != 0) { rax = 0xf79803fa } ; [r10].d = rax`.
STATE A `0x615f1eca` -> dispatcher leaf `if (state == 0x615f1eca)` -> block `0x140075084`.
The alternate (`ecx`) is materialised in a predecessor block but `resolve_to_constants`
follows the VAR def, so both states resolve.

### Predicates are not always `!= 0`

`0x14007b446`: `cmp dword [r8], 0xffffffff ; cmove eax, r15d` =>
`state = (*r8 == -1) ? r15d : 0x2cb44d67`. We never interpret the predicate
ourselves -- the MLIL_IF condition already encodes it exactly.

### `&&` via De Morgan (general, even if absent here)

A cmov override chain with a single alternate can only directly express OR.
An `&&` is emitted as `!(!c1 || !c2)`: `default = STATE_B`, then `cmove`s
selecting `STATE_A` when each condition is *false*:
```
mov    eax, B                 ; default = both-true value
cmove  eax, A_reg             ; if *p1 == 0  -> A
cmove  eax, A_reg             ; if *p2 == 0  -> A
=> state = (*p1!=0 && *p2!=0) ? B : A
```
Structurally identical to OR (chain of conditional const-assignments + store);
only the default/alternate roles and the cmov sense differ. Modeling the cmov
sequence handles `||`, `&&`, and mixed uniformly with no classification.

---

## Historical symbolic model

Model each conditional block as: a single state-temp `t`, an ordered list of
conditional assignments, and a store:

```
t = default_const
for (cond_i, val_i) in order:    # one per cmov (val_i is a const or const-resolving var)
    if cond_i_fires: t = val_i   # cond_i_fires == the MLIL_IF condition (true branch)
store state_var = t
```

Final value = **last firing assignment, else default** (cmov override semantics).
Constraint from the analyzer: `t` takes **exactly two** distinct constant values
across the chain {STATE_A, STATE_B}, each mapping (via the backbone) to one real
successor block (`CFGLink.cases`).

**Monotone case (all alternates == the single non-default state):** first-firing
condition in *forward* order already determines the outcome (any fire -> alternate,
none -> default). No reordering needed -- covers every OR/AND/mixed-sense block
observed here.

**Non-monotone case (a later cmov re-selects the default value, e.g.
`default A; c1->B; c2->A`):** forward-first-match is WRONG (`c1` true does not
imply B because `c2` may override back to A). A symbolic model can build the ITE
expression for `t` over boolean condition vars, then for each leaf derive
`final == A` vs `final == B`, yielding a minimal/correct decision tree.

Symbolic sketch:
```
t = default
for c_i, v_i in chain:  t = If(c_i, v_i, t)        # ordered ITE = override semantics
# t now a function of the boolean c_i; only two possible concrete values A,B
# enumerate / simplify to get predicate P with: P(conds) => succ_B, else succ_A
```

---

## Replacement strategy (evaluated)

Concern: rewriting must not strand or destroy **real** instructions; only the
**gadget-specific** ones (state consts, state store, decode gadget, indirect jump)
may go.

Key facts for these conditional blocks:
- They are **routing-only** (verified for `0x140082b3e`, `0x1400833c1`): no real
  "work" instructions, only the predicate computation + obfuscation.
- The MLIL_IF **condition expressions are real logic** (the program's actual
  booleans) and are *preserved by keeping the IF instructions*.
- The only obfuscation artifacts are the `t = const` assignments, the
  `state_var = t` store, the decode gadget, and the `MLIL_JUMP_TO`.

**Chosen approach: edge re-pointing only -- never hand-delete instructions.**
1. For each MLIL_IF in the chain, `replace_expr` its terminator with a new
   `if(SAME condition expr, true_label, false_label)` whose labels target either a
   successor block or the next condition block, per the symbolic decision tree.
2. `replace_expr` the converged gadget block's `MLIL_JUMP_TO` with a `goto` to the
   fall-through successor.
3. Leave every value-computation instruction in place. After `finalize()` +
   `generate_ssa_form()`, the now-unreferenced state consts, the store, and the
   whole decode-gadget block are unreachable/dead and BN's analysis drops them.

Why edge-only beats synthesize-and-delete: it touches **only terminators** (the
gadget jump and the IF edges), reuses the exact original condition expressions
(semantics preserved), and delegates removal to BN's proven DCE -- so we can never
accidentally delete a real instruction. This directly satisfies "excluding
jump-gadget-specific instructions."

Location note: because the chain is entered at its top block (via the previous
OBB's gadget) and all alternates collapse to the single non-default state, forward
order needs no physical block reordering for the monotone cases. Only the
non-monotone (re-selection) case requires the symbolic tree to re-point edges so the
dominating condition is evaluated first; even then we re-point edges, not move
instructions.

---

# Implementation log: bugs found & fixed (2026-06-20)

Worked end-to-end on the canonical non-monotone block `0x14008a90d` in
`sub_140088ad0` (the `RegOpenKeyExA`/`RegQueryValueExA` HKLM-then-HKCU registry
read). Expected semantics: the HKCU attempt must run **only if** the HKLM
`RegOpenKeyExA` fails **or** the HKLM `RegQueryValueExA` fails. Three independent
bugs each broke this; all are now fixed and the block decompiles correctly.

## The block (ground truth)

```
14008a90d  cmp dword [rbp], 0x0      ; cond1: value stored != 0   (var_58)
14008a911  mov eax, 0x9e454962       ; default state D
14008a916  cmovne eax, ecx           ; if cond1:  eax = ecx   (alternate state E)
14008a919  cmp byte [rbp+0x10], 0x0  ; cond2
14008a91d  cmove eax, edx            ; if (*[rbp+0x10]==0): eax = edx  (edx = 0x9e454962 = D)
14008a920  mov dword [rbp+0x14], eax ; state write
```

`state = E iff (cond1 && query-ok)`, else default `D`. The second cmov re-selects
the **default** -> non-monotone `&&`. Crucially the alternates are **registers**
(`ecx`/`edx`) materialised in other blocks, not literals.

## Bug #1 -- `build_cond_plan` dropped register-sourced alternates

`_temp_const` accepted only a literal `MLIL_CONST` as a diamond's then-value. When
the cmov alternate reaches the then-block as a register copy (`eax = ecx`) -- which
BN does NOT constant-fold unless the `mov reg, IMM` sits adjacently before the cmov
-- it returned `None`, so `build_cond_plan` bailed ("shape not recognised"), the
transition was left intact, and the gadget cleanup then collapsed it into one
unconditional `goto` (symptom: HKCU always runs). The blocks that worked (e.g.
`var_45 @ 0x14008a008`) only did so because BN folded an adjacent `mov ecx,IMM;
cmovne` into a literal then-block.

**Fix:** `_temp_const(func, il, temp_var, mask)` now resolves the then-value
transitively via `resolve_to_constants` (single-constant only), so register-carried
alternates resolve like folded ones. Both call sites pass `func =
mlil.source_function`.

## Bug #2 -- stray unconditional redirects clobbered the conditional jump

A conditional chain also surfaces its component cmov writes (the entry default and
each diamond then-block) as separate single-state links. All of them forward-walk
to the SAME chain-exit jump as the converged store, so each produced an
unconditional redirection competing for that one jump. The non-monotone path writes
its branch ONTO that jump, so `apply_redirections_il`'s repeated `replace_expr`
clobbered it (last write -- an unconditional `goto` -- wins). Seen in the log as
`812: non-monotone ... (240/389)` followed by `482/639/810: redirect 0x14008a3ae ->
240/389/240`. (Monotone survives by luck: it re-points the diamond `IF`s and leaves
the converged block unreachable, so the stray jump rewrites don't matter.)

**Fix:** `compute_redirections` drops unconditional redirections whose exit-jump
`expr_index` is owned by a conditional plan (logs `dropped N stray uncond
redirect(s)`).

## Bug #3 -- non-monotone branched on the state temp (the magic-constant leak)

The original `_apply_cond_value` emitted `if (temp == default) goto ...`, reading
the state temp `rax_7745`. The NOP cleanup (which runs after all unflattening) NOPs
that temp's defs at `0x14008a911` / `0x14008a91d` -- matched BOTH **by value**
(`0x9e454962` is a recorded dispatcher state const) AND **by var** (the temp is in
the state-var alias set, pulled in via the converged store). So the branch input was
gutted and the recovered condition kept the raw `0x9e454962`. It is a double-bind:
excluding the temp from the by-var set wouldn't help, because the by-value rule
still NOPs `temp = 0x9e454962`.

**Fix:** `_apply_cond_value` reconstructs the routing predicate from the **real
diamond `MLIL_IF` conditions** instead of the temp. `_build_cond_predicate`
enumerates the diamond-condition truth combinations, runs the same override-fold,
keeps the combinations that land on the alternate, and emits them as a
sum-of-products (`or_expr` of `and_expr`s). `_bool_lit` normalises each literal to
0/1 via `cond != 0` / `cond == 0` (built from a fresh `copy_expr`). The converged
gadget jump becomes `if (P) goto succ_alt else goto succ_default`. The whole cmov
chain/state temp is then genuinely dead and the cleanup drops it; the condition holds
only the program's own predicates.

## NOP-pass diagnostics added

`nop_pass.py` now logs, at info level, every state-write it NOPs in
`nop_state_writes` (with `value=`/`dest=` match reason), and flags any NOP inside the
gadget-taint / dead-decode rounds that references a recorded state constant
(`_cleanup_round` takes `state_consts`, helper `_ref_consts`). Grep the log for a
state const (e.g. `0x9e454962`) to see exactly where/why a write was pruned.

## Verified result (BN GUI, 2026-06-20)

MLIL of the rewritten exit jump:

```
14008a3ae  if (cond:11_1 != 0 & (var_48 == 0) == 0) then 389 @ 0x140088e2b else 240 @ 0x140089a2d
```

`P = (var_58 != 0) & (var_48 != 0)` -- both operands explicit comparisons (0/1), so
the `&` is logical AND (HLIL renders it `cond:11_1 & var_48`, both typed `bool`).
P true (`389`) -> `var_54 = 1` (HKLM fully succeeded, skip HKCU); P false (`240`) ->
the HKCU attempt. HLIL:

```
if (!rax_48 && cond:11_1 & var_48)   // HKLM open ok AND value present AND query ok
    var_54 = 1;
else { ... open HKCU, query, ... }
```

Matches the spec exactly, no magic state constant. (BN added the leading `!rax_48 &&`
short-circuit itself, because `cond:11_1`/`var_48` are only defined on the HKLM-open
path.)

## Known cosmetic residue (not a correctness issue)

The diamond `IF`s (`14008a916`, `14008a91d`) physically remain and reconverge at the
converged block, which then re-tests the conditions via `P`; BN collapses the now-
empty diamonds to gotos, so it's harmless. The state-write NOP logging is verbose --
filtered to info-level only for state-const hits; the per-instruction `via gadget-
taint`/`via dead-decode` lines are `log_debug`.
