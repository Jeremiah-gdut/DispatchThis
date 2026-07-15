"""Complete definition-graph construction and PHI edge witnesses."""

from __future__ import annotations

from collections import deque

from ..semantics import Inconclusive
from ._values_bnil import (
    NON_SSA_VARIABLES,
    PARTIAL_SSA_VARIABLES,
    PHIS,
    SSA_VARIABLES,
    direct_operands,
    is_expression,
    operation_name,
)
from ._values_contracts import DefinitionGraph
from ._values_identity import (
    entity_key,
    expression_key,
    non_ssa_definitions,
    same_entity,
    ssa_definition,
    variable_key,
)


class _PhiMapping:
    def __init__(self, join_id, cases):
        self.join_id = join_id
        self.cases = cases
        self.by_edge = {case[0]: case for case in cases}


class _GraphBuilder:
    def __init__(self, il, budget):
        self.il = il
        self.budget = budget
        self.failure = None
        self.nodes = {}
        self.children = {}
        self.edges = set()
        self.cycles = set()
        self.phis = {}
        self.joins = {}
        self.ssa_definitions = {}
        self.non_ssa_definition_sets = {}

    def build(self, expression):
        self._visit(expression)
        if self.failure is not None:
            return None
        self._record_cycles()
        leaves = tuple(
            sorted(node for node in self.nodes.values() if not self.children[node])
        )
        return DefinitionGraph(
            tuple(range(1, len(self.nodes) + 1)),
            leaves,
            tuple(sorted(self.edges)),
            tuple(sorted(self.cycles)),
        )

    def _reject(self, reason):
        if self.failure is None:
            self.failure = Inconclusive(reason)

    def _node(self, expression):
        if not is_expression(expression):
            self._reject("definition graph contains a non-expression node")
            return None
        key = expression_key(expression)
        if key not in self.nodes:
            if len(self.nodes) >= self.budget.node_limit:
                self._reject("definition graph node budget exhausted")
                return None
            self.nodes[key] = len(self.nodes) + 1
            self.children[self.nodes[key]] = set()
        return self.nodes[key]

    def _edge(self, source, target):
        edge = (source, target)
        if edge not in self.edges:
            if len(self.edges) >= self.budget.edge_limit:
                self._reject("definition graph edge budget exhausted")
                return
            self.edges.add(edge)
        self.children[source].add(target)

    def _visit(self, expression):
        pending = deque((expression,))
        expanded = set()
        while pending:
            current = pending.popleft()
            source = self._node(current)
            if source is None or self.failure is not None:
                return
            key = expression_key(current)
            if key in expanded:
                continue
            expanded.add(key)
            for child in self._children(current):
                target = self._node(child)
                if target is None or self.failure is not None:
                    return
                self._edge(source, target)
                if self.failure is not None:
                    return
                if expression_key(child) not in expanded:
                    pending.append(child)

    def _record_cycles(self):
        state = {}
        for start in self.children:
            if state.get(start, 0) != 0:
                continue
            state[start] = 1
            stack = [(start, iter(sorted(self.children[start])))]
            while stack:
                source, targets = stack[-1]
                try:
                    target = next(targets)
                except StopIteration:
                    state[source] = 2
                    stack.pop()
                    continue
                target_state = state.get(target, 0)
                if target_state == 1:
                    self.cycles.add((source, target))
                elif target_state == 0:
                    state[target] = 1
                    stack.append((target, iter(sorted(self.children[target]))))

    def _children(self, expression):
        operation = operation_name(expression)
        if operation in SSA_VARIABLES:
            definition = self.ssa_definition(getattr(expression, "src", None))
            if definition is None:
                self._reject("required SSA definition is unavailable")
                return ()
            return (definition,)
        if operation in PARTIAL_SSA_VARIABLES:
            definition = self.ssa_definition(getattr(expression, "full_reg", None))
            if definition is None:
                self._reject("required SSA definition is unavailable")
                return ()
            return (definition,)
        if operation in NON_SSA_VARIABLES:
            definitions = self.non_ssa_definitions_for(getattr(expression, "src", None))
            if not definitions:
                self._reject("required variable definitions are unavailable")
            return definitions
        if operation in PHIS:
            mapping = self._phi_mapping(expression)
            return () if mapping is None else tuple(case[2] for case in mapping.cases)
        return direct_operands(expression)

    def ssa_definition(self, variable):
        key = variable_key(variable)
        if key not in self.ssa_definitions:
            self.ssa_definitions[key] = ssa_definition(self.il, variable)
        return self.ssa_definitions[key]

    def non_ssa_definitions_for(self, variable):
        key = variable_key(variable)
        if key not in self.non_ssa_definition_sets:
            self.non_ssa_definition_sets[key] = non_ssa_definitions(self.il, variable)
        return self.non_ssa_definition_sets[key]

    def _phi_mapping(self, expression):
        key = expression_key(expression)
        cached = self.phis.get(key)
        if cached is not None:
            return cached
        join = getattr(expression, "il_basic_block", None)
        incoming = tuple(getattr(join, "incoming_edges", ()) or ())
        sources = tuple(getattr(expression, "src", ()) or ())
        if join is None or not incoming or not sources:
            self._reject(
                "phi operands cannot be uniquely matched to incoming CFG edges"
            )
            return None
        by_source = self._incoming_edges_by_source(incoming, join)
        if by_source is None:
            return None
        assigned = {}
        for operand in sources:
            if is_expression(operand):
                self._reject(
                    "phi operands cannot be uniquely matched to incoming CFG edges"
                )
                return None
            definition = self.ssa_definition(operand)
            edge = self._edge_for_direct_definition(definition, by_source)
            if edge is None:
                self._reject(
                    "phi operands cannot be uniquely matched to incoming CFG edges"
                )
                return None
            edge_key = entity_key(edge)
            if edge_key in assigned:
                self._reject(
                    "phi operands cannot be uniquely matched to incoming CFG edges"
                )
                return None
            assigned[edge_key] = (edge, definition)
        if len(assigned) != len(incoming):
            self._reject(
                "phi operands cannot be uniquely matched to incoming CFG edges"
            )
            return None
        join_id = self.joins.setdefault(entity_key(join), len(self.joins) + 1)
        mapping = _PhiMapping(
            join_id,
            tuple(
                (edge_key, edge, definition)
                for edge_key, (edge, definition) in assigned.items()
            ),
        )
        self.phis[key] = mapping
        return mapping

    def _incoming_edges_by_source(self, incoming, join):
        by_source = {}
        for edge in incoming:
            source = getattr(edge, "source", None)
            target = getattr(edge, "target", None)
            key = entity_key(source)
            if (
                source is None
                or target is None
                or not same_entity(target, join)
                or key in by_source
            ):
                self._reject(
                    "phi operands cannot be uniquely matched to incoming CFG edges"
                )
                return None
            by_source[key] = edge
        return by_source

    def _edge_for_direct_definition(self, definition, by_source):
        source = getattr(definition, "il_basic_block", None)
        edge = by_source.get(entity_key(source))
        if definition is None or source is None or edge is None:
            return None
        if not same_entity(getattr(edge, "source", None), source):
            return None
        outgoing = tuple(getattr(source, "outgoing_edges", ()) or ())
        return (
            edge
            if sum(same_entity(candidate, edge) for candidate in outgoing) == 1
            else None
        )
