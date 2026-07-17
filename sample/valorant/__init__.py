"""Automatic semantic recovery for the audited Valorant ARM64 sample."""

from __future__ import annotations

from DispatchThis import (
    AnalysisBudget,
    BranchTargetFact,
    CallTargetFact,
    CompleteBatch,
    CompleteValues,
    GlobalDataFact,
    Handled,
    Inconclusive,
    NotHandled,
    SampleSemantics,
    StringRecoveryFact,
    evaluate_values,
    register_provider,
)
from DispatchThis.helpers import memory, mlil as mlil_helpers


_VALUE_BUDGET = AnalysisBudget(node_limit=4096, edge_limit=8192)
_BRANCH_OPERATIONS = frozenset(("LLIL_JUMP", "LLIL_JUMP_TO", "LLIL_TAILCALL"))
_CALL_OPERATIONS = frozenset(
    (
        "MLIL_CALL",
        "MLIL_CALL_SSA",
        "MLIL_CALL_UNTYPED",
        "MLIL_CALL_UNTYPED_SSA",
        "MLIL_TAILCALL",
        "MLIL_TAILCALL_SSA",
        "MLIL_TAILCALL_UNTYPED",
        "MLIL_TAILCALL_UNTYPED_SSA",
    )
)
_CONSTANT_OPERATIONS = frozenset(
    ("LLIL_CONST", "LLIL_CONST_PTR", "MLIL_CONST", "MLIL_CONST_PTR")
)
_STATIC_LOAD_OPERATIONS = frozenset(
    (
        "LLIL_LOAD",
        "LLIL_LOAD_SSA",
        "MLIL_LOAD",
        "MLIL_LOAD_SSA",
        "MLIL_LOAD_STRUCT",
        "MLIL_LOAD_STRUCT_SSA",
    )
)
_MLIL_LOAD_OPERATIONS = frozenset(
    (
        "MLIL_LOAD",
        "MLIL_LOAD_SSA",
        "MLIL_LOAD_STRUCT",
        "MLIL_LOAD_STRUCT_SSA",
    )
)
_MLIL_STORE_OPERATIONS = frozenset(
    (
        "MLIL_STORE",
        "MLIL_STORE_SSA",
        "MLIL_STORE_STRUCT",
        "MLIL_STORE_STRUCT_SSA",
    )
)
_OUTCOMES = frozenset(("TrueBranch", "FalseBranch"))
_SUPPORTED_LOAD_WIDTHS = frozenset((1, 2, 4, 8))
_STACK_LOAD_OPERATION = "LLIL_LOAD_SSA"
_STACK_STORE_OPERATION = "LLIL_STORE_SSA"
_MEMORY_PHI_OPERATION = "LLIL_MEM_PHI"
_CALL_OPERATION = "LLIL_CALL_SSA"
_SSA_REGISTER_OPERATION = "LLIL_REG_SSA"
_SET_REGISTER_OPERATION = "LLIL_SET_REG_SSA"
_STACK_ADDRESSES = frozenset(("LLIL_ADD", "LLIL_SUB"))
_STRING_BYTES_LIMIT = 512
_STRING_EXECUTION_LIMIT = 8192
_FIELD_VARIABLE_OPERATIONS = frozenset(("MLIL_VAR_FIELD", "MLIL_VAR_SSA_FIELD"))
_SET_FIELD_OPERATIONS = frozenset(("MLIL_SET_VAR_FIELD", "MLIL_SET_VAR_SSA_FIELD"))
_STRING_CAST_OPERATIONS = frozenset(("MLIL_ZX", "MLIL_SX", "MLIL_LOW_PART"))
_STRING_UNARY_OPERATIONS = frozenset(("MLIL_NEG", "MLIL_NOT", "MLIL_BOOL_TO_INT"))
_STRING_BINARY_OPERATIONS = frozenset(
    (
        "MLIL_ADD",
        "MLIL_SUB",
        "MLIL_MUL",
        "MLIL_AND",
        "MLIL_OR",
        "MLIL_XOR",
        "MLIL_LSL",
        "MLIL_LSR",
        "MLIL_ASR",
        "MLIL_DIVU",
        "MLIL_DIVS",
        "MLIL_MODU",
        "MLIL_MODS",
    )
)
_STRING_COMPARISON_OPERATIONS = frozenset(
    (
        "MLIL_CMP_E",
        "MLIL_CMP_NE",
        "MLIL_CMP_SLT",
        "MLIL_CMP_SLE",
        "MLIL_CMP_SGT",
        "MLIL_CMP_SGE",
        "MLIL_CMP_ULT",
        "MLIL_CMP_ULE",
        "MLIL_CMP_UGT",
        "MLIL_CMP_UGE",
    )
)


def _operation_name(value):
    name = getattr(getattr(value, "operation", None), "name", None)
    return name if type(name) is str else None


def _instructions(il):
    try:
        return tuple(getattr(il, "instructions", ()) or ())
    except Exception:  # noqa: BLE001 - Binary Ninja wrapper boundary.
        return ()


def _block_instructions(block):
    try:
        return tuple(block)
    except Exception:  # noqa: BLE001 - Binary Ninja wrapper boundary.
        return ()


def _edges(owner, name):
    try:
        return tuple(getattr(owner, name, ()) or ())
    except Exception:  # noqa: BLE001 - Binary Ninja wrapper boundary.
        return ()


def _same_entity(left, right):
    if left is right:
        return True
    if left is None or right is None:
        return False
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001 - Binary Ninja wrapper equality boundary.
        return False


class _StackValuePolicy:
    """Add provider-proven private-stack loads to the core static-data policy."""

    def __init__(self, static_data, stack_load_values=()):
        self.static_data = static_data
        self.stack_load_values = dict(stack_load_values)

    def resolve_load(self, expression):
        index = _expression_index(expression)
        if index is None or index not in self.stack_load_values:
            return NotHandled()
        return Handled((self.stack_load_values[index],))

    def __call__(self, expression, operands):
        return self.static_data(expression, operands)


def _expression_index(expression):
    index = getattr(expression, "expr_index", None)
    return index if type(index) is int and index >= 0 else None


def _vsa_stack_offset(expression):
    if expression is None:
        return None
    try:
        values = expression.possible_values
    except Exception:  # noqa: BLE001 - Binary Ninja value-set boundary.
        return None
    if getattr(getattr(values, "type", None), "name", None) != "StackFrameOffset":
        return None
    offset = getattr(values, "offset", None)
    return offset if type(offset) is int else None


def _stack_pointer_index(ssa):
    architecture = getattr(getattr(ssa, "source_function", None), "arch", None)
    stack_pointer = getattr(architecture, "stack_pointer", None)
    register_index = getattr(architecture, "get_reg_index", None)
    if stack_pointer is None or not callable(register_index):
        return None
    try:
        index = register_index(stack_pointer)
    except Exception:  # noqa: BLE001 - Binary Ninja architecture boundary.
        return None
    return index if type(index) is int and index >= 0 else None


def _register_index(variable):
    index = getattr(getattr(variable, "reg", None), "index", None)
    return index if type(index) is int and index >= 0 else None


def _signed_constant(expression):
    if _operation_name(expression) not in _CONSTANT_OPERATIONS:
        return None
    value = getattr(expression, "constant", None)
    width = getattr(expression, "size", None)
    if type(value) is not int or type(width) is not int or width <= 0:
        return None
    bits = width * 8
    raw = value & ((1 << bits) - 1)
    sign = 1 << (bits - 1)
    return raw - (1 << bits) if raw & sign else raw


def _bounded_stack_offset(expression, offset):
    width = getattr(expression, "size", None)
    if type(width) is not int or width <= 0:
        return None
    limit = 1 << (width * 8 - 1)
    return offset if -limit <= offset < limit else None


def _syntactic_stack_offset(ssa, expression, stack_pointer, seen):
    index = _expression_index(expression)
    if index is None or index in seen:
        return None
    seen = seen | {index}
    operation = _operation_name(expression)
    if operation == _SSA_REGISTER_OPERATION:
        variable = getattr(expression, "src", None)
        if _register_index(variable) != stack_pointer:
            return None
        try:
            definition = ssa.get_ssa_reg_definition(variable)
        except Exception:  # noqa: BLE001 - Binary Ninja SSA boundary.
            return None
        if definition is None:
            return 0
        if (
            _operation_name(definition) != _SET_REGISTER_OPERATION
            or not _same_entity(getattr(definition, "dest", None), variable)
        ):
            return None
        return _syntactic_stack_offset(
            ssa, getattr(definition, "src", None), stack_pointer, seen
        )
    if operation not in _STACK_ADDRESSES:
        return None
    left = _syntactic_stack_offset(
        ssa, getattr(expression, "left", None), stack_pointer, seen
    )
    right = _signed_constant(getattr(expression, "right", None))
    if operation == "LLIL_ADD" and left is None:
        left = _syntactic_stack_offset(
            ssa, getattr(expression, "right", None), stack_pointer, seen
        )
        right = _signed_constant(getattr(expression, "left", None))
    if left is None or right is None:
        return None
    return _bounded_stack_offset(expression, left + right if operation == "LLIL_ADD" else left - right)


def _stack_offset(expression, ssa=None):
    offset = _vsa_stack_offset(expression)
    if offset is not None or ssa is None:
        return offset
    stack_pointer = _stack_pointer_index(ssa)
    return (
        None
        if stack_pointer is None
        else _syntactic_stack_offset(ssa, expression, stack_pointer, frozenset())
    )


def _ssa_expressions(ssa):
    count = getattr(ssa, "get_expr_count", None)
    expression = getattr(ssa, "get_expr", None)
    if not callable(count) or not callable(expression):
        return None
    try:
        length = count()
    except Exception:  # noqa: BLE001 - Binary Ninja IL boundary.
        return None
    if type(length) is not int or length < 0:
        return None
    values = []
    for index in range(length):
        try:
            values.append(expression(index))
        except Exception:  # noqa: BLE001 - an incomplete expression table is unusable.
            return None
    return tuple(values)


