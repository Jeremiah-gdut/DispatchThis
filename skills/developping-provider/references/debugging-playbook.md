# Debugging Playbook

## Triage by observed boundary

Use this order when recovery is missing or wrong:

| Observation | Inspect next |
| --- | --- |
| The direct provider result has no fact | Current LLIL/MLIL, the evidence slice, and the pattern's proof boundary. |
| The direct provider fact is correct but the GUI has no effect | Provider selection, enabled setting, current API contract, workflow callback, and analysis/current-IL state. |
| The GUI changes IL but HLIL meaning is wrong | Fact direction, target/value evidence, witness rebinding, and the matcher boundary. |
| The behavior requires a general operation outside the provider contract | Confirm the core classification and record `issue.md`. |

Direct provider success proves only the Python rule. Workflow logs prove only that an activity ran. Use the current GUI result and HLIL semantic reading to determine what actually happened.

## Current-IL and workflow checks

- Obtain the active provider contract from the installed DispatchThis API or source before coding against it. Record the API version, slots, Query/fact types, and registration behavior for the current run.
- Confirm the selected provider and recovery setting apply to the current function.
- Re-read the current IL after analysis changes; old IL objects, instruction indexes, and SSA conclusions are evidence for neither a new fact nor a rewrite.
- If changed registration or callback code cannot be proven bound in the GUI, restart Binary Ninja and verify the bound registry/callback before judging the workflow.
- Use the GUI log to correlate a fact with an activity; return to the evidence slice when the log and displayed semantics disagree.

## SSA and CFG proof

- Treat an unavailable SSA definition as an incomplete proof, not a reason to guess from displayed names or nearby instructions.
- Correlate a PHI operand with a unique incoming CFG edge, or with a uniquely forwarded value on that edge. Preserve the site when that correlation is ambiguous.
- Treat unknown memory effects, unresolved pointer aliases, unknown operations, or unbounded replay as reasons for a safe rejection.
- Rebind facts and plans only against current IL using the installed contract's witnesses.

## Record confirmed core issues locally

Create `issue.md` lazily in the provider root. It contains only confirmed core capability gaps and confirmed core logic bugs; it is not a generic task ledger or candidate inventory.

````markdown
# Deferred core issues

## <short capability gap or logic bug>

- Kind: `capability-gap` or `logic-bug`
- Status: `deferred` or `proposed`
- Evidence: `<sample/build, function, and site for audit only>`
- Minimal current IL/SSA slice:

  ```text
  <small reproducing slice>
  ```

- Why the provider cannot safely solve it: `<contract boundary>`
- General core need or bug reproduction: `<portable explanation>`
- For a logic bug: proposed fix and required user approval
````

Leave a capability gap deferred for later core design. Leave a logic-bug proposal in the same file until the user separately approves a core change. Keep the core read-only while developing the provider.
