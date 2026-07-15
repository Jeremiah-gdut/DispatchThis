"""Public result and policy contracts for complete value evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..semantics import Inconclusive, ProviderContractError


@dataclass(frozen=True, slots=True)
class AnalysisBudget:
    """Caller-owned limits for complete definition-graph traversal."""

    node_limit: int
    edge_limit: int

    def __post_init__(self) -> None:
        if type(self.node_limit) is not int or self.node_limit <= 0:
            raise ProviderContractError("node_limit must be a positive integer")
        if type(self.edge_limit) is not int or self.edge_limit <= 0:
            raise ProviderContractError("edge_limit must be a positive integer")


@dataclass(frozen=True, slots=True)
class Handled:
    """A pure ValuePolicy completely evaluated its current expression."""

    values: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.values) is not tuple or any(
            type(value) is not int for value in self.values
        ):
            raise ProviderContractError("handled values must be an integer tuple")


@dataclass(frozen=True, slots=True)
class NotHandled:
    """A ValuePolicy deliberately leaves the current operation to the core."""


@dataclass(frozen=True, slots=True)
class DefinitionGraph:
    """The current complete definition graph, represented by ephemeral node IDs."""

    nodes: tuple[int, ...]
    leaves: tuple[int, ...]
    edges: tuple[tuple[int, int], ...]
    cycles: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class PathSource:
    """Exact current CFG edges supporting one retained value."""

    edges: tuple


@dataclass(frozen=True, slots=True)
class ValueCase:
    """One de-duplicated concrete value and every current path source for it."""

    value: int
    sources: tuple[PathSource, ...]


@dataclass(frozen=True, slots=True)
class CompleteValues:
    """All concrete values and current-only proof evidence for one expression."""

    values: tuple[int, ...]
    cases: tuple[ValueCase, ...]
    definition_graph: DefinitionGraph


class ValuePolicy(Protocol):
    """Pure extension point for sample-specific operations and controlled loads."""

    def __call__(
        self, expression, operands: tuple[tuple[int, ...], ...]
    ) -> Handled | NotHandled | Inconclusive: ...
