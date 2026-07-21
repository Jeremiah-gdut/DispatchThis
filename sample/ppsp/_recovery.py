from __future__ import annotations

from dataclasses import dataclass

from DispatchThis import (
    AnalysisBudget,
    BranchTargetFact,
    BranchTargetQuery,
    CompleteBatch,
    CompleteValues,
    Inconclusive,
    evaluate_values,
)
from DispatchThis.helpers import memory


_VALUE_BUDGET = AnalysisBudget(node_limit=128, edge_limit=256)
_CONSTANT_DESTINATIONS = frozenset(("LLIL_CONST", "LLIL_CONST_PTR"))
_TERMINAL_TAIL_OPERATIONS = frozenset(("LLIL_SET_REG", "LLIL_STORE"))
_BOOLEAN_SELECTOR_COMPARISONS = frozenset(("LLIL_CMP_E", "LLIL_CMP_SLE"))
_FROZEN_FRAME_INTRINSICS = frozenset(
    (
        "_ReadMSR",
        "vceqq_u32",
        "vdupq_laneq_s32",
        "vdupq_laneq_s64",
        "vmlaq_lane_s32",
        "vmovn_s32",
        "vmulq_lane_s32",
        "vmulq_s32",
        "vnegq_s32",
        "vnegq_s64",
        "vorrq_s8",
        "vuzp1_s8",
    )
)


@dataclass(frozen=True, slots=True)
class _StackOffset:
    offset: int


@dataclass(frozen=True, slots=True)
class _PrivateFrozenFrameBase:
    register: object
    ssa_register: object | None


def _operation_name(instruction) -> str:
    return instruction.operation.name


def _indirect_jumps(llil):
    return tuple(
        instruction
        for instruction in llil.instructions
        if _operation_name(instruction) in ("LLIL_JUMP", "LLIL_JUMP_TO")
        and getattr(instruction, "dest", None) is not None
        and _operation_name(instruction.dest) not in _CONSTANT_DESTINATIONS
    )


def _instruction_snapshot(llil):
    instructions = {}
    duplicates = set()
    for instruction in llil.instructions:
        instruction_index = getattr(instruction, "instr_index", None)
        if type(instruction_index) is not int or instruction_index < 0:
            continue
        if instruction_index in instructions:
            duplicates.add(instruction_index)
        else:
            instructions[instruction_index] = instruction
    for instruction_index in duplicates:
        del instructions[instruction_index]
    return instructions


def _single_executable_target(view, llil_ssa, jump, policy) -> int | None:
    values = evaluate_values(view, llil_ssa, jump.ssa_form.dest, _VALUE_BUDGET, policy)
    if type(values) is not CompleteValues or len(values.values) != 1:
        return None
    target = values.values[0]
    if type(target) is not int or not memory.is_executable_target(view, target):
        return None
    return target


def _register_index(register) -> int | None:
    index = getattr(register, "index", None)
    if type(index) is not int or index < 0:
        return None
    return index


def _modified_register_indexes(architecture, register) -> tuple[int, ...] | None:
    name = getattr(register, "name", None)
    if type(name) is not str:
        return None
    names = architecture.get_modified_regs_on_write(name)
    if not isinstance(names, (list, tuple)):
        return None
    indexes = []
    for modified_name in names:
        index = architecture.get_reg_index(modified_name)
        if type(index) is not int or index < 0:
            return None
        indexes.append(index)
    return tuple(indexes)


def _same_register(left, right) -> bool:
    return left is right or left == right


def _is_stack_register(architecture, register) -> bool:
    register_index = _register_index(register)
    stack_pointer = getattr(architecture, "stack_pointer", None)
    stack_index = architecture.get_reg_index(stack_pointer)
    return (
        type(register_index) is int
        and type(stack_index) is int
        and register_index == stack_index
    )


def _read_register(architecture, registers, register):
    for known_register, value in reversed(registers):
        if _same_register(known_register, register):
            return value
    if _is_stack_register(architecture, register):
        return _StackOffset(0)
    return None


def _write_register(architecture, registers, register, value) -> bool:
    modified = _modified_register_indexes(architecture, register)
    if modified is None:
        return False
    filtered = []
    for known_register, known_value in registers:
        index = _register_index(known_register)
        if index is None:
            return False
        if index not in modified:
            filtered.append((known_register, known_value))
    registers[:] = filtered
    if value is not None:
        registers.append((register, value))
    return True


def _forget_non_stack_registers(registers) -> None:
    registers[:] = [
        (register, value)
        for register, value in registers
        if isinstance(value, _StackOffset)
    ]


def _mask(value: int, size) -> int | None:
    if type(size) is not int or size <= 0:
        return None
    return value & ((1 << (size * 8)) - 1)


def _add(left, right, size):
    if type(left) is int and type(right) is int:
        return _mask(left + right, size)
    if isinstance(left, _StackOffset) and type(right) is int:
        return _StackOffset(left.offset + right)
    if type(left) is int and isinstance(right, _StackOffset):
        return _StackOffset(left + right.offset)
    return None


def _subtract(left, right, size):
    if type(left) is int and type(right) is int:
        return _mask(left - right, size)
    if isinstance(left, _StackOffset) and type(right) is int:
        return _StackOffset(left.offset - right)
    return None


def _binary_operands(expression):
    operands = getattr(expression, "operands", None)
    if not isinstance(operands, (list, tuple)) or len(operands) != 2:
        return None
    return operands


def _forget_overlapping_stack_values(stack_values, offset: int, size: int) -> bool:
    end = offset + size
    if end <= offset:
        return False
    retained = []
    for known_offset, known_size, known_value in stack_values:
        known_end = known_offset + known_size
        if known_end <= known_offset:
            return False
        if end <= known_offset or known_end <= offset:
            retained.append((known_offset, known_size, known_value))
    stack_values[:] = retained
    return True


def _write_stack_value(stack_values, offset: int, size: int, value) -> bool:
    if not _forget_overlapping_stack_values(stack_values, offset, size):
        return False
    if type(value) is int:
        masked = _mask(value, size)
        if masked is None:
            return False
        stack_values.append((offset, size, masked))
    return True


def _read_stack_value(stack_values, offset: int, size: int) -> int | None:
    for known_offset, known_size, known_value in reversed(stack_values):
        if known_offset == offset and known_size == size:
            return known_value
    return None


