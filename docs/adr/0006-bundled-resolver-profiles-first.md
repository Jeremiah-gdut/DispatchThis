# Start with bundled resolver profiles

DispatchThis will load resolver profiles from a small bundled `profiles` package rather than from an external plugin or hot-reload system. New binary support should add a named bundled profile and register it explicitly; external profile discovery can wait until profile churn proves that editing the plugin package is the real bottleneck.
