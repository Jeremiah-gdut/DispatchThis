# Conditional deflattening

Most flattened transitions are unconditional: an original basic block writes one
dispatcher state token and jumps back to the dispatcher. DispatchThis rewrites that
terminator into a direct `goto` to the target block for that token.

Some transitions are conditional. The current pass handles the narrow shape where an OBB
contains an `MLIL_IF`, each branch arm is pure state-selection code, and each arm writes
exactly one known dispatcher state token before returning to the dispatcher.

## What The Analysis Recovers

`compute_redirections` first identifies the dominant dispatcher comparison cluster and
builds a map from `(state_token, width)` to target block.

For a candidate OBB, `_plan_conditional`:

- finds an `MLIL_IF` inside the OBB region;
- walks the true and false regions until the dispatcher boundary;
- rejects arms that contain anything other than state-selection control flow;
- resolves the single state token written by each arm;
- maps those tokens back to target original blocks.

If both arms resolve to different known targets, `apply_redirections_il` replaces the
candidate `MLIL_IF` with an `if` whose true and false labels point directly at the real
successors.

## Limits

This is intentionally narrower than a symbolic predicate rebuild. It does not try to
solve arbitrary `cmov` chains or rewrite impure branch tails. Unsupported shapes are left
intact for Binary Ninja to display normally.