def _operand_expressions(value):
    index = _expression_index(value)
    if index is not None:
        return (value,)
    if type(value) not in (tuple, list):
        return ()
    expressions = []
    for item in value:
        expressions.extend(_operand_expressions(item))
    return tuple(expressions)


def _private_stack_slots(ssa, expressions):
    offsets = {}
    uses = {}
    for expression in expressions:
        offset = _stack_offset(expression, ssa)
        if offset is not None:
            index = _expression_index(expression)
            if index is None:
                return None
            offsets.setdefault(offset, set()).add(index)
            uses[index] = []
    for parent in expressions:
        operands = getattr(parent, "detailed_operands", None)
        if operands is None:
            return None
        try:
            items = tuple(operands)
        except Exception:  # noqa: BLE001 - Binary Ninja operand boundary.
            return None
        for item in items:
            if (
                type(item) not in (tuple, list)
                or len(item) != 3
                or type(item[0]) is not str
            ):
                return None
            name, value, _kind = item
            for child in _operand_expressions(value):
                index = _expression_index(child)
                if index in uses:
                    uses[index].append((_operation_name(parent), name))
    private_indexes = {
        index
        for index, references in uses.items()
        if references
        and all(
            (operation == _STACK_LOAD_OPERATION and name == "src")
            or (operation == _STACK_STORE_OPERATION and name == "dest")
            for operation, name in references
        )
    }
    return {
        offset
        for offset, indexes in offsets.items()
        if indexes and indexes <= private_indexes
    }


def _overlaps(left_offset, left_size, right_offset, right_size):
    left_end = left_offset + left_size
    right_end = right_offset + right_size
    return left_end > left_offset and right_end > right_offset and max(
        left_offset, right_offset
    ) < min(left_end, right_end)


def _memory_sources_for_private_stack_slot(ssa, memory, offset, size, seen):
    if type(memory) is not int or memory in seen:
        return None
    try:
        definition = ssa.get_ssa_memory_definition(memory)
    except Exception:  # noqa: BLE001 - Binary Ninja memory-SSA boundary.
        return None
    if definition is None:
        return None
    operation = _operation_name(definition)
    next_seen = seen | {memory}
    if operation == _MEMORY_PHI_OPERATION:
        incoming = getattr(definition, "src_memory", None)
        if type(incoming) not in (tuple, list) or not incoming:
            return None
        sources = []
        for source_memory in incoming:
            branch = _memory_sources_for_private_stack_slot(
                ssa, source_memory, offset, size, next_seen
            )
            if not branch:
                return None
            sources.extend(branch)
        return tuple(sources)
    if operation == _STACK_STORE_OPERATION:
        store_offset = _stack_offset(getattr(definition, "dest", None), ssa)
        store_size = getattr(definition, "size", None)
        if store_offset is not None:
            if type(store_size) is not int or store_size <= 0:
                return None
            if _overlaps(offset, size, store_offset, store_size):
                return (
                    (getattr(definition, "src", None),)
                    if store_offset == offset and store_size == size
                    else None
                )
        return _memory_sources_for_private_stack_slot(
            ssa, getattr(definition, "src_memory", None), offset, size, next_seen
        )
    if operation == _CALL_OPERATION:
        return _memory_sources_for_private_stack_slot(
            ssa, getattr(definition, "stack_memory", None), offset, size, next_seen
        )
    return None


def _instruction_index(expression):
    index = getattr(expression, "instr_index", None)
    return index if type(index) is int and index >= 0 else None


def _store_dominates_load(store, load):
    store_block = getattr(store, "il_basic_block", None)
    load_block = getattr(load, "il_basic_block", None)
    if store_block is None or load_block is None:
        return False
    if _same_entity(store_block, load_block):
        store_index = _instruction_index(store)
        load_index = _instruction_index(load)
        return store_index is not None and load_index is not None and store_index < load_index
    try:
        dominators = tuple(getattr(load_block, "dominators", ()) or ())
    except Exception:  # noqa: BLE001 - Binary Ninja CFG boundary.
        return False
    return any(_same_entity(store_block, block) for block in dominators)


def _dominating_private_stack_store_sources(ssa, expressions, load, offset, size):
    sources = []
    for store in expressions:
        if _operation_name(store) != _STACK_STORE_OPERATION:
            continue
        store_offset = _stack_offset(getattr(store, "dest", None), ssa)
        store_size = getattr(store, "size", None)
        if store_offset is None or type(store_size) is not int or store_size <= 0:
            continue
        if not _overlaps(offset, size, store_offset, store_size):
            continue
        if store_offset != offset or store_size != size or not _store_dominates_load(store, load):
            return None
        source = getattr(store, "src", None)
        if source is None:
            return None
        sources.append(source)
    return tuple(sources) if len(sources) == 1 else None


def _private_stack_load_values(view, ssa, policy):
    expressions = _ssa_expressions(ssa)
    if not expressions:
        return ()
    private_offsets = _private_stack_slots(ssa, expressions)
    if private_offsets is None:
        return ()
    values = []
    for load in expressions:
        if _operation_name(load) != _STACK_LOAD_OPERATION:
            continue
        index = _expression_index(load)
        offset = _stack_offset(getattr(load, "src", None), ssa)
        size = getattr(load, "size", None)
        memory = getattr(load, "src_memory", None)
        if (
            index is None
            or offset not in private_offsets
            or type(size) is not int
            or size <= 0
        ):
            continue
        sources = _memory_sources_for_private_stack_slot(
            ssa, memory, offset, size, frozenset()
        )
        if not sources:
            sources = _dominating_private_stack_store_sources(
                ssa, expressions, load, offset, size
            )
        if not sources or any(source is None for source in sources):
            continue
        resolved = tuple(_evaluate(view, ssa, source, policy) for source in sources)
        if any(type(result) is not CompleteValues for result in resolved):
            continue
        candidates = tuple(getattr(result, "values", None) for result in resolved)
        if (
            any(type(candidate) is not tuple or len(candidate) != 1 for candidate in candidates)
            or any(type(candidate[0]) is not int for candidate in candidates)
            or any(candidate != candidates[0] for candidate in candidates[1:])
        ):
            continue
        values.append((index, candidates[0][0]))
    return tuple(values)


def _branch_policy(view, llil):
    policy = memory.initialized_data_policy(view)
    if policy is None:
        return None
    ssa = getattr(llil, "ssa_form", None)
    if ssa is None:
        return policy
    return _StackValuePolicy(policy, _private_stack_load_values(view, ssa, policy))


def _targets_from_values(view, result):
    if type(result) is not CompleteValues:
        return result
    raw_targets = getattr(result, "values", None)
    if type(raw_targets) is not tuple or not raw_targets:
        return Inconclusive("value evaluation produced no target set")
    if any(type(target) is not int or target < 0 for target in raw_targets):
        return Inconclusive("value evaluation produced a malformed target")
    targets = tuple(sorted(set(raw_targets)))
    if any(not memory.is_executable_target(view, target) for target in targets):
        return Inconclusive("value evaluation produced a non-executable target")
    return targets


def _evaluate(view, il, destination, policy):
    if il is None or destination is None:
        return Inconclusive("current SSA destination is unavailable")
    try:
        result = evaluate_values(view, il, destination, _VALUE_BUDGET, policy)
    except Exception:  # noqa: BLE001 - public value-engine boundary.
        return Inconclusive("the core value evaluator raised an exception")
    if type(result) in (CompleteValues, Inconclusive):
        return result
    return Inconclusive("the core value evaluator returned an invalid result")


def _branch_values(view, llil, jump, policy):
    """Evaluate one current LLIL jump destination without changing IL layers."""

    llil_ssa = getattr(llil, "ssa_form", None)
    jump_ssa = getattr(jump, "ssa_form", None)
    return _evaluate(view, llil_ssa, getattr(jump_ssa, "dest", None), policy)


def _iter_indirect_jumps(llil):
    for instruction in _instructions(llil):
        if _operation_name(instruction) not in _BRANCH_OPERATIONS:
            continue
        if _operation_name(getattr(instruction, "dest", None)) not in _CONSTANT_OPERATIONS:
            yield instruction


def _edge_kind(edge):
    name = getattr(getattr(edge, "type", None), "name", None)
    return name if type(name) is str else None


def _same_edge(left, right):
    return (
        _edge_kind(left) == _edge_kind(right)
        and _same_entity(getattr(left, "source", None), getattr(right, "source", None))
        and _same_entity(getattr(left, "target", None), getattr(right, "target", None))
    )


def _terminal(block):
    instructions = _block_instructions(block)
    return instructions[-1] if instructions else None


def _parent_arms(parent):
    outgoing = _edges(parent, "outgoing_edges")
    if len(outgoing) != 2:
        return None
    arms = {}
    for edge in outgoing:
        kind = _edge_kind(edge)
        target = getattr(edge, "target", None)
        if kind not in _OUTCOMES or target is None or kind in arms:
            return None
        arms[kind] = target
    return arms if set(arms) == _OUTCOMES else None


def _arm_from_path_edge(edge):
    arm = getattr(edge, "source", None)
    join = getattr(edge, "target", None)
    if arm is None or join is None:
        return None
    outgoing = _edges(arm, "outgoing_edges")
    if len(outgoing) != 1 or not _same_edge(outgoing[0], edge):
        return None
    incoming = _edges(arm, "incoming_edges")
    if len(incoming) != 1:
        return None
    feeder = incoming[0]
    kind = _edge_kind(feeder)
    parent = getattr(feeder, "source", None)
    if (
        kind not in _OUTCOMES
        or parent is None
        or not _same_entity(getattr(feeder, "target", None), arm)
        or _operation_name(_terminal(parent)) not in {"LLIL_IF", "MLIL_IF"}
    ):
        return None
    arms = _parent_arms(parent)
    if arms is None or not _same_entity(arms[kind], arm):
        return None
    return kind, parent, arm


