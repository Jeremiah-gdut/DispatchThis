# Semantic Acceptance

## Judge the named function

Validate only the function or functions named for the current task. Capture the recovery-before HLIL, the supporting current IL evidence, and the recovery-after HLIL. Then read the resulting code as a program, not as a count of facts or log entries.

Ask whether the recovered result agrees with the proved source semantics:

- Does an indirect branch or call reflect the target/value evidence and its proven direction?
- Does a recovered global describe the static slot's type without claiming an unproved runtime value?
- Does a recovered string make sense at its consumer and along its surrounding control/data flow, rather than merely looking printable?
- Does the changed control flow preserve the behavior justified by the current IL witnesses?

A multi-entry switch, an opaque expression, or an unrecovered string can be correct for the current sample. Use the surrounding HLIL and the selected task's meaning to decide whether it warrants another evidence loop. Candidate collection can help navigate; it is never a universal completeness oracle.

## Use the validation layers in order

1. Run the provider's minimal regression test for accepted and safely rejected shapes.
2. Call the provider directly on the selected real function and inspect its facts or inconclusive reason.
3. Run the actual GUI workflow for that selected function and use logs only to locate the activity.
4. Compare HLIL before and after, then make the semantic judgment.

For string work, inspect the real decoder/loop/static-initialization behavior and its consumers. If the HLIL suggests a remaining meaningful decode path, return to the evidence loop and classify it from real IL; do not infer coverage from a fixed list of candidate forms.

## Finish or defer

Finish when the requested semantic effect is clear in HLIL and agrees with its IL evidence. If the remaining behavior is outside the requested meaning, safely unproved, or confirmed as a core issue, state that classification and preserve or record it accordingly. Batch reanalysis is outside this skill's acceptance path.
