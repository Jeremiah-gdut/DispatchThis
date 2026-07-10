# Conditional deflattening

Most flattened transitions are unconditional: an original basic block writes one
dispatcher state token and jumps back to the dispatcher. DispatchThis rewrites that
terminator into a direct `goto` to the target block for that token.

Some transitions are conditional. The current pass handles the narrow shape where an
original basic block contains an `MLIL_IF`, each branch arm is pure state-selection code,
and each arm writes exactly one known dispatcher state token before returning to the
dispatcher.

## What The Analysis Recovers

`compute_redirections` first identifies the dominant dispatcher comparison cluster and
builds a map from `(state_token, width)` to target block.

For a candidate original basic block, `_plan_conditional`:

- finds an `MLIL_IF` inside the original basic block region;
- walks the true and false regions until the dispatcher boundary;
- rejects arms that contain anything other than state-selection control flow;
- resolves the single state token written by each arm;
- maps those tokens back to target original blocks.

If both arms resolve to different known targets, `rewrite_redirections_mlil` copies the
candidate `MLIL_IF` condition into a replacement MLIL function and uses copied
source-block labels for its true and false real-successor edges. The whole replacement is
discarded if any selected transition cannot be emitted.

## Limits

This is intentionally narrower than a symbolic predicate rebuild. It does not try to
solve arbitrary multi-step state-selection chains or rewrite impure branch tails.
Unsupported shapes are left intact for Binary Ninja to display normally.
