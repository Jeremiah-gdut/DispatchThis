---
name: developping-provider
description: "Manual workflow for developing and validating an independent DispatchThis provider from a live Binary Ninja sample."
---

# Developping Provider

Use this skill only after an explicit request. Work in the provider root named by the user and keep the run focused on the requested semantic problem in the named function or functions.

## Evidence loop

Repeat this loop for one real semantic problem at a time.

### 1. Establish evidence

Use `$bn` to run `bn target list`. Treat the active GUI target as the selected sample and pass `--target active` to each subsequent target-scoped `bn` command. If the bridge state is unexpected, run `bn doctor`; stop until a real, loaded sample is available.

Before forming a rule, read [analysis-to-pattern.md](references/analysis-to-pattern.md). Inspect the recovery-before HLIL and the relevant current LLIL and MLIL in the same function. Capture the smallest IL/SSA/CFG slice that establishes the observed behavior and its expected semantic result.

Complete this step when the function, site, current IL evidence, and intended semantic effect are all concrete.

### 2. Classify the evidence

Choose exactly one route for the observed problem:

| Evidence | Route |
| --- | --- |
| A sample-specific, safely provable shape is missing | Add one provider pattern. |
| The provider cannot express a generally useful operation with the current core contract | Record a deferred capability gap in `issue.md`. |
| The core contradicts its own contract on a minimal reproduction | Record a proposed core bug in `issue.md`. |
| The evidence cannot prove a safe recovery | Preserve the original IL and report the evidence. |

Treat the core as read-only during this skill. A deferred capability gap waits for later core design. A core-bug proposal waits for the user's separate approval before any core edit.

Read [debugging-playbook.md](references/debugging-playbook.md) whenever the provider result, workflow state, current IL, or classification is unclear.

Complete this step when the selected route has evidence, not merely a plausible explanation.

### 3. Change the provider route

For a provider pattern, first establish the current provider contract from the installed DispatchThis API or source: API version, available slots, Query and fact/result types, and registration behavior. Treat this as a contract probe that can later be replaced by a formal API description or probe tool.

Create a new provider only after this loop has proved its first real pattern. Use the explicitly supplied provider root; keep the provider independent from the DispatchThis core.

Implement the narrow matcher or bounded replay required by the evidence. Add a minimal regression test in the provider's own `tests/` directory. For every new semantic pattern, create or update the provider's `patterns.md` according to [analysis-to-pattern.md](references/analysis-to-pattern.md). Use source locations only as audit evidence; derive matching from current IL shape, data flow, and CFG proof.

Call the provider directly on the selected real function before running the GUI workflow. This diagnoses provider recognition; it is not final acceptance.

Complete this step when the direct provider result has the expected fact or a precise safe-rejection reason, the test covers the new boundary, and `patterns.md` explains the implemented pattern.

### 4. Validate semantic recovery

Read [semantic-acceptance.md](references/semantic-acceptance.md). Run the relevant workflow only for the requested function or functions, then compare the recovery-before and recovery-after HLIL semantically. Use logs to locate activity behavior, not to declare success.

Focus on the user's named functions; do not expand the run into batch reanalysis. Treat automated candidate collection as navigation, while the HLIL reading supplies the completeness judgment for this sample.

Complete the loop when the requested HLIL semantic effect is demonstrated, or when the remaining issue has been safely preserved or recorded with its proven classification. Return to step 1 for the next real semantic problem.

## Provider artifacts

- `patterns.md` — durable evidence for implemented provider patterns, not a candidate inventory.
- `issue.md` — created lazily for confirmed core capability gaps and core logic bugs; see [debugging-playbook.md](references/debugging-playbook.md).
- `tests/` — minimal provider-owned regression checks for accepted and rejected shapes.
