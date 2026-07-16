"""Transient CFG-edge correlation and complete-value evidence."""

from __future__ import annotations

from collections import deque

from ._values_bnil import COMPARISONS, CONSTANTS, SSA_VARIABLES, direct_operands, operation_name
from ._values_contracts import CompleteValues, PathSource, ValueCase
from ._values_identity import entity_key, same_entity, variable_key


class _PathCorrelation:
    def __init__(self, phis, il):
        self.phis = phis
        self.il = il
        self.guards = {}

    def phi_jobs(self, mapping, selections):
        selected = self._selection(mapping, selections)
        if selected is not None:
            return ((selected[2], selections),)
        jobs = []
        for candidate in mapping.cases:
            if not self._compatible(mapping, candidate, selections):
                continue
            jobs.append(
                (
                    candidate[2],
                    self._add_selection(selections, mapping.join_id, candidate[0]),
                )
            )
        return tuple(jobs)

    def _selection(self, mapping, selections):
        for join_id, edge_key in selections:
            if join_id == mapping.join_id:
                return mapping.by_edge.get(edge_key)
        return None

    def _add_selection(self, selections, join_id, edge_key):
        return tuple(
            sorted((*selections, (join_id, edge_key)), key=lambda item: item[0])
        )

    def _compatible(self, mapping, candidate, selections):
        candidate_guard = self._edge_guard(candidate[1])
        for join_id, edge_key in selections:
            existing = self._mapping(join_id).by_edge[edge_key]
            if join_id == mapping.join_id:
                return edge_key == candidate[0]
            existing_edge = existing[1]
            candidate_edge = candidate[1]
            existing_guard = self._edge_guard(existing_edge)
            if (
                candidate_guard is not None
                and existing_guard is not None
                and candidate_guard[0] == existing_guard[0]
                and candidate_guard[1] != existing_guard[1]
            ):
                return False
            if not (
                self._reachable(
                    getattr(existing_edge, "target", None),
                    getattr(candidate_edge, "source", None),
                )
                or self._reachable(
                    getattr(candidate_edge, "target", None),
                    getattr(existing_edge, "source", None),
                )
            ):
                return False
        return True

    def _edge_guard(self, edge):
        key = entity_key(edge)
        if key not in self.guards:
            self.guards[key] = self._discover_guard(edge)
        return self.guards[key]

    def _discover_guard(self, phi_edge):
        arm = getattr(phi_edge, "source", None)
        incoming = tuple(getattr(arm, "incoming_edges", ()) or ())
        if len(incoming) != 1:
            return None
        branch_edge = incoming[0]
        if not same_entity(getattr(branch_edge, "target", None), arm):
            return None
        outcome = getattr(getattr(branch_edge, "type", None), "name", None)
        if outcome not in ("TrueBranch", "FalseBranch"):
            return None
        branch = getattr(branch_edge, "source", None)
        if branch is None:
            return None
        instructions = tuple(branch)
        if not instructions:
            return None
        terminal = instructions[-1]
        if operation_name(terminal) != "LLIL_IF":
            return None
        condition = getattr(terminal, "condition", None)
        if operation_name(condition) != "LLIL_FLAG_SSA":
            return None
        getter = getattr(self.il, "get_ssa_flag_definition", None)
        if not callable(getter):
            return None
        definition = getter(getattr(condition, "src", None))
        if operation_name(definition) != "LLIL_SET_FLAG_SSA":
            return None
        comparison = getattr(definition, "src", None)
        operation = operation_name(comparison)
        operands = direct_operands(comparison)
        if operation not in COMPARISONS or len(operands) != 2:
            return None
        left = self._guard_operand(operands[0])
        right = self._guard_operand(operands[1])
        if left is None or right is None:
            return None
        return ((operation, left, right), outcome)

    @staticmethod
    def _guard_operand(expression):
        operation = operation_name(expression)
        if operation in SSA_VARIABLES:
            variable = getattr(expression, "src", None)
            return None if variable is None else ("ssa", variable_key(variable))
        if operation not in CONSTANTS:
            return None
        size = getattr(expression, "size", None)
        value = getattr(expression, "constant", None)
        if type(size) is not int or size <= 0 or type(value) is not int:
            return None
        return ("constant", size, value)

    def _mapping(self, join_id):
        for mapping in self.phis.values():
            if mapping.join_id == join_id:
                return mapping
        raise LookupError("missing PHI mapping")

    def _reachable(self, start, goal):
        if start is None or goal is None:
            return False
        if same_entity(start, goal):
            return True
        queue = deque((start,))
        seen = {entity_key(start)}
        while queue:
            block = queue.popleft()
            for edge in tuple(getattr(block, "outgoing_edges", ()) or ()):
                target = getattr(edge, "target", None)
                if target is None:
                    continue
                if same_entity(target, goal):
                    return True
                key = entity_key(target)
                if key not in seen:
                    seen.add(key)
                    queue.append(target)
        return False


def complete_values(states, phis, graph):
    """Deduplicate values while retaining every selected raw CFG-edge source."""

    mappings = {mapping.join_id: mapping for mapping in phis.values()}
    values = {}
    for value, selections in states:
        sources = values.setdefault(value, {})
        if selections in sources:
            continue
        edges = tuple(
            mappings[join_id].by_edge[edge_key][1] for join_id, edge_key in selections
        )
        sources[selections] = PathSource(edges)
    cases = tuple(
        ValueCase(value, tuple(source.values()))
        for value, source in sorted(values.items())
    )
    return CompleteValues(
        tuple(value for value, _sources in sorted(values.items())), cases, graph
    )