def _same_arm_match(left, right):
    return (
        left[0] == right[0]
        and _same_entity(left[1], right[1])
        and _same_entity(left[2], right[2])
    )


def _case_arm(case):
    matches = _case_arms(case)
    return matches[0] if len(matches) == 1 else None


def _case_arms(case):
    matches = []
    for source in getattr(case, "sources", ()) or ():
        for edge in getattr(source, "edges", ()) or ():
            match = _arm_from_path_edge(edge)
            if match is not None and not any(
                _same_arm_match(match, previous) for previous in matches
            ):
                matches.append(match)
    return tuple(matches)


def _case_outcomes(values):
    targets = getattr(values, "values", None)
    cases = getattr(values, "cases", None)
    if type(targets) is not tuple or len(targets) != 2 or type(cases) is not tuple:
        return None
    outcomes = {}
    parent = None
    arms = {}
    for case in cases:
        value = getattr(case, "value", None)
        if type(value) is not int or value not in targets or value in outcomes:
            return None
        match = _case_arm(case)
        if match is None:
            return None
        kind, candidate_parent, arm = match
        if kind in arms or (parent is not None and not _same_entity(parent, candidate_parent)):
            return None
        parent = candidate_parent
        arms[kind] = arm
        outcomes[value] = kind
    if set(outcomes) != set(targets) or set(arms) != _OUTCOMES:
        return None
    return parent, arms, {kind: value for value, kind in outcomes.items()}


def _flag_definition_reaches_branch(definition, branch, parent):
    definition_block = getattr(definition, "il_basic_block", None)
    branch_block = getattr(branch, "il_basic_block", None)
    if definition_block is None:
        definition_block = parent
    if branch_block is None:
        branch_block = parent
    if _same_entity(definition_block, branch_block):
        instructions = _block_instructions(parent)
        definition_indices = [
            index
            for index, instruction in enumerate(instructions)
            if _same_entity(instruction, definition)
        ]
        branch_indices = [
            index
            for index, instruction in enumerate(instructions)
            if _same_entity(instruction, branch)
        ]
        return (
            len(definition_indices) == 1
            and len(branch_indices) == 1
            and definition_indices[0] < branch_indices[0]
        )
    return _block_dominates(definition_block, branch_block)


def _llil_condition(parent, llil=None):
    branch = _terminal(parent)
    if _operation_name(branch) != "LLIL_IF":
        return None
    flag = getattr(branch, "condition", None)
    if _operation_name(flag) != "LLIL_FLAG":
        return None
    flag_ref = getattr(flag, "src", None)
    if flag_ref is None:
        return None
    instructions = _instructions(llil) if llil is not None else _block_instructions(parent)
    definitions = [
        instruction
        for instruction in instructions
        if _operation_name(instruction) == "LLIL_SET_FLAG"
        and _same_entity(getattr(instruction, "dest", None), flag_ref)
    ]
    if len(definitions) != 1:
        return None
    definition = definitions[0]
    if not _flag_definition_reaches_branch(definition, branch, parent):
        return None
    condition = getattr(definition, "src", None)
    operation = _operation_name(condition)
    return condition if operation is not None and operation.startswith("LLIL_CMP_") else None


def _block_span(block):
    instructions = _block_instructions(block)
    if not instructions:
        return None
    addresses = tuple(getattr(instruction, "address", None) for instruction in instructions)
    if any(type(address) is not int or address < 0 for address in addresses):
        return None
    return addresses[0], addresses[-1]


def _matching_llil_condition(llil, proof_parent, proof_arms):
    proof_branch = _terminal(proof_parent)
    source = getattr(proof_branch, "address", None)
    if (
        _operation_name(proof_branch) not in {"LLIL_IF", "MLIL_IF"}
        or type(source) is not int
        or source < 0
    ):
        return None
    candidates = []
    for instruction in _instructions(llil):
        if _operation_name(instruction) != "LLIL_IF" or getattr(instruction, "address", None) != source:
            continue
        parent = getattr(instruction, "il_basic_block", None)
        arms = _parent_arms(parent)
        condition = _llil_condition(parent, llil)
        if arms is None or condition is None:
            continue
        if any(_block_span(arms[kind]) != _block_span(proof_arms[kind]) for kind in _OUTCOMES):
            continue
        candidates.append(condition)
    return candidates[0] if len(candidates) == 1 else None


def _condition_for_arms(llil, parent, arms):
    operation = _operation_name(_terminal(parent))
    condition = _llil_condition(parent) if operation == "LLIL_IF" else None
    if condition is None and operation in {"LLIL_IF", "MLIL_IF"}:
        condition = _matching_llil_condition(llil, parent, arms)
    return condition


def _same_direct_llil_operand(left, right):
    operation = _operation_name(left)
    if operation != _operation_name(right):
        return False
    left_width = getattr(left, "size", None)
    right_width = getattr(right, "size", None)
    if (
        type(left_width) is not int
        or left_width < 0
        or left_width != right_width
    ):
        return False
    if operation in {"LLIL_REG", "LLIL_REG_SSA"}:
        left_source = getattr(left, "src", None)
        right_source = getattr(right, "src", None)
        return left_source is not None and _same_entity(left_source, right_source)
    if operation in {"LLIL_CONST", "LLIL_CONST_PTR"}:
        left_value = getattr(left, "constant", None)
        right_value = getattr(right, "constant", None)
        return type(left_value) is int and left_value == right_value
    return False


def _same_direct_llil_comparison(left, right):
    operation = _operation_name(left)
    left_width = getattr(left, "size", None)
    right_width = getattr(right, "size", None)
    if (
        operation is None
        or not operation.startswith("LLIL_CMP_")
        or operation != _operation_name(right)
        or type(left_width) is not int
        or left_width < 0
        or left_width != right_width
    ):
        return False
    return _same_direct_llil_operand(getattr(left, "left", None), getattr(right, "left", None)) and _same_direct_llil_operand(
        getattr(left, "right", None), getattr(right, "right", None)
    )


def _equivalent_case_targets(values, llil):
    targets = getattr(values, "values", None)
    cases = getattr(values, "cases", None)
    if type(targets) is not tuple or len(targets) != 2 or type(cases) is not tuple:
        return None

    matches_by_value = {}
    for case in cases:
        value = getattr(case, "value", None)
        matches = _case_arms(case)
        if (
            type(value) is not int
            or value not in targets
            or value in matches_by_value
            or len(matches) < 2
        ):
            return None
        matches_by_value[value] = matches
    if set(matches_by_value) != set(targets):
        return None

    parent_groups = []
    for value, matches in matches_by_value.items():
        for match in matches:
            parent = match[1]
            for group_parent, entries in parent_groups:
                if _same_entity(parent, group_parent):
                    entries.append((value, match))
                    break
            else:
                parent_groups.append((parent, [(value, match)]))
    if len(parent_groups) < 2:
        return None

    reference = None
    for parent, entries in parent_groups:
        arm_by_value = {}
        for value, match in entries:
            if value in arm_by_value:
                return None
            arm_by_value[value] = match
        if set(arm_by_value) != set(targets):
            return None

        arms = {}
        directed = {}
        for value in targets:
            kind, _candidate_parent, arm = arm_by_value[value]
            if kind in arms:
                return None
            arms[kind] = arm
            directed[kind] = value
        if set(arms) != _OUTCOMES:
            return None
        condition = _condition_for_arms(llil, parent, arms)
        if condition is None:
            return None
        if reference is None:
            reference = condition, directed
            continue
        if directed != reference[1] or not _same_direct_llil_comparison(
            reference[0], condition
        ):
            return None

    if reference is None:
        return None
    return reference[0], reference[1]["TrueBranch"], reference[1]["FalseBranch"]


def _directed_targets(values, llil):
    recovered = _case_outcomes(values)
    if recovered is None:
        return _equivalent_case_targets(values, llil)
    parent, arms, targets = recovered
    condition = _condition_for_arms(llil, parent, arms)
    if condition is None:
        return None
    return condition, targets["TrueBranch"], targets["FalseBranch"]


def _batch_failure(kind, failures):
    details = []
    for address, reason in failures[:4]:
        location = hex(address) if type(address) is int and address >= 0 else "<unknown>"
        details.append(f"{location}: {reason}")
    suffix = "" if len(failures) <= len(details) else f" (+{len(failures) - len(details)} more)"
    return Inconclusive(
        f"automatic {kind} collection could not prove {len(failures)} site(s): "
        + "; ".join(details)
        + suffix
    )


def branch_targets(query):
    jumps = tuple(_iter_indirect_jumps(query.llil))
    if not jumps:
        return CompleteBatch(())
    policy = _branch_policy(query.view, query.llil)
    if policy is None:
        return Inconclusive("could not snapshot initialized static data")

    facts = []
    failures = []
    sources = set()
    for jump in jumps:
        source = getattr(jump, "address", None)
        if type(source) is not int or source < 0 or source in sources:
            failures.append((source, "current indirect jump has no unique source"))
            continue
        sources.add(source)
        values = _branch_values(query.view, query.llil, jump, policy)
        targets = _targets_from_values(query.view, values)
        if type(targets) is Inconclusive:
            failures.append((source, targets.reason))
            continue
        directed = _directed_targets(values, query.llil)
        if directed is None:
            facts.append(BranchTargetFact(jump, targets))
            continue
        condition, true_target, false_target = directed
        facts.append(
            BranchTargetFact(
                jump,
                targets,
                condition=condition,
                true_target=true_target,
                false_target=false_target,
            )
        )
    return _batch_failure("branch target", failures) if failures else CompleteBatch(tuple(facts))


