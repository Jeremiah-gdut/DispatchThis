# Specialize DispatchThis for ARM64 ELF samples only

DispatchThis will explicitly abandon x86, PE, and FortiEndpoint compatibility and become an ARM64 ELF analysis plugin for the user's recurring sample family. This is accepted because the original plugin's pipeline is useful, but preserving the old sample support would keep x86/PE-specific decode, call, and state-machine assumptions in the way of the narrower daily-analysis workflow.
