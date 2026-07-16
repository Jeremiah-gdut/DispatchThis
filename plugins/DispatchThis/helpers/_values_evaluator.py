"""Explicit-stack complete-value evaluator over a prebuilt definition graph."""

from __future__ import annotations

from ..semantics import Inconclusive
from ._values_bnil import (
    CONSTANTS,
    CONTROLLED_LOADS,
    KNOWN_BNIL_OPERATIONS,
    NON_SSA_VARIABLES,
    PARTIAL_SSA_VARIABLES,
    PHIS,
    SETS,
    SSA_FIELD_VARIABLES,
    SSA_VARIABLES,
    direct_operands,
    controlled_load_value,
    mask_for,
    operation_name,
)
from ._values_contracts import Handled, NotHandled
from ._values_identity import expression_key
from ._values_operations import standard_value
from ._values_paths import _PathCorrelation


class _Evaluator:
    def __init__(self, builder, policy):
        self.builder = builder
        self.policy = policy
        self.paths = _PathCorrelation(builder.phis, builder.il)
        self.failure = None
        self.tasks = []
        self.results = {}
        self.next_token = 1

    def evaluate(self, expression):
        root = self._new_token()
        self.tasks.append(("evaluate", root, expression, (), frozenset()))
        while self.tasks and self.failure is None:
            task = self.tasks.pop()
            kind = task[0]
            if kind == "evaluate":
                self._evaluate_task(*task[1:])
            elif kind == "mask":
                self._mask_task(*task[1:])
            elif kind == "field":
                self._field_task(*task[1:])
            elif kind == "collect-mask":
                self._collect_mask_task(*task[1:])
            elif kind == "operands":
                self._operands_task(*task[1:])
            elif kind == "operand-result":
                self._operand_result_task(*task[1:])
            else:
                raise AssertionError(f"unknown evaluation task {kind}")
        return None if self.failure is not None else self.results[root]

    def _reject(self, reason):
        if self.failure is None:
            self.failure = Inconclusive(reason)

    def _new_token(self):
        token = self.next_token
        self.next_token += 1
        return token

    def _evaluate_task(self, token, expression, selections, active):
        key = expression_key(expression)
        if key in active:
            self._reject("definition graph contains a cycle")
            return
        operation = operation_name(expression)
        next_active = active | {key}
        resolved_load_values = self.builder.controlled_load_values.get(key)
        load_value = (
            None if resolved_load_values is not None else controlled_load_value(expression)
        )
        if resolved_load_values is not None:
            mask = mask_for(expression)
            if mask is None:
                self._reject("expression width is unavailable")
                return
            self.results[token] = [
                (value & mask, selections) for value in resolved_load_values
            ]
        elif load_value is not None:
            mask = mask_for(expression)
            if mask is None:
                self._reject("expression width is unavailable")
                return
            self.results[token] = [(load_value & mask, selections)]
        elif operation in CONSTANTS:
            value = getattr(expression, "constant", None)
            mask = mask_for(expression)
            if type(value) is not int or mask is None:
                self._reject("constant value or width is unavailable")
                return
            self.results[token] = [(value & mask, selections)]
        elif operation in SSA_VARIABLES:
            definition = self.builder.ssa_definition(getattr(expression, "src", None))
            self._schedule_mask(token, expression, definition, selections, next_active)
        elif operation in SSA_FIELD_VARIABLES:
            definition = self.builder.ssa_definition(getattr(expression, "src", None))
            self._schedule_field(token, expression, definition, selections, next_active)
        elif operation in PARTIAL_SSA_VARIABLES:
            definition = self.builder.ssa_definition(
                getattr(expression, "full_reg", None)
            )
            self._schedule_mask(token, expression, definition, selections, next_active)
        elif operation in NON_SSA_VARIABLES:
            definitions = self.builder.non_ssa_definitions_for(
                getattr(expression, "src", None)
            )
            if not definitions:
                self._reject("required variable definitions are unavailable")
                return
            self._schedule_many_masked(
                token,
                expression,
                tuple(
                    (definition, selections, next_active) for definition in definitions
                ),
            )
        elif operation in PHIS:
            self._schedule_phi(token, expression, selections, next_active)
        elif operation in SETS:
            self._schedule_mask(
                token,
                expression,
                getattr(expression, "src", None),
                selections,
                next_active,
            )
        elif (
            operation is None
            or operation.endswith("_UNIMPL")
            or operation.endswith("_UNIMPL_MEM")
        ):
            self._reject("unsupported unmodeled operation")
        else:
            self.results[token] = []
            self.tasks.append(
                (
                    "operands",
                    token,
                    expression,
                    direct_operands(expression),
                    0,
                    (),
                    selections,
                    next_active,
                )
            )

    def _schedule_mask(self, token, expression, definition, selections, active):
        if definition is None:
            self._reject("required SSA definition is unavailable")
            return
        child = self._new_token()
        self.tasks.append(("mask", token, child, expression))
        self.tasks.append(("evaluate", child, definition, selections, active))

    def _schedule_field(self, token, expression, definition, selections, active):
        if definition is None:
            self._reject("required SSA definition is unavailable")
            return
        offset = getattr(expression, "offset", None)
        if type(offset) is not int or offset < 0:
            self._reject("SSA field offset is unavailable")
            return
        child = self._new_token()
        self.tasks.append(("field", token, child, expression, offset))
        self.tasks.append(("evaluate", child, definition, selections, active))

    def _schedule_many_masked(self, token, expression, jobs):
        children = tuple(self._new_token() for _job in jobs)
        self.tasks.append(("collect-mask", token, children, expression))
        for child, (definition, selections, active) in zip(children, jobs):
            self.tasks.append(("evaluate", child, definition, selections, active))

    def _schedule_phi(self, token, expression, selections, active):
        mapping = self.builder.phis.get(expression_key(expression))
        if mapping is None:
            self._reject(
                "phi operands cannot be uniquely matched to incoming CFG edges"
            )
            return
        jobs = tuple(
            (definition, next_selections, active)
            for definition, next_selections in self.paths.phi_jobs(mapping, selections)
        )
        self._schedule_many_masked(token, expression, jobs)

    def _mask_task(self, token, child, expression):
        self.results[token] = self._masked(self.results[child], expression)

    def _field_task(self, token, child, expression, offset):
        shifted = tuple(
            (value >> (offset * 8), selections)
            for value, selections in self.results[child]
        )
        self.results[token] = self._masked(shifted, expression)

    def _collect_mask_task(self, token, children, expression):
        states = []
        for child in children:
            states.extend(self.results[child])
        self.results[token] = self._masked(states, expression)

    def _operands_task(
        self, token, expression, operands, index, values, selections, active
    ):
        if index == len(operands):
            self._finish_operation(token, expression, values, selections)
            return
        child = self._new_token()
        self.tasks.append(
            (
                "operand-result",
                token,
                expression,
                operands,
                index,
                values,
                active,
                child,
            )
        )
        self.tasks.append(("evaluate", child, operands[index], selections, active))

    def _operand_result_task(
        self, token, expression, operands, index, values, active, child
    ):
        for value, selections in self.results[child]:
            self.tasks.append(
                (
                    "operands",
                    token,
                    expression,
                    operands,
                    index + 1,
                    values + (value,),
                    selections,
                    active,
                )
            )

    def _finish_operation(self, token, expression, operands, selections):
        operation = operation_name(expression)
        standard = standard_value(operation, expression, operands)
        if type(standard) is Inconclusive:
            self.failure = standard
        elif standard is not None:
            self.results[token].extend(
                self._masked([(standard, selections)], expression)
            )
        elif operation in KNOWN_BNIL_OPERATIONS and operation not in CONTROLLED_LOADS:
            # Policies extend sample semantics, not missing core BNIL semantics.
            self._reject(f"unsupported standard operation {operation}")
        elif self.policy is None:
            self._reject(f"unsupported operation {operation}")
        else:
            self._finish_policy(token, expression, operands, selections, operation)

    def _finish_policy(self, token, expression, operands, selections, operation):
        try:
            result = self.policy(expression, tuple((value,) for value in operands))
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — external pure-policy boundary.
            self._reject("value policy raised an exception")
            return
        if type(result) is Inconclusive:
            self.failure = result
        elif type(result) is NotHandled:
            self._reject(f"unsupported operation {operation}")
        elif type(result) is not Handled:
            self._reject("value policy returned an invalid result")
        else:
            self.results[token].extend(
                self._masked(
                    [(value, selections) for value in result.values], expression
                )
            )

    def _masked(self, states, expression):
        if states is None:
            return None
        mask = mask_for(expression)
        if mask is None:
            if operation_name(expression) in PHIS:
                # BN reports zero width for some register PHIs. Their typed
                # consumers still apply the correct mask.
                return states
            self._reject("expression width is unavailable")
            return None
        return [(value & mask, selections) for value, selections in states]
