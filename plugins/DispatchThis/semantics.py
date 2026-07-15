"""Public, read-only contract for external DispatchThis sample providers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeAlias, TypeVar

if TYPE_CHECKING:
    from binaryninja import (
        BasicBlock,
        BasicBlockEdge,
        BinaryView,
        Function,
        LowLevelILFunction,
        LowLevelILInstruction,
        MediumLevelILFunction,
        MediumLevelILInstruction,
        Type,
    )


CORE_API_VERSION = 3

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderContractError(ValueError):
    """A provider supplied a value outside the public contract."""

    detail: str

    def __str__(self) -> str:
        return self.detail


def _require_address(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ProviderContractError(f"{name} must be a non-negative integer")


def _require_targets(targets: tuple[int, ...]) -> None:
    if type(targets) is not tuple or not targets:
        raise ProviderContractError("targets must be a non-empty tuple")
    if any(type(target) is not int or target < 0 for target in targets):
        raise ProviderContractError("targets must contain non-negative integers")
    if tuple(sorted(set(targets))) != targets:
        raise ProviderContractError("targets must be sorted and de-duplicated")


@dataclass(frozen=True, slots=True)
class CompleteBatch(Generic[T]):
    """A slot scanned its complete current frontier and found these facts."""

    facts: tuple[T, ...]

    def __post_init__(self) -> None:
        if type(self.facts) is not tuple:
            raise ProviderContractError("facts must be a tuple")


@dataclass(frozen=True, slots=True)
class Inconclusive:
    """A slot could not complete its proof for the current frontier."""

    reason: str

    def __post_init__(self) -> None:
        if type(self.reason) is not str or not self.reason:
            raise ProviderContractError("inconclusive reason must be non-empty text")


@dataclass(frozen=True, slots=True)
class BranchTargetQuery:
    """Read-only inputs for one indirect-branch target scan."""

    view: BinaryView
    function: Function
    llil: LowLevelILFunction


@dataclass(frozen=True, slots=True)
class CallTargetQuery:
    """Read-only inputs for one indirect-call target scan."""

    view: BinaryView
    function: Function
    mlil: MediumLevelILFunction


@dataclass(frozen=True, slots=True)
class GlobalDataQuery:
    """Read-only inputs for one global-data semantic scan."""

    view: BinaryView
    function: Function
    mlil: MediumLevelILFunction


@dataclass(frozen=True, slots=True)
class CorrelatedStoreQuery:
    """Read-only inputs for one path-correlated STORE scan."""

    view: BinaryView
    function: Function
    mlil: MediumLevelILFunction


@dataclass(frozen=True, slots=True)
class StringRecoveryQuery:
    """Read-only inputs for one string-recovery scan."""

    view: BinaryView
    function: Function
    mlil: MediumLevelILFunction
    deflattened_function_starts: frozenset[int]

    def __post_init__(self) -> None:
        if type(self.deflattened_function_starts) is not frozenset or any(
            type(start) is not int or start < 0
            for start in self.deflattened_function_starts
        ):
            raise ProviderContractError(
                "deflattened_function_starts must be a frozenset of non-negative integers"
            )


@dataclass(frozen=True, slots=True)
class DeflattenQuery:
    """Read-only inputs for one dispatcher-redirection scan."""

    view: BinaryView
    function: Function
    mlil: MediumLevelILFunction


@dataclass(frozen=True, slots=True)
class BranchTargetFact:
    """A complete target set witnessed by a current LLIL indirect jump."""

    jump_il: LowLevelILInstruction
    targets: tuple[int, ...]
    condition: LowLevelILInstruction | None = None
    true_target: int | None = None
    false_target: int | None = None

    def __post_init__(self) -> None:
        if self.jump_il is None:
            raise ProviderContractError("jump_il is required")
        _require_targets(self.targets)
        if self.condition is None:
            if self.true_target is not None or self.false_target is not None:
                raise ProviderContractError("unconditional facts cannot name branch arms")
            return
        if self.true_target is None or self.false_target is None:
            raise ProviderContractError("conditional facts require both branch arms")
        _require_address("true_target", self.true_target)
        _require_address("false_target", self.false_target)
        if self.true_target == self.false_target:
            raise ProviderContractError("conditional facts require distinct branch arms")
        if self.targets != tuple(sorted((self.true_target, self.false_target))):
            raise ProviderContractError("conditional targets must match both branch arms")


@dataclass(frozen=True, slots=True)
class CallTargetFact:
    """A complete callee set witnessed by a current MLIL indirect call."""

    call_il: MediumLevelILInstruction
    targets: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.call_il is None:
            raise ProviderContractError("call_il is required")
        _require_targets(self.targets)


@dataclass(frozen=True, slots=True)
class GlobalDataFact:
    """A provider-proven type for one exact global-data slot."""

    slot_addr: int
    data_type: Type

    def __post_init__(self) -> None:
        _require_address("slot_addr", self.slot_addr)
        if self.data_type is None:
            raise ProviderContractError("data_type is required")


@dataclass(frozen=True, slots=True)
class CorrelatedStoreArm:
    """One query-MLIL predecessor store and its exact CFG/value evidence."""

    predecessor: BasicBlock
    incoming_edge: BasicBlockEdge
    goto_il: MediumLevelILInstruction
    dest_expr: MediumLevelILInstruction
    dest_addr: int
    src_expr: MediumLevelILInstruction
    src_addr: int

    def __post_init__(self) -> None:
        if self.predecessor is None:
            raise ProviderContractError("predecessor is required")
        if self.incoming_edge is None:
            raise ProviderContractError("incoming_edge is required")
        if self.goto_il is None:
            raise ProviderContractError("goto_il is required")
        if self.dest_expr is None:
            raise ProviderContractError("dest_expr is required")
        _require_address("dest_addr", self.dest_addr)
        if self.src_expr is None:
            raise ProviderContractError("src_expr is required")
        _require_address("src_addr", self.src_addr)


@dataclass(frozen=True, slots=True)
class CorrelatedStorePlan:
    """A current non-SSA MLIL join STORE and its two owned predecessor arms."""

    store_il: MediumLevelILInstruction
    join_block: BasicBlock
    size: int
    arms: tuple[CorrelatedStoreArm, ...]

    def __post_init__(self) -> None:
        if self.store_il is None:
            raise ProviderContractError("store_il is required")
        if self.join_block is None:
            raise ProviderContractError("join_block is required")
        if type(self.size) is not int or self.size <= 0:
            raise ProviderContractError("size must be a positive integer")
        if (
            type(self.arms) is not tuple
            or len(self.arms) != 2
            or any(type(arm) is not CorrelatedStoreArm for arm in self.arms)
        ):
            raise ProviderContractError("correlated store plans require two arms")


@dataclass(frozen=True, slots=True)
class StringRecoveryFact:
    """A decrypted string and the current callsite it explains."""

    call_addr: int
    source_addr: int
    destination_addr: int
    plaintext: bytes

    def __post_init__(self) -> None:
        _require_address("call_addr", self.call_addr)
        _require_address("source_addr", self.source_addr)
        _require_address("destination_addr", self.destination_addr)
        if type(self.plaintext) is not bytes:
            raise ProviderContractError("plaintext must be bytes")


@dataclass(frozen=True, slots=True)
class DeflattenRedirection:
    """One current MLIL exit and its recovered original-block successor."""

    exit_il: MediumLevelILInstruction
    target_block_start: int

    def __post_init__(self) -> None:
        if self.exit_il is None:
            raise ProviderContractError("exit_il is required")
        _require_address("target_block_start", self.target_block_start)


@dataclass(frozen=True, slots=True)
class DeflattenPlan:
    """A complete, declarative batch of dispatcher exit redirections."""

    redirections: tuple[DeflattenRedirection, ...]
    obsolete_state_writes: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if type(self.redirections) is not tuple or not self.redirections:
            raise ProviderContractError("redirections must be a non-empty tuple")
        if type(self.obsolete_state_writes) is not frozenset or any(
            type(index) is not int or index < 0 for index in self.obsolete_state_writes
        ):
            raise ProviderContractError("obsolete_state_writes must be non-negative instruction indexes")


BranchSlot: TypeAlias = Callable[[BranchTargetQuery], CompleteBatch[BranchTargetFact] | Inconclusive]
CallSlot: TypeAlias = Callable[[CallTargetQuery], CompleteBatch[CallTargetFact] | Inconclusive]
GlobalDataSlot: TypeAlias = Callable[[GlobalDataQuery], CompleteBatch[GlobalDataFact] | Inconclusive]
CorrelatedStoreSlot: TypeAlias = Callable[[CorrelatedStoreQuery], CompleteBatch[CorrelatedStorePlan] | Inconclusive]
StringRecoverySlot: TypeAlias = Callable[[StringRecoveryQuery], CompleteBatch[StringRecoveryFact] | Inconclusive]
DeflattenSlot: TypeAlias = Callable[[DeflattenQuery], CompleteBatch[DeflattenPlan] | Inconclusive]


@dataclass(frozen=True, slots=True)
class SampleSemantics:
    """The sole registration object for one external sample's pure semantics."""

    provider_id: str
    name: str
    api_version: int
    branch_targets: BranchSlot | None = None
    call_targets: CallSlot | None = None
    global_data: GlobalDataSlot | None = None
    correlated_stores: CorrelatedStoreSlot | None = None
    string_recovery: StringRecoverySlot | None = None
    deflatten: DeflattenSlot | None = None

    def __post_init__(self) -> None:
        if type(self.provider_id) is not str or not self.provider_id:
            raise ProviderContractError("provider_id must be non-empty text")
        if type(self.name) is not str or not self.name:
            raise ProviderContractError("name must be non-empty text")
        if type(self.api_version) is not int or self.api_version < 0:
            raise ProviderContractError("api_version must be a non-negative integer")


SLOT_NAMES = (
    "branch_targets",
    "call_targets",
    "global_data",
    "correlated_stores",
    "string_recovery",
    "deflatten",
)


__all__ = (
    "CORE_API_VERSION",
    "BranchTargetQuery",
    "CallTargetQuery",
    "GlobalDataQuery",
    "CorrelatedStoreQuery",
    "StringRecoveryQuery",
    "DeflattenQuery",
    "BranchTargetFact",
    "CallTargetFact",
    "GlobalDataFact",
    "CorrelatedStorePlan",
    "CorrelatedStoreArm",
    "StringRecoveryFact",
    "DeflattenPlan",
    "DeflattenRedirection",
    "CompleteBatch",
    "Inconclusive",
    "ProviderContractError",
    "SampleSemantics",
)
