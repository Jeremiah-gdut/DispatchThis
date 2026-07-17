# Evidence to Pattern

## Read the real sample first

Use the current function's IL, not a remembered formula or a decompiler transcript. Inspect layers in this order:

| Concern | Primary evidence | Semantic check |
| --- | --- | --- |
| Indirect branch | LLIL `jump(reg)` and its complete current definition chain | HLIL/MLIL control-flow result |
| Indirect call | MLIL call destination and its complete value evidence | call meaning and recovered target form |
| Global data | Nested MLIL static load, mapping/type boundary, and overlapping local stores | recovered data-variable type |
| String recovery | Decoder call, inline loop, or short static-initialization path | use of the recovered text in HLIL |
| Correlated store or deflatten | Current MLIL/SSA/CFG witnesses required by the installed contract | resulting state/control-flow semantics |

Use HLIL to judge meaning. Derive recovery facts from LLIL or MLIL as required by the slot.

## Form a narrow pattern

Keep the matcher anchored in operations, operands, current data flow, and local CFG proof.

- For a branch, prove every concrete target through the current LLIL chain. Supply a condition and direction only when both are proven. A non-directional target set or dispatcher may correctly remain a switch.
- For a call, preserve the complete destination set. A multi-target call is evidence, not permission to choose a direct callee.
- For global data, walk the full MLIL expression tree and exclude overlapping current-function stores. Recovering a data-variable type does not prove a runtime load can become an immediate value.
- For strings, recognize a constrained decoder call, inline loop, or static-initialization path. Keep replay bounded by bytes, steps, visited state, and a termination condition. Read the surrounding semantics to decide whether another real pattern is present.
- For any current-IL fact or plan, preserve the installed contract's exact witnesses and return an explicit inconclusive result when the proof is incomplete.

Use an address, plaintext, or key only to audit the observed sample. Keep those values out of the matching rule.

## Maintain `patterns.md`

Create `patterns.md` when the provider gains its first implemented semantic pattern. Add one entry per distinct pattern; update an entry when its matcher boundary changes. Do not turn it into a list of every automatically found candidate.

Use this shape:

````markdown
## <semantic pattern name>

- Slot: `<DispatchThis slot>`
- Observed: `<sample/build, function, and site for audit only>`
- Meaning: `<what this pattern proves or recovers>`
- Required proof: `<definitions, CFG edges, memory/type facts, and bounds>`
- Safe rejection: `<ambiguities or effects that preserve the original IL>`
- Test: `<provider-owned regression test>`

```text
<minimal real current IL slice>
```
````

Build the code block from the real SSA definition chain. Retain the relevant IL operations, SSA versions, and PHI predecessor/edge relationships; retain CFG edges when they establish direction or ownership. Cut unrelated statements rather than rewriting the slice into an idealized formula.

## Pattern completion

A pattern is ready for implementation only when its evidence slice identifies the source operation, all required proof edges, and the expected fact. A safe rejection is a valid outcome when a required edge, value, memory effect, or termination condition is not proven.