def call_targets(query):
    calls = tuple(mlil_helpers.iter_indirect_calls(query.mlil))
    if not calls:
        return CompleteBatch(())
    policy = memory.initialized_data_policy(query.view)
    if policy is None:
        return Inconclusive("could not snapshot initialized static data")
    ssa = getattr(query.mlil, "ssa_form", None)

    facts = []
    sources = set()
    for call in calls:
        source = getattr(call, "address", None)
        if type(source) is not int or source < 0 or source in sources:
            continue
        sources.add(source)
        call_ssa = getattr(call, "ssa_form", None)
        values = _evaluate(query.view, ssa, getattr(call_ssa, "dest", None), policy)
        targets = _targets_from_values(query.view, values)
        if type(targets) is Inconclusive:
            continue
        facts.append(CallTargetFact(call, targets))
    return CompleteBatch(tuple(facts))


def _direct_static_loads(mlil):
    for expression in mlil_helpers.iter_expressions(mlil):
        if _operation_name(expression) not in _MLIL_LOAD_OPERATIONS:
            continue
        address = _static_pointer_address(getattr(expression, "src", None))
        width = getattr(expression, "size", None)
        if address is not None and type(width) is int and width > 0:
            yield address, width


def _expression_identity(expression):
    index = _expression_index(expression)
    return ("index", index) if index is not None else ("identity", id(expression))


def _nested_expressions(value):
    if _operation_name(value) is not None:
        return (value,)
    if type(value) not in (tuple, list):
        return ()
    expressions = []
    for item in value:
        expressions.extend(_nested_expressions(item))
    return tuple(expressions)


def _expression_children(expression):
    operands = getattr(expression, "detailed_operands", None)
    if operands is not None:
        try:
            items = tuple(operands)
        except Exception:  # noqa: BLE001 - Binary Ninja operand boundary.
            return ()
        children = []
        for item in items:
            if type(item) not in (tuple, list) or len(item) != 3:
                return ()
            children.extend(_nested_expressions(item[1]))
        return tuple(children)
    children = []
    for name in ("src", "left", "right", "condition", "dest", "params"):
        children.extend(_nested_expressions(getattr(expression, name, None)))
    return tuple(children)


def _static_pointer_address(expression):
    if _operation_name(expression) not in _CONSTANT_OPERATIONS:
        return None
    address = getattr(expression, "constant", None)
    return address if type(address) is int and address >= 0 else None


def _direct_static_writes(mlil):
    for expression in mlil_helpers.iter_expressions(mlil):
        operation = _operation_name(expression)
        if operation not in _MLIL_STORE_OPERATIONS:
            continue
        address = _static_pointer_address(getattr(expression, "dest", None))
        width = getattr(expression, "size", None)
        if address is None or type(width) is not int or width <= 0:
            continue
        if operation in {"MLIL_STORE_STRUCT", "MLIL_STORE_STRUCT_SSA"}:
            offset = getattr(expression, "offset", None)
            if type(offset) is not int:
                continue
            address += offset
        if address >= 0:
            yield address, width


def global_data(query):
    policy = memory.initialized_data_policy(query.view)
    if policy is None:
        return Inconclusive("could not snapshot initialized static data")

    widths = {}
    writes = tuple(_direct_static_writes(query.mlil))
    for address, width in _direct_static_loads(query.mlil):
        if width not in _SUPPORTED_LOAD_WIDTHS or address % width != 0:
            continue
        if any(_overlaps(address, width, written_address, written_width) for written_address, written_width in writes):
            continue
        if policy.bytes_at(address, width) is None:
            continue
        previous = widths.get(address)
        if previous is not None and previous != width:
            return Inconclusive("one static slot is read at incompatible widths")
        widths[address] = width

    slots = tuple(sorted(widths.items()))
    if any(address < previous + width for (previous, width), (address, _next_width) in zip(slots, slots[1:])):
        return Inconclusive("direct static load slots overlap")

    types = {}
    facts = []
    for address, width in slots:
        data_type = types.get(width)
        if data_type is None:
            try:
                data_type, _name = query.view.parse_type_string(f"uint{width * 8}_t const")
            except Exception:  # noqa: BLE001 - Binary Ninja type parser boundary.
                return Inconclusive("could not create a native static-data type")
            if data_type is None:
                return Inconclusive("could not create a native static-data type")
            types[width] = data_type
        facts.append(GlobalDataFact(address, data_type))
    return CompleteBatch(tuple(facts))


def _integer_mask(size):
    if type(size) is not int or size <= 0 or size > 16:
        return None
    return (1 << (size * 8)) - 1


def _masked_integer(value, expression):
    mask = _integer_mask(getattr(expression, "size", None))
    return None if type(value) is not int or mask is None else value & mask


def _signed_integer(value, expression):
    mask = _integer_mask(getattr(expression, "size", None))
    if type(value) is not int or mask is None:
        return None
    bits = getattr(expression, "size") * 8
    value &= mask
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def _entity_identity(value):
    try:
        hash(value)
    except TypeError:
        return ("identity", id(value))
    return ("equality", value)


def _variable_identity(variable):
    version = getattr(variable, "version", None)
    if type(version) is int:
        for name in ("var", "reg"):
            base = getattr(variable, name, None)
            if base is not None:
                return ("ssa", _entity_identity(base), version)
    return ("variable", _entity_identity(variable))


def _zero_filled_ranges(view):
    try:
        sections = tuple(getattr(view, "sections", {}).values())
    except Exception:  # noqa: BLE001 - Binary Ninja wrapper boundary.
        return ()
    ranges = []
    for section in sections:
        if getattr(section, "type", None) != "NOBITS":
            continue
        start = getattr(section, "start", None)
        end = getattr(section, "end", None)
        if type(start) is int and type(end) is int and 0 <= start < end:
            ranges.append((start, end))
    return tuple(ranges)


class _ConcreteMemory:
    def __init__(self, view, byte_order):
        self.view = view
        self.byte_order = byte_order
        self.zero_filled = _zero_filled_ranges(view)
        self.writes = {}
        self.view_reads = set()

    def _zero_filled_at(self, address):
        return any(start <= address < end for start, end in self.zero_filled)

    def read(self, address, width):
        if (
            type(address) is not int
            or type(width) is not int
            or address < 0
            or width <= 0
            or width > 16
        ):
            return None
        data = bytearray()
        for offset in range(width):
            current = address + offset
            if current in self.writes:
                data.append(self.writes[current])
                continue
            try:
                raw = self.view.read(current, 1)
            except Exception:  # noqa: BLE001 - Binary Ninja memory boundary.
                return None
            if raw is not None and len(raw) == 1:
                data.append(raw[0])
                self.view_reads.add(current)
                continue
            if not self._zero_filled_at(current):
                return None
            data.append(0)
        return int.from_bytes(data, self.byte_order)

    def write(self, address, width, value):
        if (
            type(address) is not int
            or type(width) is not int
            or type(value) is not int
            or address < 0
            or width <= 0
            or width > 16
        ):
            return False
        data = (value & ((1 << (width * 8)) - 1)).to_bytes(
            width, self.byte_order
        )
        for offset, byte in enumerate(data):
            self.writes[address + offset] = byte
        return True

    def reset(self):
        self.writes.clear()
        self.view_reads.clear()


