"""Stable public API for complete, path-correlated BNIL value evaluation."""

from __future__ import annotations

from ..semantics import Inconclusive
from ._values_contracts import (
    AnalysisBudget,
    CompleteValues,
    DefinitionGraph,
    Handled,
    NotHandled,
    PathSource,
    ValueCase,
    ValuePolicy,
)
from ._values_evaluator import _Evaluator
from ._values_graph import _GraphBuilder
from ._values_paths import complete_values


def evaluate_values(
    _view, il, expression, budget: AnalysisBudget, policy: ValuePolicy | None = None
):
    """Return every complete concrete value, or Inconclusive without a subset."""

    builder = _GraphBuilder(il, budget)
    graph = builder.build(expression)
    if builder.failure is not None:
        return builder.failure
    if graph.cycles:
        return Inconclusive("definition graph contains a cycle")
    evaluator = _Evaluator(builder, policy)
    states = evaluator.evaluate(expression)
    if evaluator.failure is not None:
        return evaluator.failure
    if not states:
        return Inconclusive("no CFG-compatible path evaluates expression")
    return complete_values(states, builder.phis, graph)


__all__ = (
    "AnalysisBudget",
    "CompleteValues",
    "DefinitionGraph",
    "Handled",
    "NotHandled",
    "PathSource",
    "ValueCase",
    "ValuePolicy",
    "evaluate_values",
)
