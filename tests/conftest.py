from contextlib import contextmanager
from enum import Enum
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
        return True

    def set_bool(self, *_args, **_kwargs):
        return True

    def get_integer(self, key, *_args, **_kwargs):
        return {
            "analysis.limits.maxFunctionSize": 0,
            "analysis.limits.expressionValueComputeMaxDepth": 99999,
            "analysis.limits.maxFunctionAnalysisTime": 1800000,
            "analysis.limits.maxFunctionUpdateCount": 1024,
        }.get(key, 0)

    def get_bool(self, *_args, **_kwargs):
        return False

    def get_string(self, *_args, **_kwargs):
        return ""

    def set_string(self, *_args, **_kwargs):
        return True


class FakeType:
    def __init__(self, key):
        self.key = key

    def __eq__(self, other):
        return isinstance(other, FakeType) and self.key == other.key

    @staticmethod
    def int(width, sign=True):
        return FakeType(("int", width, sign))

    @staticmethod
    def void():
        return FakeType(("void",))

    @staticmethod
    def pointer_of_width(width, type_):
        return FakeType(("pointer", width, type_))


class FakeFunctionType(FakeType):
    @classmethod
    def create(
        cls,
        ret=None,
        params=None,
        calling_convention=None,
        variable_arguments=False,
        stack_adjust=None,
        platform=None,
        confidence=255,
        can_return=True,
        pure=False,
        **_kwargs,
    ):
        params = tuple(params or ())
        result = cls(("function", ret, params, calling_convention, bool(variable_arguments)))
        result.return_value = ret or FakeType.void()
        result.parameters = [types.SimpleNamespace(type=param) for param in params]
        result.calling_convention = calling_convention
        result.has_variable_arguments = variable_arguments
        result.stack_adjustment = stack_adjust
        result.platform = platform
        result.confidence = confidence
        result.can_return = can_return
        result.pure = pure
        return result


FakeMediumLevelILOperation = Enum(
    "MediumLevelILOperation",
    """
    MLIL_NOP MLIL_SET_VAR_SPLIT MLIL_ASSERT MLIL_FORCE_VER
    MLIL_ADD MLIL_ADC MLIL_ADDRESS_OF MLIL_ADDRESS_OF_FIELD MLIL_AND MLIL_ASR MLIL_BOOL_TO_INT MLIL_BP
    MLIL_CALL MLIL_CALL_SSA
    MLIL_CALL_UNTYPED MLIL_CALL_UNTYPED_SSA MLIL_CALL_OUTPUT MLIL_CALL_PARAM
    MLIL_SEPARATE_PARAM_LIST MLIL_SHARED_PARAM_SLOT
    MLIL_CMP_E MLIL_CMP_NE
    MLIL_CMP_SGE MLIL_CMP_SGT MLIL_CMP_SLE MLIL_CMP_SLT MLIL_CMP_UGE
    MLIL_CMP_UGT MLIL_CMP_ULE MLIL_CMP_ULT MLIL_CONST MLIL_CONST_PTR
    MLIL_CONST_DATA MLIL_EXTERN_PTR MLIL_FLOAT_CONST MLIL_IMPORT
    MLIL_DIVU MLIL_DIVU_DP MLIL_DIVS MLIL_DIVS_DP MLIL_MODU MLIL_MODU_DP MLIL_MODS MLIL_MODS_DP
    MLIL_GOTO MLIL_IF MLIL_INTRINSIC MLIL_INTRINSIC_SSA MLIL_JUMP MLIL_JUMP_TO MLIL_RET_HINT
    MLIL_LOAD MLIL_LOAD_SSA MLIL_LOW_PART MLIL_LSL MLIL_LSR MLIL_MUL MLIL_MULU_DP MLIL_MULS_DP MLIL_NEG MLIL_NOT MLIL_OR MLIL_RET MLIL_NORET
    MLIL_ROL MLIL_RLC MLIL_ROR MLIL_RRC MLIL_SBB
    MLIL_LOAD_STRUCT MLIL_LOAD_STRUCT_SSA MLIL_MEMORY_INTRINSIC_OUTPUT_SSA
    MLIL_MEMORY_INTRINSIC_SSA MLIL_SET_VAR MLIL_SET_VAR_ALIASED_FIELD
    MLIL_SET_VAR_ALIASED MLIL_SET_VAR_FIELD MLIL_SET_VAR_SSA MLIL_SET_VAR_SSA_FIELD MLIL_SET_VAR_SPLIT_SSA
    MLIL_STORE MLIL_STORE_SSA MLIL_STORE_STRUCT
    MLIL_STORE_STRUCT_SSA MLIL_SYSCALL MLIL_SYSCALL_SSA
    MLIL_SYSCALL_UNTYPED MLIL_SYSCALL_UNTYPED_SSA MLIL_TAILCALL
    MLIL_TAILCALL_SSA MLIL_TAILCALL_UNTYPED MLIL_TAILCALL_UNTYPED_SSA
    MLIL_CALL_PARAM_SSA MLIL_CALL_OUTPUT_SSA MLIL_FREE_VAR_SLOT MLIL_FREE_VAR_SLOT_SSA
    MLIL_SUB MLIL_SX MLIL_TRAP MLIL_UNDEF MLIL_UNIMPL MLIL_UNIMPL_MEM MLIL_VAR
    MLIL_VAR_ALIASED MLIL_VAR_ALIASED_FIELD MLIL_VAR_FIELD MLIL_VAR_SPLIT
    MLIL_VAR_SSA MLIL_VAR_SSA_FIELD MLIL_VAR_SPLIT_SSA MLIL_VAR_PHI MLIL_MEM_PHI
    MLIL_TEST_BIT MLIL_ADD_OVERFLOW MLIL_XOR MLIL_ZX
    MLIL_FADD MLIL_FSUB MLIL_FMUL MLIL_FDIV MLIL_FSQRT MLIL_FNEG MLIL_FABS
    MLIL_FLOAT_TO_INT MLIL_INT_TO_FLOAT MLIL_FLOAT_CONV MLIL_ROUND_TO_INT
    MLIL_FLOOR MLIL_CEIL MLIL_FTRUNC MLIL_FCMP_E MLIL_FCMP_NE MLIL_FCMP_LT
    MLIL_FCMP_LE MLIL_FCMP_GE MLIL_FCMP_GT MLIL_FCMP_O MLIL_FCMP_UO
    MLIL_ASSERT_SSA MLIL_FORCE_VER_SSA
    """.split(),
)

