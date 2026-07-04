"""Binary Ninja plugin loader for the repository checkout layout."""

from pathlib import Path

_IMPL_DIR = Path(__file__).resolve().parent / "plugins" / "DispatchThis"

# BN loads this repository directory as the plugin package. The actual plugin
# package lives under plugins/DispatchThis per the upstream checkout layout.
if "__path__" not in globals():
    __path__ = [str(Path(__file__).resolve().parent)]
__path__.insert(0, str(_IMPL_DIR))

_impl_init = _IMPL_DIR / "__init__.py"
exec(compile(_impl_init.read_text(encoding="utf-8"), str(_impl_init), "exec"), globals(), globals())