def _evaluate_literal(architecture, expression, registers, stack_values):
    operation = _operation_name(expression)
    size = getattr(expression, "size", None)
    if operation in _CONSTANT_DESTINATIONS:
        value = getattr(expression, "constant", None)
        return _mask(value, size) if type(value) is int else None
    if operation == "LLIL_REG":
        return _read_register(architecture, registers, getattr(expression, "src", None))
    if operation == "LLIL_LOAD":
        address = _evaluate_literal(
            architecture, getattr(expression, "src", None), registers, stack_values
        )
        if not isinstance(address, _StackOffset):
            return None
        return _read_stack_value(stack_values, address.offset, size)
    if operation in ("LLIL_ADD", "LLIL_SUB", "LLIL_MUL", "LLIL_AND", "LLIL_OR"):
        operands = _binary_operands(expression)
        if operands is None:
            return None
        left = _evaluate_literal(architecture, operands[0], registers, stack_values)
        right = _evaluate_literal(architecture, operands[1], registers, stack_values)
        if operation == "LLIL_ADD":
            return _add(left, right, size)
        if operation == "LLIL_SUB":
            return _subtract(left, right, size)
        if type(left) is not int or type(right) is not int:
            return None
        if operation == "LLIL_MUL":
            return _mask(left * right, size)
        if operation == "LLIL_AND":
            return _mask(left & right, size)
        return _mask(left | right, size)
    if operation == "LLIL_SX":
        source = getattr(expression, "src", None)
        value = _evaluate_literal(architecture, source, registers, stack_values)
        source_size = getattr(source, "size", None)
        if type(value) is not int or type(source_size) is not int or source_size <= 0:
            return None
        bits = source_size * 8
        value &= (1 << bits) - 1
        if value & (1 << (bits - 1)):
            value -= 1 << bits
        return _mask(value, size)
    return None


def _is_direct_no_argument_call(instruction) -> bool:
    destination = getattr(instruction, "dest", None)
    if _operation_name(destination) not in _CONSTANT_DESTINATIONS:
        return False
    params = getattr(instruction, "params", None)
    return params is None or len(params) == 0


def _is_vector_lane_duplicate(instruction) -> bool:
    intrinsic = getattr(instruction, "intrinsic", None)
    params = getattr(instruction, "params", None)
    return (
        getattr(intrinsic, "name", None) == "vdupq_laneq_s32"
        and isinstance(params, (list, tuple))
        and len(params) == 2
        and _operation_name(params[0]) == "LLIL_REG"
        and _operation_name(params[1]) in _CONSTANT_DESTINATIONS
    )


def _step_literal(architecture, instruction, registers, stack_values) -> bool:
    operation = _operation_name(instruction)
    if operation == "LLIL_SET_REG":
        value = _evaluate_literal(
            architecture, getattr(instruction, "src", None), registers, stack_values
        )
        return _write_register(
            architecture, registers, getattr(instruction, "dest", None), value
        )
    if operation == "LLIL_STORE":
        size = getattr(instruction, "size", None)
        if type(size) is not int or size <= 0:
            return False
        address = _evaluate_literal(
            architecture, getattr(instruction, "dest", None), registers, stack_values
        )
        value = _evaluate_literal(
            architecture, getattr(instruction, "src", None), registers, stack_values
        )
        if isinstance(address, _StackOffset):
            return _write_stack_value(stack_values, address.offset, size, value)
        if address is None or isinstance(value, _StackOffset):
            stack_values.clear()
        return True
    if operation == "LLIL_CALL":
        if not _is_direct_no_argument_call(instruction):
            stack_values.clear()
        _forget_non_stack_registers(registers)
        return True
    if operation == "LLIL_INTRINSIC":
        if _is_vector_lane_duplicate(instruction):
            _forget_non_stack_registers(registers)
            return True
        stack_values.clear()
        _forget_non_stack_registers(registers)
        return True
    return False


def _register_expression(expression):
    if _operation_name(expression) != "LLIL_REG":
        return None
    register = getattr(expression, "src", None)
    return register if _register_index(register) is not None else None


def _constant_value(expression) -> int | None:
    if _operation_name(expression) not in _CONSTANT_DESTINATIONS:
        return None
    value = getattr(expression, "constant", None)
    return value if type(value) is int else None


def _set_register_at(llil, instruction_index: int, instructions_by_index=None):
    instruction = _current_instruction(llil, instruction_index, instructions_by_index)
    if (
        instruction is None
        or _operation_name(instruction) != "LLIL_SET_REG"
        or _register_index(getattr(instruction, "dest", None)) is None
    ):
        return None
    return instruction


def _blocks_by_instruction(llil):
    blocks = {}
    for block in llil.basic_blocks:
        start = getattr(block, "start", None)
        end = getattr(block, "end", None)
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            continue
        for instruction_index in range(start, end):
            blocks.setdefault(instruction_index, block)
    return blocks


def _jump_targets(
    llil, instruction, blocks_by_instruction=None, instructions_by_index=None
):
    targets = getattr(instruction, "targets", None)
    if type(targets) is not dict or not targets:
        return None
    resolved = []
    for target_address, target_index in targets.items():
        target = _current_instruction(llil, target_index, instructions_by_index)
        target_block = _current_block(llil, target_index, blocks_by_instruction)
        if not (
            type(target_address) is int
            and target_address >= 0
            and type(target_index) is int
            and target is not None
            and target_block is not None
            and target_block.start == target_index
        ):
            return None
        resolved.append(target_index)
    return tuple(resolved)


def _jump_predecessors(llil, blocks_by_instruction, instructions_by_index=None):
    predecessors = {}
    for instruction_index, block in blocks_by_instruction.items():
        start = getattr(block, "start", None)
        end = getattr(block, "end", None)
        if instruction_index != start or type(end) is not int or end <= start:
            continue
        terminal = _current_instruction(llil, end - 1, instructions_by_index)
        if terminal is None or _operation_name(terminal) != "LLIL_JUMP_TO":
            continue
        targets = _jump_targets(
            llil, terminal, blocks_by_instruction, instructions_by_index
        )
        if targets is None:
            continue
        for target in targets:
            predecessors.setdefault(target, set()).add(start)
    return predecessors


def _reaching_starts(predecessors, stop_block_start: int):
    reachable = {stop_block_start}
    pending = [stop_block_start]
    while pending:
        target = pending.pop()
        for source in predecessors.get(target, ()):
            if source not in reachable:
                reachable.add(source)
                pending.append(source)
    return reachable


def _reaches_stop(
    llil,
    block_start: int,
    stop_block_start: int,
    seen,
    blocks_by_instruction=None,
    predecessors=None,
    reachable_by_stop=None,
    instructions_by_index=None,
) -> bool:
    if blocks_by_instruction is None:
        blocks_by_instruction = _blocks_by_instruction(llil)
    if not seen and predecessors is not None and reachable_by_stop is not None:
        if stop_block_start not in reachable_by_stop:
            reachable_by_stop[stop_block_start] = _reaching_starts(
                predecessors, stop_block_start
            )
        return block_start in reachable_by_stop[stop_block_start]
    pending = [block_start]
    visited = set(seen)
    while pending:
        current_start = pending.pop()
        if current_start == stop_block_start:
            return True
        if current_start in visited:
            continue
        visited.add(current_start)
        block = _current_block(llil, current_start, blocks_by_instruction)
        if block is None or block.start != current_start or block.end <= block.start:
            continue
        terminal = _current_instruction(llil, block.end - 1, instructions_by_index)
        if terminal is None or _operation_name(terminal) != "LLIL_JUMP_TO":
            continue
        targets = _jump_targets(
            llil, terminal, blocks_by_instruction, instructions_by_index
        )
        if targets is not None:
            pending.extend(targets)
    return False