FakeLowLevelILOperation = Enum(
    "LowLevelILOperation",
    """
    LLIL_ADD LLIL_AND LLIL_BOOL_TO_INT LLIL_CALL_SSA LLIL_CMP_E LLIL_CMP_NE
    LLIL_CMP_SGE LLIL_CMP_SGT LLIL_CMP_SLE LLIL_CMP_SLT LLIL_CMP_UGE
    LLIL_CMP_UGT LLIL_CMP_ULE LLIL_CMP_ULT LLIL_CONST LLIL_CONST_PTR
    LLIL_FLAG_SSA LLIL_IF LLIL_JUMP LLIL_JUMP_TO LLIL_LOAD LLIL_LOAD_SSA LLIL_LOW_PART
    LLIL_LSL LLIL_LSR LLIL_MEM_PHI LLIL_MUL LLIL_NEG LLIL_NOP LLIL_OR LLIL_REG
    LLIL_REG_PHI LLIL_REG_SSA
    LLIL_REG_SSA_PARTIAL LLIL_SET_REG_SSA LLIL_SET_REG_SSA_PARTIAL
    LLIL_RET LLIL_STORE_SSA LLIL_SUB LLIL_SX LLIL_TAILCALL LLIL_UNIMPL LLIL_XOR LLIL_ZX
    """.split(),
)

FakeRegisterValueType = Enum(
    "RegisterValueType",
    "ConstantValue ConstantPointerValue ImportedAddressValue StackFrameOffset".split(),
)
FakeSymbolType = Enum(
    "SymbolType",
    "DataSymbol ExternalSymbol FunctionSymbol ImportedFunctionSymbol "
    "LibraryFunctionSymbol SymbolicFunctionSymbol".split(),
)
FakeTypeClass = Enum(
    "TypeClass",
    "IntegerTypeClass PointerTypeClass VoidTypeClass".split(),
)
FakeVariableSourceType = Enum(
    "VariableSourceType",
    "StackVariableSourceType RegisterVariableSourceType FlagVariableSourceType".split(),
)


binaryninja = sys.modules.setdefault("binaryninja", types.SimpleNamespace())
for name, value in {
    "Activity": FakeActivity,
    "AnalysisContext": object,
    "FunctionType": FakeFunctionType,
    "ILSourceLocation": FakeILSourceLocation,
    "Logger": FakeLogger,
    "LowLevelILOperation": FakeLowLevelILOperation,
    "MediumLevelILOperation": FakeMediumLevelILOperation,
    "MediumLevelILJump": object,
    "MediumLevelILFunction": object,
    "RegisterValueType": FakeRegisterValueType,
    "SymbolType": FakeSymbolType,
    "Settings": FakeSettings,
    "SettingsScope": types.SimpleNamespace(SettingsResourceScope="resource"),
    "TypeClass": FakeTypeClass,
    "Type": FakeType,
    "VariableSourceType": FakeVariableSourceType,
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