class _ConcreteMLIL:
    def __init__(self, view, il, initial_values=()):
        self.instructions = _instructions(il)
        self.by_index = {}
        self.next_index = {}
        self.values = {}
        self._program_valid = True
        self.memory = _ConcreteMemory(view, memory.byte_order(view) or "little")

        for instruction in self.instructions:
            index = _instruction_index(instruction)
            if index is None or index in self.by_index:
                self._program_valid = False
                continue
            self.by_index[index] = instruction
        self._record_next_indexes()
        self.reset(initial_values)

    def reset(self, initial_values=()):
        self.values.clear()
        self.memory.reset()
        self.valid = self._program_valid
        for variable, value in initial_values:
            if type(value) is not int:
                self.valid = False
                continue
            self.values[_variable_identity(variable)] = value
        return self.valid

    def _record_next_indexes(self):
        seen_blocks = set()
        for instruction in self.instructions:
            block = getattr(instruction, "il_basic_block", None)
            if block is None:
                continue
            key = _entity_identity(block)
            if key in seen_blocks:
                continue
            seen_blocks.add(key)
            block_instructions = _block_instructions(block)
            for position, current in enumerate(block_instructions[:-1]):
                following = block_instructions[position + 1]
                index = _instruction_index(current)
                next_index = _instruction_index(following)
                if index is not None and next_index is not None:
                    self.next_index[index] = next_index
        ordered = tuple(sorted(self.by_index))
        for index, following in zip(ordered, ordered[1:]):
            self.next_index.setdefault(index, following)

    def _variable_value(self, variable):
        return self.values.get(_variable_identity(variable))

    def _set_variable(self, variable, value):
        if variable is None or type(value) is not int:
            return False
        self.values[_variable_identity(variable)] = value
        return True

    def _evaluate(self, expression, active=frozenset()):
        operation = _operation_name(expression)
        if operation is None:
            return None
        key = _expression_identity(expression)
        if key in active:
            return None
        active = active | {key}

        if operation in _CONSTANT_OPERATIONS:
            value = getattr(expression, "constant", None)
            if type(value) is not int:
                return None
            return (
                value
                if getattr(expression, "size", None) == 0
                else _masked_integer(value, expression)
            )
        if operation in {"MLIL_VAR", "MLIL_VAR_SSA"}:
            return self._variable_value(getattr(expression, "src", None))
        if operation in _FIELD_VARIABLE_OPERATIONS:
            value = self._variable_value(getattr(expression, "src", None))
            offset = getattr(expression, "offset", None)
            mask = _integer_mask(getattr(expression, "size", None))
            if value is None or type(offset) is not int or offset < 0 or mask is None:
                return None
            return (value >> (offset * 8)) & mask
        if operation in _MLIL_LOAD_OPERATIONS:
            address = self._evaluate(getattr(expression, "src", None), active)
            if operation in {"MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA"}:
                offset = getattr(expression, "offset", None)
                if type(offset) is not int:
                    return None
                address = None if address is None else address + offset
            width = getattr(expression, "size", None)
            return self.memory.read(address, width)
        if operation in _STRING_CAST_OPERATIONS:
            value = self._evaluate(getattr(expression, "src", None), active)
            if value is None:
                return None
            if operation == "MLIL_SX":
                source = getattr(expression, "src", None)
                signed = _signed_integer(value, source)
                if signed is None:
                    return None
                return _masked_integer(signed, expression)
            return _masked_integer(value, expression)
        if operation in _STRING_UNARY_OPERATIONS:
            value = self._evaluate(getattr(expression, "src", None), active)
            if value is None:
                return None
            if operation == "MLIL_NEG":
                return _masked_integer(-value, expression)
            if operation == "MLIL_NOT":
                return _masked_integer(~value, expression)
            return _masked_integer(1 if value else 0, expression)
        if operation in _STRING_BINARY_OPERATIONS:
            return self._binary_value(expression, active)
        if operation in {"MLIL_MULU_DP", "MLIL_MULS_DP"}:
            left_expression = getattr(expression, "left", None)
            right_expression = getattr(expression, "right", None)
            left = self._evaluate(left_expression, active)
            right = self._evaluate(right_expression, active)
            if left is None or right is None:
                return None
            if operation == "MLIL_MULS_DP":
                left = _signed_integer(left, left_expression)
                right = _signed_integer(right, right_expression)
                return None if left is None or right is None else left * right
            left_mask = _integer_mask(getattr(left_expression, "size", None))
            right_mask = _integer_mask(getattr(right_expression, "size", None))
            if left_mask is None or right_mask is None:
                return None
            return (left & left_mask) * (right & right_mask)
        if operation in _STRING_COMPARISON_OPERATIONS:
            return self._comparison_value(expression, active)
        if operation == "MLIL_TEST_BIT":
            left = self._evaluate(getattr(expression, "left", None), active)
            right = self._evaluate(getattr(expression, "right", None), active)
            return None if left is None or right is None else (left >> right) & 1
        return None

    def _binary_value(self, expression, active):
        left_expression = getattr(expression, "left", None)
        right_expression = getattr(expression, "right", None)
        left = self._evaluate(left_expression, active)
        right = self._evaluate(right_expression, active)
        if left is None or right is None:
            return None
        operation = _operation_name(expression)
        if operation == "MLIL_ADD":
            value = left + right
        elif operation == "MLIL_SUB":
            value = left - right
        elif operation == "MLIL_MUL":
            value = left * right
        elif operation == "MLIL_AND":
            value = left & right
        elif operation == "MLIL_OR":
            value = left | right
        elif operation == "MLIL_XOR":
            value = left ^ right
        elif operation == "MLIL_LSL":
            value = left << right
        elif operation == "MLIL_LSR":
            value = left >> right
        elif operation == "MLIL_ASR":
            value = _signed_integer(left, left_expression)
            value = None if value is None else value >> right
        elif operation == "MLIL_DIVU":
            value = None if right == 0 else left // right
        elif operation == "MLIL_MODU":
            value = None if right == 0 else left % right
        else:
            signed_left = _signed_integer(left, left_expression)
            signed_right = _signed_integer(right, right_expression)
            if signed_left is None or signed_right in (None, 0):
                return None
            quotient = abs(signed_left) // abs(signed_right)
            if (signed_left < 0) != (signed_right < 0):
                quotient = -quotient
            value = quotient if operation == "MLIL_DIVS" else signed_left - quotient * signed_right
        return None if value is None else _masked_integer(value, expression)

    def _comparison_value(self, expression, active):
        left_expression = getattr(expression, "left", None)
        right_expression = getattr(expression, "right", None)
        left = self._evaluate(left_expression, active)
        right = self._evaluate(right_expression, active)
        if left is None or right is None:
            return None
        operation = _operation_name(expression)
        if "_CMP_S" in operation:
            left = _signed_integer(left, left_expression)
            right = _signed_integer(right, right_expression)
            if left is None or right is None:
                return None
        if operation.endswith("_E"):
            return int(left == right)
        if operation.endswith("_NE"):
            return int(left != right)
        if operation.endswith("_LT"):
            return int(left < right)
        if operation.endswith("_LE"):
            return int(left <= right)
        if operation.endswith("_GT"):
            return int(left > right)
        return int(left >= right)

    def run(
        self,
        start_index,
        allowed_indexes=None,
        stop_after=None,
        stop_call_destination=None,
    ):
        if not self.valid or type(start_index) is not int or start_index < 0:
            return False, None
        if stop_after is not None and (type(stop_after) is not int or stop_after < 0):
            return False, None
        if stop_call_destination is not None and (
            type(stop_call_destination) is not int or stop_call_destination < 0
        ):
            return False, None
        allowed = None if allowed_indexes is None else frozenset(allowed_indexes)
        index = start_index
        for _step in range(_STRING_EXECUTION_LIMIT):
            if allowed is not None and index not in allowed:
                return True, index
            current_index = index
            instruction = self.by_index.get(index)
            if instruction is None:
                return False, None
            operation = _operation_name(instruction)
            if operation in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"}:
                value = self._evaluate(getattr(instruction, "src", None))
                if value is None or not self._set_variable(getattr(instruction, "dest", None), value):
                    return False, None
                index = self.next_index.get(index)
            elif operation in _SET_FIELD_OPERATIONS:
                variable = getattr(instruction, "dest", None)
                value = self._evaluate(getattr(instruction, "src", None))
                offset = getattr(instruction, "offset", None)
                mask = _integer_mask(getattr(instruction, "size", None))
                previous = self._variable_value(variable)
                if (
                    value is None
                    or previous is None
                    or type(offset) is not int
                    or offset < 0
                    or mask is None
                ):
                    return False, None
                shifted_mask = mask << (offset * 8)
                if not self._set_variable(variable, (previous & ~shifted_mask) | ((value & mask) << (offset * 8))):
                    return False, None
                index = self.next_index.get(index)
            elif operation in _MLIL_STORE_OPERATIONS:
                address = self._evaluate(getattr(instruction, "dest", None))
                if operation in {"MLIL_STORE_STRUCT", "MLIL_STORE_STRUCT_SSA"}:
                    offset = getattr(instruction, "offset", None)
                    if type(offset) is not int:
                        return False, None
                    address = None if address is None else address + offset
                value = self._evaluate(getattr(instruction, "src", None))
                if not self.memory.write(address, getattr(instruction, "size", None), value):
                    return False, None
                index = self.next_index.get(index)
            elif operation == "MLIL_IF":
                condition = self._evaluate(getattr(instruction, "condition", None))
                target = getattr(instruction, "true" if condition else "false", None)
                if condition is None or type(target) is not int or target < 0:
                    return False, None
                index = target
            elif operation == "MLIL_GOTO":
                target = getattr(instruction, "dest", None)
                if type(target) is not int or target < 0:
                    return False, None
                index = target
            elif operation in _CALL_OPERATIONS:
                arguments = _direct_call_arguments(instruction)
                if (
                    stop_call_destination is not None
                    and arguments
                    and arguments[0] == stop_call_destination
                ):
                    return True, current_index
                return False, None
            elif operation in {"MLIL_RET", "MLIL_NORET"}:
                return operation == "MLIL_RET", None
            elif operation == "MLIL_NOP":
                index = self.next_index.get(index)
            else:
                return False, None
            if current_index == stop_after:
                return True, None
            if index is None:
                return False, None
        return False, None


def _direct_call_arguments(call):
    try:
        params = tuple(getattr(call, "params", ()) or ())
    except Exception:  # noqa: BLE001 - Binary Ninja call-parameter boundary.
        return None
    addresses = tuple(_static_pointer_address(param) for param in params)
    return addresses if all(address is not None for address in addresses) else None


def _textual_plaintext(memory, destination):
    if destination not in memory.writes:
        return None
    data = bytearray()
    for offset in range(_STRING_BYTES_LIMIT):
        value = memory.read(destination + offset, 1)
        if value is None:
            return None
        if value == 0:
            break
        data.append(value)
    else:
        return None
    if not data:
        return None
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(not character.isprintable() and character not in "\t\n\r" for character in text):
        return None
    return bytes(data)


def _function_at(view, address):
    getter = getattr(view, "get_function_at", None)
    if not callable(getter):
        return None
    try:
        return getter(address)
    except Exception:  # noqa: BLE001 - Binary Ninja function lookup boundary.
        return None


def _recover_direct_call_strings(query):
    facts = []
    seen = set()
    decoders = {}
    for call in mlil_helpers.iter_calls(query.mlil):
        call_address = getattr(call, "address", None)
        target = _static_pointer_address(getattr(call, "dest", None))
        arguments = _direct_call_arguments(call)
        if (
            type(call_address) is not int
            or call_address < 0
            or call_address in seen
            or target is None
            or arguments is None
            or len(arguments) != 2
        ):
            continue
        seen.add(call_address)
        decoder = decoders.get(target)
        if decoder is None:
            function = _function_at(query.view, target)
            try:
                parameters = tuple(getattr(function, "parameter_vars", ()) or ())
            except Exception:  # noqa: BLE001 - Binary Ninja function boundary.
                decoders[target] = False
                continue
            if len(parameters) != 2:
                decoders[target] = False
                continue
            callee_mlil = getattr(function, "medium_level_il", None)
            instructions = _instructions(callee_mlil)
            start = _instruction_index(instructions[0]) if instructions else None
            if start is None:
                decoders[target] = False
                continue
            machine = _ConcreteMLIL(query.view, callee_mlil)
            if not machine.valid:
                decoders[target] = False
                continue
            decoder = machine, parameters, start
            decoders[target] = decoder
        if decoder is False:
            continue
        machine, parameters, start = decoder
        if not machine.reset(
            ((parameters[0], arguments[0]), (parameters[1], arguments[1]))
        ):
            continue
        completed, _exit = machine.run(start)
        plaintext = _textual_plaintext(machine.memory, arguments[0]) if completed else None
        if plaintext is not None:
            facts.append(
                StringRecoveryFact(
                    call_address,
                    arguments[1],
                    arguments[0],
                    plaintext,
                )
            )
    return tuple(facts)


