"""Complete definition-graph construction and PHI edge witnesses."""

from __future__ import annotations

from collections import deque

from ..semantics import Inconclusive
from ._values_bnil import (
    CONTROLLED_LOADS,
    NON_SSA_VARIABLES,
    PARTIAL_SSA_VARIABLES,
    PHIS,
    SSA_FIELD_VARIABLES,
    SSA_VARIABLES,
    controlled_load_value,
    direct_operands,
    is_expression,
    operation_name,
)
from ._values_contracts import DefinitionGraph, Handled, NotHandled
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
    def __init__(self, il, budget, policy):
        self.il = il
        self.budget = budget
        self.policy = policy
        self.failure = None
        self.nodes = {}
        self.children = {}
        self.edges = set()
        self.cycles = set()
        self.phis = {}
        self.joins = {}
        self.ssa_definitions = {}
        self.non_ssa_definition_sets = {}
        self.controlled_load_values = {}

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
        if operation in CONTROLLED_LOADS and (
            controlled_load_value(expression) is not None
            or self._policy_load_values(expression)
        ):
            return ()
        if operation in SSA_VARIABLES:
            definition = self.ssa_definition(getattr(expression, "src", None))
            if definition is None:
                self._reject("required SSA definition is unavailable")
                return ()
            return (definition,)
        if operation in SSA_FIELD_VARIABLES:
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

    def _policy_load_values(self, expression):
        resolver = getattr(self.policy, "resolve_load", None)
        if not callable(resolver):
            return False
        try:
            result = resolver(expression)
        except Exception:  # noqa: BLE001 - external pure-policy boundary.
            self._reject("value policy load resolver raised an exception")
            return True
        if type(result) is NotHandled:
            return False
        if type(result) is Inconclusive:
            self._reject(result.reason)
            return True
        if type(result) is not Handled or not result.values:
            self._reject("value policy load resolver returned an invalid result")
            return True
        self.controlled_load_values[expression_key(expression)] = result.values
        return True

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
        candidates_by_edge = {}
        source_positions = set()
        for position, operand in enumerate(sources):
            if is_expression(operand):
                self._reject(
                    "phi operands cannot be uniquely matched to incoming CFG edges"
                )
                return None
            definition = self.ssa_definition(operand)
            edges = self._edges_for_definition(operand, definition, by_source)
            if not edges:
                self._reject(
                    "phi operands cannot be uniquely matched to incoming CFG edges"
                )
                return None
            source_positions.add(position)
            for edge in edges:
                candidates_by_edge.setdefault(entity_key(edge), []).append(
                    (position, definition)
                )
        assigned = {}
        for edge in incoming:
            edge_key = entity_key(edge)
            candidates = candidates_by_edge.get(edge_key, ())
            if len(candidates) != 1:
                self._reject(
                    "phi operands cannot be uniquely matched to incoming CFG edges"
                )
                return None
            assigned[edge_key] = (edge, candidates[0])
        if len(assigned) != len(incoming) or {
            position for _edge, (position, _definition) in assigned.values()
        } != source_positions:
            self._reject(
                "phi operands cannot be uniquely matched to incoming CFG edges"
            )
            return None
        join_id = self.joins.setdefault(entity_key(join), len(self.joins) + 1)
        mapping = _PhiMapping(
            join_id,
            tuple(
                (edge_key, edge, definition)
                for edge_key, (edge, (_position, definition)) in assigned.items()
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

    def _edges_for_definition(self, operand, definition, by_source):
        candidates = {}
        direct = self._edge_for_direct_definition(definition, by_source)
        if direct is not None:
            candidates[entity_key(direct)] = direct
        for edge in by_source.values():
            if self._definition_reaches_block(operand, definition, edge.source):
                candidates[entity_key(edge)] = edge
        return tuple(candidates.values())

    def _definition_reaches_block(self, operand, definition, target):
        origin = getattr(definition, "il_basic_block", None)
        position = self._instruction_index(definition)
        if origin is None or target is None or position is None:
            return False
        pending = [(origin, position)]
        seen = set()
        while pending:
            block, after = pending.pop()
            key = (entity_key(block), after)
            if key in seen:
                continue
            seen.add(key)
            if not self._version_reaches_block_exit(block, operand, after):
                continue
            if same_entity(block, target):
                return True
            try:
                outgoing = tuple(getattr(block, "outgoing_edges", ()) or ())
            except Exception:  # noqa: BLE001 - Binary Ninja CFG boundary.
                return False
            for edge in outgoing:
                source = getattr(edge, "source", None)
                successor = getattr(edge, "target", None)
                if (
                    source is None
                    or successor is None
                    or not same_entity(source, block)
                ):
                    return False
                pending.append((successor, None))
        return False

    def _version_reaches_block_exit(self, block, operand, after):
        try:
            instructions = tuple(block)
        except Exception:  # noqa: BLE001 - Binary Ninja basic-block boundary.
            return False
        for instruction in instructions:
            position = self._instruction_index(instruction)
            if after is not None:
                if position is None:
                    return False
                if position <= after:
                    continue
            defines = self._defines_other_ssa_storage(instruction, operand)
            if defines is None:
                return False
            if defines:
                return False
        return True

    @staticmethod
    def _instruction_index(instruction):
        index = getattr(instruction, "instr_index", None)
        return index if type(index) is int and index >= 0 else None

    def _defines_other_ssa_storage(self, instruction, operand):
        operands = getattr(instruction, "detailed_operands", None)
        if operands is None:
            return None
        try:
            entries = tuple(operands)
        except Exception:  # noqa: BLE001 - Binary Ninja operand boundary.
            return None
        for entry in entries:
            if type(entry) not in (tuple, list) or len(entry) != 3:
                return None
            _name, value, _kind = entry
            for variable in self._ssa_variables(value):
                if not self._same_ssa_storage(variable, operand):
                    continue
                if not same_entity(self.ssa_definition(variable), instruction):
                    continue
                if variable_key(variable) != variable_key(operand):
                    return True
        return False

    @staticmethod
    def _ssa_variables(value):
        version = getattr(value, "version", None)
        if type(version) is int and any(
            getattr(value, attribute, None) is not None for attribute in ("reg", "var")
        ):
            return (value,)
        if type(value) not in (tuple, list):
            return ()
        variables = []
        for item in value:
            variables.extend(_GraphBuilder._ssa_variables(item))
        return tuple(variables)

    @staticmethod
    def _same_ssa_storage(left, right):
        for attribute in ("reg", "var"):
            left_base = getattr(left, attribute, None)
            right_base = getattr(right, attribute, None)
            if left_base is not None and right_base is not None:
                return same_entity(left_base, right_base)
        return False
