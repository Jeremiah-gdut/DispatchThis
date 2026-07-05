from contextlib import contextmanager
import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


def ensure_package(name, path):
    module = sys.modules.setdefault(name, types.ModuleType(name))
    module.__path__ = [str(path)]
    return module


ensure_package("plugins", ROOT / "plugins")
ensure_package("plugins.DispatchThis", ROOT / "plugins" / "DispatchThis")
ensure_package("plugins.DispatchThis.passes", ROOT / "plugins" / "DispatchThis" / "passes")
ensure_package("plugins.DispatchThis.passes.low", ROOT / "plugins" / "DispatchThis" / "passes" / "low")
ensure_package("plugins.DispatchThis.passes.medium", ROOT / "plugins" / "DispatchThis" / "passes" / "medium")
ensure_package("plugins.DispatchThis.utils", ROOT / "plugins" / "DispatchThis" / "utils")


class FakeILSourceLocation:
    @staticmethod
    def from_instruction(instr):
        return ("loc", getattr(instr, "expr_index", None))


class FakeMediumLevelILLabel:
    pass


class FakeLogger:
    def __init__(self, *_args, **_kwargs):
        pass

    def log_info(self, _msg):
        pass

    def log_warn(self, _msg):
        pass

    def log_error(self, _msg):
        pass

    def log_debug(self, _msg):
        pass


class FakeActivity:
    def __init__(self, config, action=None):
        self.config = config
        self.action = action


class FakeWorkflow:
    def __init__(self, _name):
        self.activities = []

    def clone(self):
        return self

    def register_activity(self, activity):
        self.activities.append(activity)

    def insert(self, *_args, **_kwargs):
        pass

    def register(self):
        pass


class FakeSettings:
    def register_group(self, *_args, **_kwargs):
        return True

    def register_setting(self, *_args, **_kwargs):
        return True

    def set_integer(self, *_args, **_kwargs):
        pass

    def set_bool(self, *_args, **_kwargs):
        pass

    def get_string(self, *_args, **_kwargs):
        return ""

    def set_string(self, *_args, **_kwargs):
        return True


binaryninja = sys.modules.setdefault("binaryninja", types.SimpleNamespace())
for name, value in {
    "Activity": FakeActivity,
    "AnalysisContext": object,
    "ILSourceLocation": FakeILSourceLocation,
    "Logger": FakeLogger,
    "MediumLevelILJump": object,
    "MediumLevelILLabel": FakeMediumLevelILLabel,
    "Settings": FakeSettings,
    "SettingsScope": types.SimpleNamespace(SettingsResourceScope="resource"),
    "Workflow": FakeWorkflow,
}.items():
    if not hasattr(binaryninja, name):
        setattr(binaryninja, name, value)

log_stub = sys.modules.setdefault("plugins.DispatchThis.utils.log", types.SimpleNamespace())
for name in ("log_info", "log_warn", "log_error", "log_debug"):
    if not hasattr(log_stub, name):
        setattr(log_stub, name, lambda _msg: None)


def load_plugin_module(name):
    path = ROOT.joinpath(*name.split(".")).with_suffix(".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name.rpartition(".")[0]
    previous = sys.modules.get(name, _MISSING)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


@contextmanager
def temporary_modules(modules, clear=()):
    names = set(modules) | set(clear)
    saved = {name: sys.modules.get(name, _MISSING) for name in names}
    for name in clear:
        sys.modules.pop(name, None)
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