def _loop_layout(mlil):
    instructions = _instructions(mlil)
    blocks = []
    seen = set()
    for instruction in instructions:
        block = getattr(instruction, "il_basic_block", None)
        if block is None:
            continue
        key = _entity_identity(block)
        if key not in seen:
            seen.add(key)
            blocks.append(block)
    block_by_key = {_entity_identity(block): block for block in blocks}
    block_by_index = {}
    for block in blocks:
        for instruction in _block_instructions(block):
            index = _instruction_index(instruction)
            if index is None or index in block_by_index:
                return None
            block_by_index[index] = block
    instruction_by_index = {
        index: instruction
        for instruction in instructions
        for index in (_instruction_index(instruction),)
        if index is not None
    }
    return instructions, block_by_key, block_by_index, instruction_by_index


def _loop_context(store, layout):
    store_block = getattr(store, "il_basic_block", None)
    if store_block is None:
        return None
    _instructions, block_by_key, block_by_index, _instruction_by_index = layout
    store_key = _entity_identity(store_block)
    if (
        store_key not in block_by_key
        or _operation_name(_terminal(store_block)) != "MLIL_IF"
    ):
        return None
    latch = _terminal(store_block)
    targets = []
    for name in ("true", "false"):
        index = getattr(latch, name, None)
        target = block_by_index.get(index) if type(index) is int and index >= 0 else None
        if target is not None:
            targets.append(target)
    if len(targets) != 2 or _same_entity(targets[0], targets[1]):
        return None

    try:
        dominators = tuple(getattr(store_block, "dominators", ()) or ())
    except Exception:  # noqa: BLE001 - Binary Ninja CFG boundary.
        dominators = ()
    headers = [
        block
        for block in targets
        if any(_same_entity(block, dominator) for dominator in dominators)
    ]
    if len(headers) > 1:
        return None

    def region_from(candidate):
        pending = [candidate]
        region = {}
        while pending:
            block = pending.pop()
            key = _entity_identity(block)
            if key in region:
                continue
            if len(region) >= _STRING_BYTES_LIMIT:
                return None
            region[key] = block
            if _same_entity(block, store_block):
                continue
            successors = [
                getattr(edge, "target", None)
                for edge in _edges(block, "outgoing_edges")
            ]
            if not successors or any(successor is None for successor in successors):
                return None
            pending.extend(successors)
        if store_key not in region:
            return None
        for key, block in region.items():
            if not _same_entity(block, store_block) and any(
                _entity_identity(getattr(edge, "target", None)) not in region
                for edge in _edges(block, "outgoing_edges")
                if getattr(edge, "target", None) is not None
            ):
                return None
            if not _same_entity(block, candidate) and any(
                _entity_identity(getattr(edge, "source", None)) not in region
                for edge in _edges(block, "incoming_edges")
                if getattr(edge, "source", None) is not None
            ):
                return None
        return region

    header = headers[0] if headers else None
    region = region_from(header) if header is not None else None
    if region is None and not dominators:
        candidates = [
            (candidate, region_from(candidate))
            for candidate in targets
        ]
        candidates = [item for item in candidates if item[1] is not None]
        if len(candidates) != 1:
            return None
        header, region = candidates[0]
    if header is None or region is None:
        return None
    preheaders = {
        _entity_identity(getattr(edge, "source", None)): getattr(edge, "source", None)
        for edge in _edges(header, "incoming_edges")
        if getattr(edge, "source", None) is not None
        and _entity_identity(getattr(edge, "source", None)) not in region
    }
    if len(preheaders) != 1:
        return None
    preheader = next(iter(preheaders.values()))
    preheader_edges = _edges(preheader, "outgoing_edges")
    if (
        _operation_name(_terminal(preheader)) != "MLIL_GOTO"
        or len(preheader_edges) != 1
        or not _same_entity(getattr(preheader_edges[0], "target", None), header)
    ):
        return None
    allowed = set()
    for block in tuple(region.values()) + (preheader,):
        for instruction in _block_instructions(block):
            index = _instruction_index(instruction)
            if index is None:
                return None
            allowed.add(index)
    start_instructions = _block_instructions(preheader)
    start = _instruction_index(start_instructions[0]) if start_instructions else None
    return None if start is None else start, frozenset(allowed)


def _indexed_static_base(expression):
    if _operation_name(expression) != "MLIL_ADD":
        return None
    left = getattr(expression, "left", None)
    right = getattr(expression, "right", None)
    left_address = _static_pointer_address(left)
    right_address = _static_pointer_address(right)
    if left_address is not None and _operation_name(right) not in _CONSTANT_OPERATIONS:
        return left_address
    if right_address is not None and _operation_name(left) not in _CONSTANT_OPERATIONS:
        return right_address
    return None


def _indexed_static_pointer(expression):
    if _operation_name(expression) != "MLIL_ADD":
        return None
    left = getattr(expression, "left", None)
    right = getattr(expression, "right", None)
    left_address = _static_pointer_address(left)
    right_address = _static_pointer_address(right)
    if left_address is not None and _operation_name(right) in {"MLIL_VAR", "MLIL_VAR_SSA"}:
        return left_address, getattr(right, "src", None)
    if right_address is not None and _operation_name(left) in {"MLIL_VAR", "MLIL_VAR_SSA"}:
        return right_address, getattr(left, "src", None)
    return None


def _field_variable(expression):
    operation = _operation_name(expression)
    if operation in _FIELD_VARIABLE_OPERATIONS | {"MLIL_VAR", "MLIL_VAR_SSA"}:
        return getattr(expression, "src", None)
    if operation == "MLIL_LOW_PART":
        return _field_variable(getattr(expression, "src", None))
    return None


def _constant_value(expression):
    if _operation_name(expression) not in _CONSTANT_OPERATIONS:
        return None
    return _masked_integer(getattr(expression, "constant", None), expression)


def _expression_variables(expression):
    pending = [expression]
    seen = set()
    variables = []
    variable_keys = set()
    while pending:
        current = pending.pop()
        operation = _operation_name(current)
        if operation is None:
            continue
        key = _expression_identity(current)
        if key in seen:
            continue
        seen.add(key)
        if operation in {"MLIL_VAR", "MLIL_VAR_SSA"}:
            variable = getattr(current, "src", None)
            if variable is None:
                continue
            variable_key = _entity_identity(variable)
            if variable_key not in variable_keys:
                variable_keys.add(variable_key)
                variables.append(variable)
            continue
        for name in ("src", "left", "right", "condition"):
            child = getattr(current, name, None)
            if _operation_name(child) is not None:
                pending.append(child)
    return tuple(variables)


def _same_variable_expression(expression, variable):
    return (
        _operation_name(expression) in {"MLIL_VAR", "MLIL_VAR_SSA"}
        and _same_entity(getattr(expression, "src", None), variable)
    )


def _set_constant(block, variable):
    values = [
        _constant_value(getattr(instruction, "src", None))
        for instruction in _block_instructions(block)
        if _operation_name(instruction) in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"}
        and _same_entity(getattr(instruction, "dest", None), variable)
    ]
    return values[0] if len(values) == 1 and values[0] is not None else None


def _counter_step(block, variable):
    steps = []
    for instruction in _block_instructions(block):
        if (
            _operation_name(instruction) not in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"}
            or not _same_entity(getattr(instruction, "dest", None), variable)
        ):
            continue
        source = getattr(instruction, "src", None)
        if _operation_name(source) != "MLIL_ADD":
            continue
        left = getattr(source, "left", None)
        right = getattr(source, "right", None)
        if _same_variable_expression(left, variable):
            steps.append(_constant_value(right))
        elif _same_variable_expression(right, variable):
            steps.append(_constant_value(left))
    return steps[0] if len(steps) == 1 and steps[0] is not None else None


def _counter_bound(latch, variable):
    condition = getattr(latch, "condition", None)
    if _operation_name(condition) != "MLIL_CMP_NE":
        return None
    left = getattr(condition, "left", None)
    right = getattr(condition, "right", None)
    if _same_variable_expression(left, variable):
        return _constant_value(right)
    if _same_variable_expression(right, variable):
        return _constant_value(left)
    return None


