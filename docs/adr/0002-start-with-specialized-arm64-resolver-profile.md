# Start with a specialized ARM64 resolver profile

The first implementation phase will build a specialized indirect-branch resolver profile for the current ARM64 ELF sample family instead of a generic rule engine. This keeps the plugin reusable across similar daily-analysis samples while avoiding premature abstractions before multiple concrete variants prove which decode-gadget parameters actually vary.
