# Keep resolver profiles pure

Resolver profiles will return standard recovery facts for indirect branches, indirect calls, global constant slots, and string decrypt calls, but they will not call Binary Ninja mutation APIs directly. Workflow callbacks remain the only layer that submits reanalysis-triggering mutations, so profile changes can adapt new sample families without bypassing phase receipts, stability gates, and cleanup invalidation rules.