def _inline_feedback_pattern(store, layout):
    pointer = _indexed_static_pointer(getattr(store, "dest", None))
    state = _field_variable(getattr(store, "src", None))
    context = _loop_context(store, layout) if pointer is not None and state is not None else None
    if context is None:
        return None
    preheader_start, allowed = context
    _instructions, _block_by_key, _block_by_index, instruction_by_index = layout
    preheader = getattr(instruction_by_index.get(preheader_start), "il_basic_block", None)
    preheader_goto = _terminal(preheader)
    header_start = getattr(preheader_goto, "dest", None)
    store_block = getattr(store, "il_basic_block", None)
    latch = _terminal(store_block)
    if (
        type(header_start) is not int
        or header_start < 0
        or _operation_name(latch) != "MLIL_IF"
    ):
        return None
    header = getattr(instruction_by_index.get(header_start), "il_basic_block", None)
    targets = [
        getattr(instruction_by_index.get(getattr(latch, name, None)), "il_basic_block", None)
        for name in ("true", "false")
    ]
    exits = [target for target in targets if target is not None and not _same_entity(target, header)]
    destination, index_variable = pointer
    initial_index = _set_constant(preheader, index_variable)
    initial_state = _set_constant(preheader, state)
    step = _counter_step(store_block, index_variable)
    bound = _counter_bound(latch, index_variable)
    state_updates = [
        instruction
        for instruction in _block_instructions(store_block)
        if _operation_name(instruction) in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"}
        and _same_entity(getattr(instruction, "dest", None), state)
        and _operation_name(getattr(instruction, "src", None)) == "MLIL_XOR"
    ]
    if (
        header is None
        or len(exits) != 1
        or initial_index is None
        or initial_state is None
        or step is None
        or bound is None
        or step <= 0
        or bound <= initial_index
        or (bound - initial_index) % step != 0
        or len(state_updates) != 1
    ):
        return None
    count = (bound - initial_index) // step
    if count <= 0 or count > _STRING_BYTES_LIMIT:
        return None
    store_index = _instruction_index(store)
    if store_index is None:
        return None
    return (
        destination,
        index_variable,
        state,
        initial_index,
        initial_state,
        count,
        step,
        header_start,
        allowed,
        store_index,
        exits[0],
    )


def _consumer_call(mlil, start_block, destination):
    if start_block is None:
        return None
    calls = {}
    pending = [start_block]
    seen = set()
    while pending and len(seen) <= _STRING_BYTES_LIMIT:
        block = pending.pop()
        key = _entity_identity(block)
        if key in seen:
            continue
        seen.add(key)
        for instruction in _block_instructions(block):
            if _operation_name(instruction) not in _CALL_OPERATIONS:
                continue
            arguments = _direct_call_arguments(instruction)
            address = getattr(instruction, "address", None)
            if (
                arguments
                and arguments[0] == destination
                and type(address) is int
                and address >= 0
            ):
                calls[address] = instruction
        pending.extend(
            target
            for edge in _edges(block, "outgoing_edges")
            for target in (getattr(edge, "target", None),)
            if target is not None
        )
    return next(iter(calls.values())) if len(calls) == 1 else None


def _block_dominates(source, target):
    if _same_entity(source, target):
        return True
    try:
        dominators = tuple(getattr(target, "dominators", ()) or ())
    except Exception:  # noqa: BLE001 - Binary Ninja CFG boundary.
        return False
    return any(_same_entity(source, block) for block in dominators)


def _loop_extra_initial_states(layout, header_start, allowed, excluded):
    instructions, _blocks, block_by_index, instruction_by_index = layout
    header = block_by_index.get(header_start)
    if header is None:
        return ()
    excluded_keys = {_entity_identity(variable) for variable in excluded}
    read_variables = []
    read_keys = set()
    assigned = set()
    for index in allowed:
        instruction = instruction_by_index.get(index)
        if instruction is None:
            continue
        for variable in _expression_variables(instruction):
            key = _entity_identity(variable)
            if key not in read_keys:
                read_keys.add(key)
                read_variables.append(variable)
        if _operation_name(instruction) in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"}:
            variable = getattr(instruction, "dest", None)
            if variable is not None:
                assigned.add(_entity_identity(variable))
    states = []
    for variable in read_variables:
        key = _entity_identity(variable)
        if key in excluded_keys or key not in assigned:
            continue
        definitions = []
        for instruction in instructions:
            index = _instruction_index(instruction)
            if (
                index is None
                or _operation_name(instruction) not in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"}
                or not _same_entity(getattr(instruction, "dest", None), variable)
            ):
                continue
            value = _constant_value(getattr(instruction, "src", None))
            block = getattr(instruction, "il_basic_block", None)
            if (
                value is not None
                and block is not None
                and not _same_entity(block, header)
                and _block_dominates(block, header)
            ):
                definitions.append(value)
        if len(definitions) == 1:
            states.append((variable, definitions[0]))
    return tuple(states)


def _replay_inline_pattern(machine, pattern, extra_states=()):
    (
        destination,
        index_variable,
        state_variable,
        initial_index,
        initial_state,
        count,
        step,
        header_start,
        allowed,
        store_index,
        _exit_block,
    ) = pattern
    data = bytearray()
    sources = set()
    current_index = initial_index
    current_state = initial_state
    extra_values = [value for _variable, value in extra_states]
    for _iteration in range(count):
        values = [
            (index_variable, current_index),
            (state_variable, current_state),
            *zip((variable for variable, _value in extra_states), extra_values),
        ]
        if not machine.reset(values):
            return None
        completed, _exit = machine.run(header_start, allowed, store_index)
        value = machine.memory.writes.get(destination + current_index)
        current_state = machine._variable_value(state_variable)
        extra_values = [
            machine._variable_value(variable)
            for variable, _value in extra_states
        ]
        if (
            not completed
            or type(value) is not int
            or type(current_state) is not int
            or any(type(extra) is not int for extra in extra_values)
            or len(machine.memory.view_reads) < 2
        ):
            return None
        data.append(value)
        sources.update(machine.memory.view_reads)
        current_index += step
    return bytes(data), sources


def _recover_inline_loop_strings(query):
    layout = _loop_layout(query.mlil)
    if layout is None:
        return ()
    machine = None
    facts = []
    seen = set()
    for store in layout[0]:
        if (
            _operation_name(store) not in _MLIL_STORE_OPERATIONS
            or getattr(store, "size", None) != 1
        ):
            continue
        pattern = _inline_feedback_pattern(store, layout)
        if pattern is None:
            continue
        destination = pattern[0]
        index_variable = pattern[1]
        state_variable = pattern[2]
        header_start = pattern[7]
        allowed = pattern[8]
        store_index = pattern[9]
        exit_block = pattern[10]
        if (destination, store_index) in seen:
            continue
        seen.add((destination, store_index))
        if machine is None:
            machine = _ConcreteMLIL(query.view, query.mlil)
            if not machine.valid:
                return tuple(facts)
        replay = _replay_inline_pattern(machine, pattern)
        if replay is None:
            extra_states = _loop_extra_initial_states(
                layout,
                header_start,
                allowed,
                (index_variable, state_variable),
            )
            replay = _replay_inline_pattern(machine, pattern, extra_states)
        if replay is None:
            continue
        data, sources = replay
        if 0 not in data or not sources:
            continue
        terminator = data.index(0)
        if any(data[terminator + 1 :]):
            continue
        plaintext = bytes(data[:terminator])
        consumer = _consumer_call(query.mlil, exit_block, destination)
        comment = consumer if consumer is not None else store
        call_address = getattr(comment, "address", None)
        if (
            plaintext == b""
            or type(call_address) is not int
            or call_address < 0
        ):
            continue
        source = min(sources)
        facts.append(StringRecoveryFact(call_address, source, destination, plaintext))
    return tuple(facts)


def _proven_pointer_address(expression):
    address = _static_pointer_address(expression)
    if address is not None:
        return address
    try:
        values = expression.possible_values
    except Exception:  # noqa: BLE001 - Binary Ninja value-set boundary.
        return None
    name = getattr(getattr(values, "type", None), "name", None)
    value = getattr(values, "value", None)
    return value if name in {"ConstantValue", "ConstantPointerValue"} and type(value) is int and value >= 0 else None


def _llil_block_static_loads(function, address):
    llil = getattr(function, "low_level_il", None)
    blocks = {
        _entity_identity(getattr(instruction, "il_basic_block", None)): getattr(
            instruction, "il_basic_block", None
        )
        for instruction in _instructions(llil)
        if getattr(instruction, "il_basic_block", None) is not None
        and getattr(instruction, "address", None) == address
        and _operation_name(instruction) in {"LLIL_STORE", "LLIL_STORE_SSA"}
    }
    addresses = set()
    for block in blocks.values():
        pending = list(_block_instructions(block))
        seen = set()
        while pending:
            expression = pending.pop()
            operation = _operation_name(expression)
            if operation is None:
                continue
            key = _expression_identity(expression)
            if key in seen:
                continue
            seen.add(key)
            if operation in _STATIC_LOAD_OPERATIONS:
                pointer = _proven_pointer_address(getattr(expression, "src", None))
                if pointer is not None:
                    addresses.add(pointer)
                    continue
            pending.extend(_expression_children(expression))
    return tuple(sorted(addresses))


def _static_consumer_destinations(current_mlil):
    destinations = set()
    for call in mlil_helpers.iter_calls(current_mlil):
        if _static_pointer_address(getattr(call, "dest", None)) is None:
            continue
        arguments = _direct_call_arguments(call)
        if arguments:
            destinations.add(arguments[0])
    return destinations


def _recover_static_initializer_strings(query):
    destinations = _static_consumer_destinations(query.mlil)
    machine = None
    facts = []
    seen = set()
    for store in _instructions(query.mlil):
        if (
            _operation_name(store) not in _MLIL_STORE_OPERATIONS
            or getattr(store, "size", None) != 1
        ):
            continue
        destination = _static_pointer_address(getattr(store, "dest", None))
        block = getattr(store, "il_basic_block", None)
        block_key = _entity_identity(block) if block is not None else None
        if (
            destination not in destinations
            or block is None
            or (destination, block_key) in seen
        ):
            continue
        seen.add((destination, block_key))
        sources = _llil_block_static_loads(query.function, getattr(store, "address", None))
        start_instructions = _block_instructions(block)
        start = _instruction_index(start_instructions[0]) if start_instructions else None
        if not sources or start is None:
            continue
        if machine is None:
            machine = _ConcreteMLIL(query.view, query.mlil)
            if not machine.valid:
                return tuple(facts)
        if not machine.reset():
            continue
        completed, call_index = machine.run(
            start,
            stop_call_destination=destination,
        )
        plaintext = _textual_plaintext(machine.memory, destination) if completed else None
        call = machine.by_index.get(call_index)
        call_address = getattr(call, "address", None)
        if plaintext is not None and type(call_address) is int and call_address >= 0:
            facts.append(
                StringRecoveryFact(call_address, min(sources), destination, plaintext)
            )
    return tuple(facts)


