# Specialize DispatchThis for ARM64 ELF samples only

DispatchThis will explicitly target ARM64 ELF analysis for the user's recurring sample family. This is accepted because the original plugin's pipeline is useful, but preserving legacy non-ARM64 sample support would keep unrelated decode, call, and state-machine assumptions in the way of the narrower daily-analysis workflow.