def _jump_target_for_stop(
    llil,
    instruction,
    stop_block_start: int,
    blocks_by_instruction=None,
    predecessors=None,
    reachable_by_stop=None,
    instructions_by_index=None,
) -> int | None:
    targets = _jump_targets(
        llil, instruction, blocks_by_instruction, instructions_by_index
    )
    if targets is None:
        return None
    candidates = [
        target
        for target in targets
        if _reaches_stop(
            llil,
            target,
            stop_block_start,
            set(),
            blocks_by_instruction,
            predecessors,
            reachable_by_stop,
            instructions_by_index,
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def _route_prefix_state(view, llil, instructions_by_index):
    architecture = getattr(view, "arch", None)
    if architecture is None:
        return None
    registers = []
    stack_values = []
    for instruction_index, instruction in enumerate(llil.instructions):
        if (
            getattr(instruction, "instr_index", None) != instruction_index
            or instructions_by_index.get(instruction_index) is None
        ):
            return None
        if _operation_name(instruction) == "LLIL_JUMP_TO":
            return instruction_index, tuple(registers), tuple(stack_values)
        if not _step_literal(architecture, instruction, registers, stack_values):
            return None
    return None


def _path_state_before(
    view,
    llil,
    instruction_index: int,
    blocks_by_instruction=None,
    predecessors=None,
    reachable_by_stop=None,
    instructions_by_index=None,
    prefix_state=None,
):
    stop_block = _current_block(llil, instruction_index, blocks_by_instruction)
    if stop_block is None:
        return None
    registers = []
    stack_values = []
    current_index = 0
    if (
        isinstance(prefix_state, tuple)
        and len(prefix_state) == 3
        and type(prefix_state[0]) is int
        and 0 <= prefix_state[0] <= instruction_index
        and isinstance(prefix_state[1], tuple)
        and isinstance(prefix_state[2], tuple)
    ):
        current_index = prefix_state[0]
        registers = list(prefix_state[1])
        stack_values = list(prefix_state[2])
    while current_index < instruction_index:
        instruction = _current_instruction(llil, current_index, instructions_by_index)
        if instruction is None:
            return None
        if _operation_name(instruction) == "LLIL_JUMP_TO":
            target_index = _jump_target_for_stop(
                llil,
                instruction,
                stop_block.start,
                blocks_by_instruction,
                predecessors,
                reachable_by_stop,
                instructions_by_index,
            )
            if target_index is None or target_index <= current_index:
                return None
            current_index = target_index
        elif not _step_literal(view.arch, instruction, registers, stack_values):
            return None
        else:
            current_index += 1
    return registers, stack_values


def _read_static_target(view, policy, address: int) -> int | None:
    address_size = getattr(view.arch, "address_size", None)
    byte_order = getattr(policy, "byte_order", None)
    if type(address_size) is not int or address_size <= 0:
        return None
    if byte_order not in ("little", "big"):
        return None
    data = policy.bytes_at(address, address_size)
    if type(data) is not bytes or len(data) != address_size:
        return None
    target = int.from_bytes(data, byte_order)
    return target if memory.is_executable_target(view, target) else None


def _current_block(llil, instruction_index: int, blocks_by_instruction=None):
    if type(instruction_index) is not int or instruction_index < 0:
        return None
    if blocks_by_instruction is not None:
        return blocks_by_instruction.get(instruction_index)
    for block in llil.basic_blocks:
        start = getattr(block, "start", None)
        end = getattr(block, "end", None)
        if (
            type(start) is int
            and type(end) is int
            and start >= 0
            and start <= instruction_index < end
        ):
            return block
    return None


def _current_instruction(llil, instruction_index: int, instructions_by_index=None):
    if type(instruction_index) is not int or instruction_index < 0:
        return None
    if instructions_by_index is not None and instruction_index in instructions_by_index:
        instruction = instructions_by_index[instruction_index]
    else:
        instruction = llil[instruction_index]
    if getattr(instruction, "instr_index", None) != instruction_index:
        return None
    return instruction


def _last_target_write(
    llil, block, jump_index: int, target_register, instructions_by_index=None
):
    last_write = None
    for instruction_index in range(block.start, jump_index):
        instruction = _current_instruction(
            llil, instruction_index, instructions_by_index
        )
        if instruction is None:
            return None
        if _operation_name(instruction) == "LLIL_SET_REG" and _same_register(
            getattr(instruction, "dest", None), target_register
        ):
            last_write = instruction
    return last_write


def _target_is_preserved(
    architecture,
    llil,
    start: int,
    end: int,
    target_register,
    instructions_by_index=None,
) -> bool:
    for instruction_index in range(start, end):
        instruction = _current_instruction(
            llil, instruction_index, instructions_by_index
        )
        if instruction is None:
            return False
        operation = _operation_name(instruction)
        if operation not in _TERMINAL_TAIL_OPERATIONS:
            return False
        if operation != "LLIL_SET_REG":
            continue
        modified = _modified_register_indexes(
            architecture, getattr(instruction, "dest", None)
        )
        target_index = _register_index(target_register)
        if modified is None or target_index is None or target_index in modified:
            return False
    return True


def _literal_table_target(
    view, llil, jump, policy, blocks_by_instruction=None, instructions_by_index=None
) -> int | None:
    if _operation_name(jump.dest) != "LLIL_REG":
        return None
    jump_index = getattr(jump, "instr_index", None)
    if type(jump_index) is not int or jump_index < 0:
        return None
    block = _current_block(llil, jump_index, blocks_by_instruction)
    if block is None:
        return None
    target_register = getattr(jump.dest, "src", None)
    if _register_index(target_register) is None:
        return None
    table_write = _last_target_write(
        llil, block, jump_index, target_register, instructions_by_index
    )
    if table_write is None or _operation_name(table_write.src) != "LLIL_LOAD":
        return None
    table_load = table_write.src
    address_size = getattr(view.arch, "address_size", None)
    if (
        type(address_size) is not int
        or getattr(table_load, "size", None) != address_size
    ):
        return None
    registers = []
    stack_values = []
    for instruction_index in range(block.start, table_write.instr_index):
        instruction = _current_instruction(
            llil, instruction_index, instructions_by_index
        )
        if instruction is None or not _step_literal(
            view.arch, instruction, registers, stack_values
        ):
            return None
    table_address = _evaluate_literal(
        view.arch, getattr(table_load, "src", None), registers, stack_values
    )
    if type(table_address) is not int or not _target_is_preserved(
        view.arch,
        llil,
        table_write.instr_index + 1,
        jump_index,
        target_register,
        instructions_by_index,
    ):
        return None
    return _read_static_target(view, policy, table_address)


def _ranges_overlap(
    left_offset: int, left_size: int, right_offset: int, right_size: int
) -> bool:
    if (
        type(left_offset) is not int
        or type(left_size) is not int
        or type(right_offset) is not int
        or type(right_size) is not int
        or left_size <= 0
        or right_size <= 0
    ):
        return True
    left_end = left_offset + left_size
    right_end = right_offset + right_size
    if left_end <= left_offset or right_end <= right_offset:
        return True
    return not (left_end <= right_offset or right_end <= left_offset)


def _direct_stack_slot(expression):
    operation = _operation_name(expression)
    if operation == "LLIL_REG":
        register = _register_expression(expression)
        return None if register is None else (register, 0)
    operands = _binary_operands(expression)
    if operation == "LLIL_ADD" and operands is not None:
        left_register = _register_expression(operands[0])
        right_register = _register_expression(operands[1])
        if left_register is not None and type(_constant_value(operands[1])) is int:
            return left_register, _constant_value(operands[1])
        if right_register is not None and type(_constant_value(operands[0])) is int:
            return right_register, _constant_value(operands[0])
    if operation == "LLIL_SUB" and operands is not None:
        left_register = _register_expression(operands[0])
        if left_register is not None and type(_constant_value(operands[1])) is int:
            return left_register, -_constant_value(operands[1])
    return None


def _direct_stack_base(expression):
    slot = _direct_stack_slot(expression)
    return None if slot is None else slot[0]


def _ssa_register_base(register):
    base_register = getattr(register, "reg", None)
    version = getattr(register, "version", None)
    if (
        _register_index(base_register) is None
        or type(version) is not int
        or version < 0
    ):
        return None
    return base_register


def _ssa_direct_stack_base(expression):
    operation = getattr(getattr(expression, "operation", None), "name", None)
    if operation == "LLIL_REG_SSA":
        ssa_register = getattr(expression, "src", None)
        base_register = _ssa_register_base(ssa_register)
        return None if base_register is None else (base_register, ssa_register)
    operands = _binary_operands(expression)
    if operation == "LLIL_ADD" and operands is not None:
        left = _ssa_direct_stack_base(operands[0])
        right = _ssa_direct_stack_base(operands[1])
        if left is not None and type(_constant_value(operands[1])) is int:
            return left
        if right is not None and type(_constant_value(operands[0])) is int:
            return right
    if operation == "LLIL_SUB" and operands is not None:
        left = _ssa_direct_stack_base(operands[0])
        if left is not None and type(_constant_value(operands[1])) is int:
            return left
    return None


def _ssa_memory_access_address(instruction):
    operation = _operation_name(instruction)
    ssa_instruction = getattr(instruction, "ssa_form", None)
    if (
        ssa_instruction is None
        or getattr(ssa_instruction, "non_ssa_form", None) != instruction
    ):
        return None
    if operation == "LLIL_STORE":
        return (
            getattr(ssa_instruction, "dest", None)
            if _operation_name(ssa_instruction) == "LLIL_STORE_SSA"
            else None
        )
    if (
        operation != "LLIL_SET_REG"
        or _operation_name(ssa_instruction) != "LLIL_SET_REG_SSA"
    ):
        return None
    source = getattr(ssa_instruction, "src", None)
    while getattr(getattr(source, "operation", None), "name", None) in (
        "LLIL_LOW_PART",
        "LLIL_SX",
        "LLIL_ZX",
    ):
        next_source = getattr(source, "src", None)
        if next_source is source:
            return None
        source = next_source
    if getattr(getattr(source, "operation", None), "name", None) != "LLIL_LOAD_SSA":
        return None
    return getattr(source, "src", None)


def _private_frame_access_uses_anchor(
    instruction, base_register, anchor_register
) -> bool:
    address = _ssa_memory_access_address(instruction)
    ssa_base = _ssa_direct_stack_base(address)
    anchor_base = _ssa_register_base(anchor_register)
    if ssa_base is None or anchor_base is None:
        return False
    ssa_base_register, access_register = ssa_base
    return (
        _same_register(base_register, ssa_base_register)
        and _same_register(base_register, anchor_base)
        and _same_register(access_register, anchor_register)
    )


def _private_frame_prefix_ssa_register(
    view, base_register, prefix_state, instructions_by_index
):
    if not (
        isinstance(prefix_state, tuple)
        and len(prefix_state) == 3
        and type(prefix_state[0]) is int
        and prefix_state[0] >= 0
    ):
        return None
    architecture = getattr(view, "arch", None)
    base_index = _register_index(base_register)
    if architecture is None or base_index is None:
        return None
    anchor_register = None
    for instruction_index in range(prefix_state[0]):
        instruction = instructions_by_index.get(instruction_index)
        if getattr(instruction, "instr_index", None) != instruction_index:
            return None
        if _operation_name(instruction) != "LLIL_SET_REG":
            continue
        modified = _modified_register_indexes(
            architecture, getattr(instruction, "dest", None)
        )
        if modified is None:
            return None
        if base_index not in modified:
            continue
        ssa_instruction = getattr(instruction, "ssa_form", None)
        if (
            ssa_instruction is None
            or _operation_name(ssa_instruction) != "LLIL_SET_REG_SSA"
            or getattr(ssa_instruction, "non_ssa_form", None) != instruction
        ):
            return None
        candidate = getattr(ssa_instruction, "dest", None)
        candidate_base = _ssa_register_base(candidate)
        if candidate_base is None or not _same_register(candidate_base, base_register):
            return None
        anchor_register = candidate
    return anchor_register


def _private_frame_base_uses_current_anchor(
    instruction, base_register, private_frame_bases
) -> bool:
    for frame_base in private_frame_bases:
        if not _same_register(base_register, frame_base.register):
            continue
        anchor_register = frame_base.ssa_register
        return anchor_register is None or _private_frame_access_uses_anchor(
            instruction, base_register, anchor_register
        )
    return False


def _frozen_prefix_writes(view, prefix_state, instructions_by_index):
    if not (
        isinstance(prefix_state, tuple)
        and len(prefix_state) == 3
        and type(prefix_state[0]) is int
        and isinstance(prefix_state[1], tuple)
        and isinstance(prefix_state[2], tuple)
    ):
        return None
    prefix_end, registers, stack_values = prefix_state
    writes = []
    for instruction_index in sorted(instructions_by_index):
        if instruction_index <= prefix_end:
            continue
        instruction = instructions_by_index[instruction_index]
        operation = _operation_name(instruction)
        if operation == "LLIL_STORE":
            size = getattr(instruction, "size", None)
            address = _evaluate_literal(
                view.arch, getattr(instruction, "dest", None), registers, stack_values
            )
            if (
                not isinstance(address, _StackOffset)
                or type(size) is not int
                or size <= 0
            ):
                return None
            writes.append((address.offset, size))
        elif operation == "LLIL_CALL" and not _is_direct_no_argument_call(instruction):
            return None
        elif (
            operation == "LLIL_INTRINSIC"
            and getattr(getattr(instruction, "intrinsic", None), "name", None)
            not in _FROZEN_FRAME_INTRINSICS
        ):
            return None
    return tuple(writes)


def _frozen_frame_base_is_stable(
    view, base_register, prefix_state, instructions_by_index
) -> bool:
    if not isinstance(prefix_state, tuple) or len(prefix_state) != 3:
        return False
    prefix_end = prefix_state[0]
    base_index = _register_index(base_register)
    if type(prefix_end) is not int or base_index is None:
        return False
    for instruction_index in sorted(instructions_by_index):
        if instruction_index <= prefix_end:
            continue
        instruction = instructions_by_index[instruction_index]
        if _operation_name(instruction) != "LLIL_SET_REG":
            continue
        modified = _modified_register_indexes(
            view.arch, getattr(instruction, "dest", None)
        )
        if modified is None or base_index in modified:
            return False
    return True


def _cached_frozen_frame_base_is_stable(
    cache, view, base_register, prefix_state, instructions_by_index
) -> bool:
    for known_register, result in cache:
        if _same_register(known_register, base_register):
            return result
    result = _frozen_frame_base_is_stable(
        view, base_register, prefix_state, instructions_by_index
    )
    cache.append((base_register, result))
    return result


def _expression_children(expression):
    source = getattr(expression, "src", None)
    if getattr(source, "operation", None) is not None:
        yield source
    operands = getattr(expression, "operands", None)
    if isinstance(operands, (list, tuple)):
        for operand in operands:
            if getattr(operand, "operation", None) is not None:
                yield operand


def _uses_frame_register(expression, frame_registers) -> bool:
    operation = getattr(getattr(expression, "operation", None), "name", None)
    if operation is None:
        return False
    if operation == "LLIL_REG":
        register = _register_expression(expression)
        return register is not None and any(
            _same_register(register, frame_register)
            for frame_register in frame_registers
        )
    return any(
        _uses_frame_register(child, frame_registers)
        for child in _expression_children(expression)
    )


def _frame_registers_only_read_by_loads(expression, frame_registers) -> bool:
    operation = getattr(getattr(expression, "operation", None), "name", None)
    if operation is None:
        return True
    if operation == "LLIL_REG":
        return not _uses_frame_register(expression, frame_registers)
    if operation == "LLIL_LOAD":
        return True
    return all(
        _frame_registers_only_read_by_loads(child, frame_registers)
        for child in _expression_children(expression)
    )


def _private_frozen_frame_base_writes(
    view, base_register, prefix_state, instructions_by_index
):
    prefix_end = prefix_state[0]
    registers = prefix_state[1]
    stack_values = prefix_state[2]
    writes = []
    for instruction_index in sorted(instructions_by_index):
        if instruction_index <= prefix_end:
            continue
        instruction = instructions_by_index[instruction_index]
        operation = _operation_name(instruction)
        source = getattr(instruction, "src", None)
        destination = getattr(instruction, "dest", None)
        params = getattr(instruction, "params", None)
        parameters = params if isinstance(params, (list, tuple)) else ()
        if operation == "LLIL_SET_REG":
            if not _frame_registers_only_read_by_loads(source, (base_register,)):
                return None
            continue
        if operation == "LLIL_STORE":
            if not _frame_registers_only_read_by_loads(source, (base_register,)):
                return None
            if not _uses_frame_register(destination, (base_register,)):
                continue
            size = getattr(instruction, "size", None)
            address = _evaluate_literal(view.arch, destination, registers, stack_values)
            if (
                not isinstance(address, _StackOffset)
                or type(size) is not int
                or size <= 0
            ):
                return None
            writes.append((address.offset, size))
            continue
        if operation == "LLIL_INTRINSIC":
            if (
                getattr(getattr(instruction, "intrinsic", None), "name", None)
                not in _FROZEN_FRAME_INTRINSICS
            ):
                return None
        expressions = (source, destination, *parameters)
        if not all(
            _frame_registers_only_read_by_loads(expression, (base_register,))
            for expression in expressions
        ):
            return None
    return tuple(writes)


def _private_frozen_frame_writes(view, prefix_state, instructions_by_index):
    if not (
        isinstance(prefix_state, tuple)
        and len(prefix_state) == 3
        and type(prefix_state[0]) is int
        and isinstance(prefix_state[1], tuple)
        and isinstance(prefix_state[2], tuple)
    ):
        return None
    architecture = getattr(view, "arch", None)
    stack_pointer = getattr(architecture, "stack_pointer", None)
    stack_index = (
        None
        if architecture is None or stack_pointer is None
        else architecture.get_reg_index(stack_pointer)
    )
    if type(stack_index) is not int:
        return None
    _prefix_end, registers, _stack_values = prefix_state
    frame_bases = []
    writes = []
    for register, value in registers:
        register_index = _register_index(register)
        if not isinstance(value, _StackOffset) or register_index is None:
            continue
        if register_index == stack_index:
            continue
        base_is_stable = _frozen_frame_base_is_stable(
            view, register, prefix_state, instructions_by_index
        )
        anchor_register = (
            None
            if base_is_stable
            else _private_frame_prefix_ssa_register(
                view, register, prefix_state, instructions_by_index
            )
        )
        if not base_is_stable and anchor_register is None:
            continue
        base_writes = _private_frozen_frame_base_writes(
            view, register, prefix_state, instructions_by_index
        )
        if base_writes is None:
            continue
        if any(_same_register(register, known.register) for known in frame_bases):
            continue
        frame_bases.append(_PrivateFrozenFrameBase(register, anchor_register))
        for write in base_writes:
            if write not in writes:
                writes.append(write)
    if not frame_bases:
        return None
    return tuple(frame_bases), tuple(writes)


def _frozen_prefix_load(
    view,
    address_expression,
    size,
    prefix_state,
    frozen_writes,
):
    if (
        type(size) is not int
        or size <= 0
        or not isinstance(prefix_state, tuple)
        or len(prefix_state) != 3
        or frozen_writes is None
    ):
        return None
    registers = prefix_state[1]
    stack_values = prefix_state[2]
    address = _evaluate_literal(view.arch, address_expression, registers, stack_values)
    if not isinstance(address, _StackOffset):
        return None
    for write_offset, write_size in frozen_writes:
        if _ranges_overlap(address.offset, size, write_offset, write_size):
            return None
    return _read_stack_value(stack_values, address.offset, size)


def _frozen_frame_table_target(
    view,
    llil,
    jump,
    policy,
    prefix_state,
    frozen_writes,
    frozen_base_cache,
    blocks_by_instruction=None,
    instructions_by_index=None,
    private_frame_bases=None,
):
    jump_index = getattr(jump, "instr_index", None)
    target_register = _register_expression(getattr(jump, "dest", None))
    architecture = getattr(view, "arch", None)
    address_size = getattr(architecture, "address_size", None)
    if (
        type(jump_index) is not int
        or jump_index < 4
        or target_register is None
        or type(address_size) is not int
        or address_size <= 0
        or frozen_writes is None
    ):
        return None
    block = _current_block(llil, jump_index, blocks_by_instruction)
    target_load = _set_register_at(llil, jump_index - 1, instructions_by_index)
    indexed_write = _set_register_at(llil, jump_index - 2, instructions_by_index)
    stride_write = _set_register_at(llil, jump_index - 3, instructions_by_index)
    index_extend = _set_register_at(llil, jump_index - 4, instructions_by_index)
    if (
        block is None
        or any(
            instruction is None
            for instruction in (target_load, indexed_write, stride_write, index_extend)
        )
        or not _same_register(target_load.dest, target_register)
        or not _same_register(indexed_write.dest, target_register)
    ):
        return None
    target_load_expression = getattr(target_load, "src", None)
    indexed_expression = getattr(indexed_write, "src", None)
    if (
        target_load_expression is None
        or _operation_name(target_load_expression) != "LLIL_LOAD"
        or getattr(target_load_expression, "size", None) != address_size
        or not _same_register(
            _register_expression(getattr(target_load_expression, "src", None)),
            target_register,
        )
        or indexed_expression is None
        or _operation_name(indexed_expression) != "LLIL_ADD"
    ):
        return None
    add_operands = _binary_operands(indexed_expression)
    if add_operands is None or _operation_name(add_operands[1]) != "LLIL_MUL":
        return None
    table_register = _register_expression(add_operands[0])
    multiply_operands = _binary_operands(add_operands[1])
    if table_register is None or multiply_operands is None:
        return None
    index_register = _register_expression(multiply_operands[0])
    stride_register = _register_expression(multiply_operands[1])
    index_extend_expression = getattr(index_extend, "src", None)
    if (
        index_register is None
        or stride_register is None
        or not _same_register(stride_write.dest, stride_register)
        or _constant_value(stride_write.src) != address_size
        or not _same_register(index_extend.dest, index_register)
        or index_extend_expression is None
        or _operation_name(index_extend_expression) != "LLIL_SX"
    ):
        return None
    index_low_register = _register_expression(
        getattr(index_extend_expression, "src", None)
    )
    if index_low_register is None:
        return None
    index_load = _last_target_write(
        llil,
        block,
        index_extend.instr_index,
        index_low_register,
        instructions_by_index,
    )
    table_load = _last_target_write(
        llil,
        block,
        indexed_write.instr_index,
        table_register,
        instructions_by_index,
    )
    index_load_expression = (
        None if index_load is None else getattr(index_load, "src", None)
    )
    table_load_expression = (
        None if table_load is None else getattr(table_load, "src", None)
    )
    if (
        index_load_expression is None
        or table_load_expression is None
        or _operation_name(index_load_expression) != "LLIL_LOAD"
        or _operation_name(table_load_expression) != "LLIL_LOAD"
        or getattr(table_load_expression, "size", None) != address_size
        or not _target_is_preserved(
            view.arch,
            llil,
            indexed_write.instr_index + 1,
            target_load.instr_index,
            target_register,
            instructions_by_index,
        )
    ):
        return None
    index_address_expression = getattr(index_load_expression, "src", None)
    table_address_expression = getattr(table_load_expression, "src", None)
    index_base = _direct_stack_base(index_address_expression)
    table_base = _direct_stack_base(table_address_expression)
    if (
        index_base is None
        or table_base is None
        or not _same_register(index_base, table_base)
    ):
        return None
    if private_frame_bases is None:
        if not _cached_frozen_frame_base_is_stable(
            frozen_base_cache,
            view,
            index_base,
            prefix_state,
            instructions_by_index,
        ):
            return None
    elif not (
        _private_frame_base_uses_current_anchor(
            index_load, index_base, private_frame_bases
        )
        and _private_frame_base_uses_current_anchor(
            table_load, table_base, private_frame_bases
        )
    ):
        return None
    index_size = getattr(index_load_expression, "size", None)
    index = _frozen_prefix_load(
        view,
        index_address_expression,
        index_size,
        prefix_state,
        frozen_writes,
    )
    table_address = _frozen_prefix_load(
        view,
        table_address_expression,
        address_size,
        prefix_state,
        frozen_writes,
    )
    if (
        type(index) is not int
        or type(table_address) is not int
        or type(index_size) is not int
        or index_size <= 0
    ):
        return None
    index_bits = index_size * 8
    index &= (1 << index_bits) - 1
    if index & (1 << (index_bits - 1)):
        index -= 1 << index_bits
    entry_address = table_address + index * address_size
    pointer_limit = 1 << (address_size * 8)
    if entry_address < 0 or entry_address >= pointer_limit:
        return None
    return _read_static_target(view, policy, entry_address)


def _selector_table_entries(view, policy, table_base: int) -> tuple[int, ...] | None:
    address_size = getattr(getattr(view, "arch", None), "address_size", None)
    if (
        type(table_base) is not int
        or table_base < 0
        or type(address_size) is not int
        or address_size <= 0
    ):
        return None
    pointer_limit = 1 << (address_size * 8)
    second_slot = table_base + address_size
    table_end = second_slot + address_size
    if (
        table_base >= pointer_limit
        or second_slot <= table_base
        or table_end <= second_slot
        or table_end > pointer_limit
    ):
        return None
    first_target = _read_static_target(view, policy, table_base)
    second_target = _read_static_target(view, policy, second_slot)
    if first_target is None or second_target is None:
        return None
    return first_target, second_target


def _selector_store_reaches_index_load(index_load, index_store) -> bool:
    load_ssa = getattr(index_load, "ssa_form", None)
    store_ssa = getattr(index_store, "ssa_form", None)
    if (
        _operation_name(index_load) != "LLIL_SET_REG"
        or _operation_name(index_store) != "LLIL_STORE"
        or load_ssa is None
        or store_ssa is None
        or getattr(load_ssa, "non_ssa_form", None) != index_load
        or getattr(store_ssa, "non_ssa_form", None) != index_store
        or _operation_name(load_ssa) != "LLIL_SET_REG_SSA"
        or _operation_name(store_ssa) != "LLIL_STORE_SSA"
    ):
        return False
    load_source = getattr(load_ssa, "src", None)
    while getattr(getattr(load_source, "operation", None), "name", None) in (
        "LLIL_LOW_PART",
        "LLIL_SX",
        "LLIL_ZX",
    ):
        next_source = getattr(load_source, "src", None)
        if next_source is load_source:
            return False
        load_source = next_source
    if (
        getattr(getattr(load_source, "operation", None), "name", None)
        != "LLIL_LOAD_SSA"
    ):
        return False
    load_memory = getattr(load_source, "src_memory", None)
    store_memory = getattr(store_ssa, "dest_memory", None)
    return (
        type(load_memory) is int
        and load_memory >= 0
        and type(store_memory) is int
        and store_memory >= 0
        and load_memory == store_memory
    )


def _frozen_selector_store_is_unique(
    view,
    selector_address,
    selector_width,
    index_load,
    index_store,
    prefix_state,
    instructions_by_index,
    frozen_store_index=None,
) -> bool:
    if (
        not isinstance(selector_address, _StackOffset)
        or type(selector_width) is not int
        or selector_width <= 0
        or not isinstance(prefix_state, tuple)
        or len(prefix_state) != 3
        or type(prefix_state[0]) is not int
    ):
        return False
    store_index = getattr(index_store, "instr_index", None)
    if type(store_index) is not int or store_index <= prefix_state[0]:
        return False
    if _selector_store_reaches_index_load(index_load, index_store):
        return True
    stores = frozen_store_index
    if stores is None:
        stores = _frozen_selector_store_index(view, prefix_state, instructions_by_index)
    if not isinstance(stores, tuple):
        return False
    matching_stores = 0
    for address_offset, size, instruction_index, instruction in stores:
        if not _ranges_overlap(
            selector_address.offset, selector_width, address_offset, size
        ):
            continue
        if (
            instruction_index != store_index
            or instruction is not index_store
            or address_offset != selector_address.offset
            or size != selector_width
        ):
            return False
        matching_stores += 1
    return matching_stores == 1


def _frozen_selector_store_index(view, prefix_state, instructions_by_index):
    if (
        not isinstance(prefix_state, tuple)
        or len(prefix_state) != 3
        or type(prefix_state[0]) is not int
        or not isinstance(prefix_state[1], tuple)
        or not isinstance(prefix_state[2], tuple)
    ):
        return None
    registers = prefix_state[1]
    stack_values = prefix_state[2]
    stores = []
    for instruction_index in sorted(instructions_by_index):
        if instruction_index <= prefix_state[0]:
            continue
        instruction = instructions_by_index[instruction_index]
        if _operation_name(instruction) != "LLIL_STORE":
            continue
        size = getattr(instruction, "size", None)
        address = _evaluate_literal(
            view.arch, getattr(instruction, "dest", None), registers, stack_values
        )
        if not isinstance(address, _StackOffset) or type(size) is not int or size <= 0:
            continue
        stores.append((address.offset, size, instruction_index, instruction))
    return tuple(stores)


def _frozen_selector_table_entries(
    view,
    policy,
    index_load,
    index_store,
    table_load,
    selector_width,
    prefix_state,
    frozen_writes,
    frozen_base_cache,
    instructions_by_index,
    private_frame_bases=None,
    frozen_store_index=None,
):
    address_size = getattr(getattr(view, "arch", None), "address_size", None)
    if (
        frozen_writes is None
        or type(address_size) is not int
        or address_size <= 0
        or not isinstance(frozen_base_cache, list)
        or not isinstance(prefix_state, tuple)
        or len(prefix_state) != 3
        or not isinstance(prefix_state[1], tuple)
        or not isinstance(prefix_state[2], tuple)
    ):
        return None
    index_address_expression = getattr(index_load.src, "src", None)
    stored_address_expression = getattr(index_store, "dest", None)
    table_address_expression = getattr(table_load.src, "src", None)
    if (
        index_address_expression is None
        or stored_address_expression is None
        or table_address_expression is None
    ):
        return None
    selector_base = _direct_stack_base(index_address_expression)
    stored_base = _direct_stack_base(stored_address_expression)
    table_base = _direct_stack_base(table_address_expression)
    if (
        selector_base is None
        or stored_base is None
        or table_base is None
        or not _same_register(selector_base, stored_base)
    ):
        return None
    if private_frame_bases is None:
        if not (
            _cached_frozen_frame_base_is_stable(
                frozen_base_cache,
                view,
                selector_base,
                prefix_state,
                instructions_by_index,
            )
            and _cached_frozen_frame_base_is_stable(
                frozen_base_cache,
                view,
                table_base,
                prefix_state,
                instructions_by_index,
            )
        ):
            return None
    elif not (
        _private_frame_base_uses_current_anchor(
            index_load, selector_base, private_frame_bases
        )
        and _private_frame_base_uses_current_anchor(
            index_store, stored_base, private_frame_bases
        )
        and _private_frame_base_uses_current_anchor(
            table_load, table_base, private_frame_bases
        )
    ):
        return None
    registers = prefix_state[1]
    stack_values = prefix_state[2]
    index_address = _evaluate_literal(
        view.arch, index_address_expression, registers, stack_values
    )
    stored_address = _evaluate_literal(
        view.arch, stored_address_expression, registers, stack_values
    )
    if (
        not isinstance(index_address, _StackOffset)
        or not isinstance(stored_address, _StackOffset)
        or index_address.offset != stored_address.offset
        or not _frozen_selector_store_is_unique(
            view,
            index_address,
            selector_width,
            index_load,
            index_store,
            prefix_state,
            instructions_by_index,
            frozen_store_index,
        )
    ):
        return None
    table_pointer = _frozen_prefix_load(
        view,
        table_address_expression,
        address_size,
        prefix_state,
        frozen_writes,
    )
    if type(table_pointer) is not int:
        return None
    return _selector_table_entries(view, policy, table_pointer)


def _selector_table_targets(
    view,
    llil,
    jump,
    policy,
    blocks_by_instruction=None,
    predecessors=None,
    reachable_by_stop=None,
    instructions_by_index=None,
    prefix_state=None,
    frozen_writes=None,
    frozen_base_cache=None,
    private_frame_bases=None,
    frozen_store_index=None,
):
    jump_index = getattr(jump, "instr_index", None)
    target_register = _register_expression(getattr(jump, "dest", None))
    if type(jump_index) is not int or jump_index < 9 or target_register is None:
        return None
    block = _current_block(llil, jump_index, blocks_by_instruction)
    block_start = None if block is None else getattr(block, "start", None)
    block_end = None if block is None else getattr(block, "end", None)
    if (
        type(block_start) is not int
        or type(block_end) is not int
        or block_start > jump_index - 9
        or block_end <= jump_index
    ):
        return None
    address_size = getattr(getattr(view, "arch", None), "address_size", None)
    if type(address_size) is not int or address_size <= 0:
        return None
    target_load = _set_register_at(llil, jump_index - 1, instructions_by_index)
    indexed_write = _set_register_at(llil, jump_index - 2, instructions_by_index)
    target_load_expression = (
        None if target_load is None else getattr(target_load, "src", None)
    )
    if (
        target_load is None
        or indexed_write is None
        or not _same_register(target_load.dest, target_register)
        or target_load_expression is None
        or _operation_name(target_load_expression) != "LLIL_LOAD"
        or not _same_register(
            _register_expression(getattr(target_load_expression, "src", None)),
            target_register,
        )
        or not _same_register(indexed_write.dest, target_register)
    ):
        return None
    add = getattr(indexed_write, "src", None)
    if add is None or _operation_name(add) != "LLIL_ADD":
        return None
    add_operands = _binary_operands(add)
    if add_operands is None or _operation_name(add_operands[1]) != "LLIL_MUL":
        return None
    table_register = _register_expression(add_operands[0])
    multiply_operands = _binary_operands(add_operands[1])
    if table_register is None or multiply_operands is None:
        return None
    index_register = _register_expression(multiply_operands[0])
    stride_register = _register_expression(multiply_operands[1])
    if index_register is None or stride_register is None:
        return None
    stride_write = _set_register_at(llil, jump_index - 3, instructions_by_index)
    index_extend = _set_register_at(llil, jump_index - 4, instructions_by_index)
    index_load = _set_register_at(llil, jump_index - 5, instructions_by_index)
    table_load = _set_register_at(llil, jump_index - 6, instructions_by_index)
    index_store = _current_instruction(llil, jump_index - 7, instructions_by_index)
    selector_mask = _set_register_at(llil, jump_index - 8, instructions_by_index)
    selector_bool = _set_register_at(llil, jump_index - 9, instructions_by_index)
    if any(
        instruction is None
        for instruction in (
            stride_write,
            index_extend,
            index_load,
            table_load,
            index_store,
            selector_mask,
            selector_bool,
        )
    ):
        return None
    if (
        not _same_register(stride_write.dest, stride_register)
        or _constant_value(stride_write.src) != address_size
        or not _same_register(index_extend.dest, index_register)
        or _operation_name(index_extend.src) != "LLIL_SX"
        or not _same_register(table_load.dest, table_register)
        or _operation_name(table_load.src) != "LLIL_LOAD"
        or getattr(table_load.src, "size", None) != address_size
        or _operation_name(index_load.src) != "LLIL_LOAD"
        or _operation_name(index_store) != "LLIL_STORE"
    ):
        return None
    index_low_register = _register_expression(getattr(index_extend.src, "src", None))
    if index_low_register is None or not _same_register(
        index_load.dest, index_low_register
    ):
        return None
    selector_register = _register_expression(getattr(index_store, "src", None))
    if selector_register is None:
        return None
    mask = getattr(selector_mask, "src", None)
    mask_operands = None if mask is None else _binary_operands(mask)
    selector_expression = getattr(selector_bool, "src", None)
    selector_operands = (
        None
        if selector_expression is None
        else getattr(selector_expression, "operands", None)
    )
    selector_condition = (
        None
        if not isinstance(selector_operands, (list, tuple))
        or len(selector_operands) != 1
        else selector_operands[0]
    )
    if (
        not _same_register(selector_mask.dest, selector_register)
        or mask is None
        or _operation_name(mask) != "LLIL_AND"
        or mask_operands is None
        or not _same_register(_register_expression(mask_operands[0]), selector_register)
        or _constant_value(mask_operands[1]) != 1
        or not _same_register(selector_bool.dest, selector_register)
        or selector_expression is None
        or _operation_name(selector_expression) != "LLIL_BOOL_TO_INT"
        or not isinstance(selector_operands, (list, tuple))
        or len(selector_operands) != 1
        or selector_condition is None
        or _operation_name(selector_condition) not in _BOOLEAN_SELECTOR_COMPARISONS
        or _binary_operands(selector_condition) is None
    ):
        return None
    selector_width = getattr(index_store, "size", None)
    if (
        type(selector_width) is not int
        or selector_width <= 0
        or getattr(index_load, "size", None) != selector_width
        or getattr(selector_mask, "size", None) != selector_width
        or getattr(selector_bool, "size", None) != selector_width
        or getattr(index_load.src, "size", None) != selector_width
    ):
        return None
    entries = _frozen_selector_table_entries(
        view,
        policy,
        index_load,
        index_store,
        table_load,
        selector_width,
        prefix_state,
        frozen_writes,
        frozen_base_cache,
        instructions_by_index,
        private_frame_bases,
        frozen_store_index,
    )
    if entries is not None:
        return selector_expression, *entries
    state = _path_state_before(
        view,
        llil,
        table_load.instr_index,
        blocks_by_instruction,
        predecessors,
        reachable_by_stop,
        instructions_by_index,
        prefix_state,
    )
    if state is not None:
        registers, stack_values = state
        table_base = _evaluate_literal(
            view.arch, table_load.src, registers, stack_values
        )
        index_address = _evaluate_literal(
            view.arch, getattr(index_load.src, "src", None), registers, stack_values
        )
        stored_address = _evaluate_literal(
            view.arch, getattr(index_store, "dest", None), registers, stack_values
        )
        if (
            type(table_base) is not int
            or not isinstance(index_address, _StackOffset)
            or not isinstance(stored_address, _StackOffset)
            or index_address.offset != stored_address.offset
        ):
            return None
        entries = _selector_table_entries(view, policy, table_base)
        return None if entries is None else (selector_expression, *entries)
    if (
        (index_address_expression := getattr(index_load.src, "src", None)) is not None
        and (stored_address_expression := getattr(index_store, "dest", None))
        is not None
        and (table_address_expression := getattr(table_load.src, "src", None))
        is not None
        and (selector_slot := _direct_stack_slot(index_address_expression)) is not None
        and (stored_slot := _direct_stack_slot(stored_address_expression)) is not None
        and _same_register(selector_slot[0], stored_slot[0])
        and selector_slot[1] == stored_slot[1]
        and _direct_stack_base(table_address_expression) is not None
        and _target_is_preserved(
            view.arch,
            llil,
            selector_bool.instr_index,
            jump_index,
            selector_slot[0],
            instructions_by_index,
        )
    ):
        table_load_ssa = getattr(table_load, "ssa_form", table_load)
        table_pointer_expression = getattr(table_load_ssa, "src", None)
        if table_pointer_expression is not None:
            values = evaluate_values(
                view, llil.ssa_form, table_pointer_expression, _VALUE_BUDGET, policy
            )
            if type(values) is CompleteValues and len(values.values) == 1:
                table_pointer = values.values[0]
                if type(table_pointer) is int:
                    entries = _selector_table_entries(view, policy, table_pointer)
                    if entries is not None:
                        return selector_expression, *entries
    return None


def branch_targets(
    query: BranchTargetQuery,
) -> CompleteBatch[BranchTargetFact] | Inconclusive:
    jumps = _indirect_jumps(query.llil)
    if not jumps:
        return CompleteBatch(())
    if query.llil.ssa_form is None:
        return Inconclusive("current LLIL SSA is unavailable")
    policy = memory.initialized_data_policy(query.view)
    if policy is None:
        return Inconclusive("could not snapshot initialized static data")
    blocks_by_instruction = _blocks_by_instruction(query.llil)
    instructions_by_index = _instruction_snapshot(query.llil)
    prefix_state = _route_prefix_state(query.view, query.llil, instructions_by_index)
    frozen_writes = _frozen_prefix_writes(
        query.view, prefix_state, instructions_by_index
    )
    frozen_store_index = _frozen_selector_store_index(
        query.view, prefix_state, instructions_by_index
    )
    private_frame = (
        None
        if frozen_writes is not None
        else _private_frozen_frame_writes(
            query.view, prefix_state, instructions_by_index
        )
    )
    if private_frame is not None and not isinstance(frozen_store_index, tuple):
        private_frame = None
    private_frame_bases = None if private_frame is None else private_frame[0]
    frame_writes = frozen_writes
    if private_frame is not None:
        frame_writes = tuple(
            sorted(
                {
                    *private_frame[1],
                    *(
                        (offset, size)
                        for offset, size, _index, _instruction in frozen_store_index
                    ),
                }
            )
        )
    frozen_base_cache = []
    predecessors = _jump_predecessors(
        query.llil, blocks_by_instruction, instructions_by_index
    )
    reachable_by_stop = {}
    facts = []
    for jump in jumps:
        target = _single_executable_target(
            query.view, query.llil.ssa_form, jump, policy
        )
        targets = None if target is None else (target,)
        if targets is None:
            target = _literal_table_target(
                query.view,
                query.llil,
                jump,
                policy,
                blocks_by_instruction,
                instructions_by_index,
            )
            targets = None if target is None else (target,)
        if targets is None:
            target = _frozen_frame_table_target(
                query.view,
                query.llil,
                jump,
                policy,
                prefix_state,
                frame_writes,
                frozen_base_cache,
                blocks_by_instruction,
                instructions_by_index,
                private_frame_bases,
            )
            targets = None if target is None else (target,)
        selector = None
        if targets is None:
            selector = _selector_table_targets(
                query.view,
                query.llil,
                jump,
                policy,
                blocks_by_instruction,
                predecessors,
                reachable_by_stop,
                instructions_by_index,
                prefix_state,
                frame_writes,
                frozen_base_cache,
                private_frame_bases,
                frozen_store_index,
            )
        if selector is not None:
            condition, false_target, true_target = selector
            targets = tuple(sorted({false_target, true_target}))
            if false_target == true_target:
                facts.append(BranchTargetFact(jump, targets))
            else:
                facts.append(
                    BranchTargetFact(
                        jump,
                        targets,
                        condition=condition,
                        true_target=true_target,
                        false_target=false_target,
                    )
                )
        elif targets is not None:
            facts.append(BranchTargetFact(jump, targets))
    return CompleteBatch(tuple(facts))
