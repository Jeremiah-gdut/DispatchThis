# Resolve global constants before deflattening

DispatchThis will add global constant resolving as an MLIL workflow activity immediately before deflattening. The pass recognizes narrow global constant slots used as pointer bases, while the workflow callback owns the BinaryView-level data-variable type mutation and records view-level receipts so the same global slot is not repeatedly retyped from multiple functions.

This keeps indirect branch and indirect call resolving stable, while giving the deflattener and later HLIL generation a chance to benefit from Binary Ninja dataflow after the slot is marked as constant. The first scope deliberately skips struct recovery, broad memory-constant inference, and whole-program write proof; it only mutates slots whose known direct-reference functions do not store back to that slot.