def _static_load_address(expression):
    current = expression
    while _operation_name(current) in _STRING_CAST_OPERATIONS:
        current = getattr(current, "src", None)
    operation = _operation_name(current)
    if operation not in _MLIL_LOAD_OPERATIONS:
        return None
    address = _static_pointer_address(getattr(current, "src", None))
    if address is None:
        return None
    if operation in {"MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA"}:
        offset = getattr(current, "offset", None)
        if type(offset) is not int:
            return None
        address += offset
    return address


def _last_variable_definition(block, variable, before_index=None):
    definitions = [
        candidate
        for candidate in _block_instructions(block)
        if (
            (candidate_index := _instruction_index(candidate)) is not None
            and (before_index is None or candidate_index < before_index)
            and _operation_name(candidate) in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"}
            and _same_entity(getattr(candidate, "dest", None), variable)
        )
    ]
    return definitions[-1] if definitions else None


def _unique_static_flag_definitions(instructions, done_stores):
    definitions = {}
    for instruction in instructions:
        operation = _operation_name(instruction)
        if operation not in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA", *_SET_FIELD_OPERATIONS}:
            continue
        variable = getattr(instruction, "dest", None)
        if variable is None:
            continue
        key = _variable_identity(variable)
        definitions[key] = (
            instruction
            if operation in {"MLIL_SET_VAR", "MLIL_SET_VAR_SSA"} and key not in definitions
            else None
        )
    return {
        key: definition
        for key, definition in definitions.items()
        if (
            definition is not None
            and _static_load_address(getattr(definition, "src", None)) in done_stores
        )
    }


def _guard_flag_address(instruction, static_definitions=None):
    if _operation_name(instruction) != "MLIL_IF":
        return None
    block = getattr(instruction, "il_basic_block", None)
    instruction_index = _instruction_index(instruction)
    if block is None or instruction_index is None:
        return None
    flags = set()
    for variable in _expression_variables(getattr(instruction, "condition", None)):
        definition = _last_variable_definition(block, variable, instruction_index)
        if definition is None and static_definitions is not None:
            candidate = static_definitions.get(_variable_identity(variable))
            candidate_block = getattr(candidate, "il_basic_block", None)
            candidate_index = _instruction_index(candidate)
            if (
                candidate_block is not None
                and candidate_index is not None
                and (
                    candidate_index < instruction_index
                    if _same_entity(candidate_block, block)
                    else _block_dominates(candidate_block, block)
                )
            ):
                definition = candidate
        address = _static_load_address(getattr(definition, "src", None))
        if address is not None:
            flags.add(address)
    return next(iter(flags)) if len(flags) == 1 else None


def _reachable_blocks(start, stops=(), limit=_STRING_BYTES_LIMIT):
    pending = [start]
    blocks = {}
    stop_keys = {_entity_identity(block) for block in stops}
    while pending:
        block = pending.pop()
        key = _entity_identity(block)
        if key in blocks:
            continue
        if len(blocks) >= limit:
            return None
        blocks[key] = block
        if key in stop_keys:
            continue
        pending.extend(
            target
            for edge in _edges(block, "outgoing_edges")
            for target in (getattr(edge, "target", None),)
            if target is not None
        )
    return blocks


def _path_blocks(start, stop, reachable):
    pending = [stop]
    blocks = {}
    start_key = _entity_identity(start)
    while pending:
        block = pending.pop()
        key = _entity_identity(block)
        if key in blocks or key not in reachable:
            continue
        if len(blocks) >= _STRING_BYTES_LIMIT:
            return None
        blocks[key] = block
        pending.extend(
            source
            for edge in _edges(block, "incoming_edges")
            for source in (getattr(edge, "source", None),)
            if source is not None
        )
    return blocks if start_key in blocks else None


def _guard_done_stores(instructions):
    stores = {}
    for instruction in instructions:
        if (
            _operation_name(instruction) not in _MLIL_STORE_OPERATIONS
            or getattr(instruction, "size", None) != 1
        ):
            continue
        address = _static_pointer_address(getattr(instruction, "dest", None))
        if address is None or _constant_value(getattr(instruction, "src", None)) != 1:
            continue
        stores.setdefault(address, []).append(instruction)
    return stores


def _guarded_initializer_candidates(layout):
    instructions, _blocks, block_by_index, _instruction_by_index = layout
    done_stores = _guard_done_stores(instructions)
    seen = set()
    static_definitions = _unique_static_flag_definitions(instructions, done_stores)
    for instruction in instructions:
        flag = _guard_flag_address(instruction, static_definitions)
        if flag is None or flag not in done_stores:
            continue
        for arm in ("true", "false"):
            start_index = getattr(instruction, arm, None)
            if type(start_index) is not int or start_index < 0:
                continue
            start = block_by_index.get(start_index)
            if start is None:
                continue
            stop_blocks = tuple(
                block
                for store in done_stores[flag]
                for block in (getattr(store, "il_basic_block", None),)
                if block is not None
            )
            reachable = _reachable_blocks(start, stop_blocks)
            if reachable is None:
                continue
            stops = [
                store
                for store in done_stores[flag]
                if _entity_identity(getattr(store, "il_basic_block", None)) in reachable
            ]
            if len(stops) != 1:
                continue
            stop = stops[0]
            stop_index = _instruction_index(stop)
            stop_block = getattr(stop, "il_basic_block", None)
            if stop_index is None or stop_block is None:
                continue
            blocks = _path_blocks(start, stop_block, reachable)
            if blocks is None:
                continue
            allowed = frozenset(
                index
                for block in blocks.values()
                for candidate in _block_instructions(block)
                for index in (_instruction_index(candidate),)
                if index is not None
            )
            key = flag, start_index, stop_index
            if not allowed or key in seen:
                continue
            seen.add(key)
            yield flag, start_index, stop_index, tuple(blocks.values()), allowed


def _static_write_sites(blocks):
    sites = {}
    for block in blocks:
        for instruction in _block_instructions(block):
            if (
                _operation_name(instruction) not in _MLIL_STORE_OPERATIONS
                or getattr(instruction, "size", None) != 1
            ):
                continue
            address = _static_pointer_address(getattr(instruction, "dest", None))
            if address is None:
                continue
            sites[address] = None if address in sites else instruction
    return sites


def _guarded_initializer_tail_stop(blocks, marker_index):
    for block in blocks:
        instructions = _block_instructions(block)
        if not any(_instruction_index(instruction) == marker_index for instruction in instructions):
            continue
        return max(
            (
                instruction_index
                for instruction in instructions
                for instruction_index in (_instruction_index(instruction),)
                if (
                    instruction_index is not None
                    and instruction_index >= marker_index
                    and _operation_name(instruction) in _MLIL_STORE_OPERATIONS
                    and getattr(instruction, "size", None) == 1
                    and _static_pointer_address(getattr(instruction, "dest", None)) is not None
                )
            ),
            default=marker_index,
        )
    return marker_index


def _guarded_static_plaintexts(machine, sites, flag):
    for destination in sorted(sites):
        site = sites[destination]
        if site is None or destination == flag or destination - 1 in sites:
            continue
        for offset in range(_STRING_BYTES_LIMIT):
            address = destination + offset
            if address == flag or sites.get(address) is None:
                break
            value = machine.memory.writes.get(address)
            if type(value) is not int:
                break
            if value == 0:
                plaintext = _textual_plaintext(machine.memory, destination)
                if plaintext is not None:
                    yield site, destination, plaintext
                break


def _recover_guarded_static_strings(query):
    layout = _loop_layout(query.mlil)
    if layout is None:
        return ()
    machine = None
    facts = []
    for flag, start, stop, blocks, allowed in _guarded_initializer_candidates(layout):
        if machine is None:
            machine = _ConcreteMLIL(query.view, query.mlil)
            if not machine.valid:
                return tuple(facts)
        if not machine.reset():
            continue
        completed, exit_index = machine.run(
            start,
            allowed,
            _guarded_initializer_tail_stop(blocks, stop),
        )
        if not completed or exit_index is not None:
            continue
        sites = _static_write_sites(blocks)
        for site, destination, plaintext in _guarded_static_plaintexts(
            machine,
            sites,
            flag,
        ):
            address = getattr(site, "address", None)
            if type(address) is not int or address < 0:
                continue
            sources = tuple(sorted(machine.memory.view_reads))
            if not sources:
                sources = _llil_block_static_loads(query.function, address)
            if sources:
                facts.append(StringRecoveryFact(address, sources[0], destination, plaintext))
    return tuple(facts)


def string_recovery(query):
    facts = list(_recover_direct_call_strings(query))
    facts.extend(_recover_inline_loop_strings(query))
    facts.extend(_recover_static_initializer_strings(query))
    facts.extend(_recover_guarded_static_strings(query))
    by_call = {}
    for fact in facts:
        existing = by_call.get(fact.call_addr)
        by_call[fact.call_addr] = fact if existing is None else existing if existing == fact else False
    return CompleteBatch(
        tuple(
            fact
            for _call, fact in sorted(by_call.items())
            if fact is not False
        )
    )


provider = SampleSemantics(
    provider_id="valorant-emdqx-0927cb886ad9a706",
    name="Valorant emdqx automatic collector",
    api_version=4,
    branch_targets=branch_targets,
    call_targets=call_targets,
    global_data=global_data,
    string_recovery=string_recovery,
)


register_provider(provider)
