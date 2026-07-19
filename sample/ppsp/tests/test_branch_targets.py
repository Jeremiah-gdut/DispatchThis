from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest


_PROVIDER_PATH = Path(__file__).parents[1] / "__init__.py"


@dataclass(frozen=True, slots=True)
class _Operation:
    name: str


@dataclass(frozen=True, slots=True)
class _Intrinsic:
    name: str


@dataclass(frozen=True, slots=True)
class _Register:
    name: str
    index: int


@dataclass(frozen=True, slots=True)
class _Expression:
    operation: _Operation
    src: "_Expression | _Register | None" = None
    operands: tuple["_Expression | _Register | int", ...] = ()
    constant: int | None = None
    size: int = 8


@dataclass(frozen=True, slots=True)
class _SsaJump:
    dest: _Expression


@dataclass(frozen=True, slots=True)
class _Jump:
    operation: _Operation
    dest: _Expression
    ssa_form: _SsaJump
    address: int
    instr_index: int = 0
    size: int = 8


@dataclass(frozen=True, slots=True)
class _Instruction:
    operation: _Operation
    dest: _Expression | _Register | None
    src: _Expression | None
    instr_index: int
    size: int = 8
    params: tuple[_Expression, ...] = ()
    targets: dict[int, int] | None = None
    address: int = 0
    intrinsic: _Intrinsic | None = None


@dataclass(frozen=True, slots=True)
class _BasicBlock:
    start: int
    end: int
    outgoing_edges: tuple["_Edge", ...] = ()


@dataclass(frozen=True, slots=True)
class _BlockTarget:
    start: int


@dataclass(frozen=True, slots=True)
class _Edge:
    target: _BlockTarget


@dataclass(slots=True)
class _CountingBlocks:
    blocks: tuple[_BasicBlock, ...]
    iterations: int = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.blocks)


@dataclass(frozen=True, slots=True)
class _SsaIl:
    name: str


@dataclass(frozen=True, slots=True)
class _Llil:
    instructions: tuple[_Instruction | _Jump, ...]
    ssa_form: _SsaIl
    basic_blocks: tuple[_BasicBlock, ...] = ()
    read_count: list[int] = field(default_factory=lambda: [0])

    def __getitem__(self, index: int) -> _Instruction | _Jump:
        self.read_count[0] += 1
        return self.instructions[index]


@dataclass(frozen=True, slots=True)
class _Query:
    view: str
    llil: _Llil


@dataclass(frozen=True, slots=True)
class _AnalysisBudget:
    node_limit: int
    edge_limit: int


@dataclass(frozen=True, slots=True)
class _CompleteValues:
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _BranchTargetFact:
    jump_il: _Jump
    targets: tuple[int, ...]
    condition: _Expression | None = None
    true_target: int | None = None
    false_target: int | None = None


@dataclass(frozen=True, slots=True)
class _CompleteBatch:
    facts: tuple[_BranchTargetFact, ...]


@dataclass(frozen=True, slots=True)
class _Inconclusive:
    reason: str


@dataclass(frozen=True, slots=True)
class _SampleSemantics:
    provider_id: str
    name: str
    api_version: int
    branch_targets: Callable[[_Query], _CompleteBatch | _Inconclusive] | None = None


@dataclass(frozen=True, slots=True)
class _InitializedDataPolicy:
    table_slot: int
    target: int
    extra_targets: tuple[int, ...] = ()
    byte_order: str = "little"

    def bytes_at(self, address: int, width: int) -> bytes | None:
        offset = address - self.table_slot
        if width != 8 or offset < 0 or offset % width != 0:
            return None
        targets = (self.target, *self.extra_targets)
        index = offset // width
        if index >= len(targets):
            return None
        return targets[index].to_bytes(width, self.byte_order)


@dataclass(frozen=True, slots=True)
class _Architecture:
    address_size: int = 8
    stack_pointer: str = "sp"

    def get_reg_index(self, register: _Register | str) -> int:
        if isinstance(register, _Register):
            return register.index
        return {
            "sp": 1,
            "fp": 2,
            "x8": 8,
            "w8": 108,
            "x9": 9,
            "w9": 109,
            "x10": 10,
            "w10": 110,
            "x11": 11,
            "x22": 22,
            "x23": 23,
        }[register]

    def get_modified_regs_on_write(self, register_name: str) -> tuple[str, ...]:
        if register_name in ("x8", "w8", "x9", "w9", "x10", "w10"):
            return {
                "x8": ("x8", "w8"),
                "w8": ("x8", "w8"),
                "x9": ("x9", "w9"),
                "w9": ("x9", "w9"),
                "x10": ("x10", "w10"),
                "w10": ("x10", "w10"),
            }[register_name]
        return (register_name,)


@dataclass(frozen=True, slots=True)
class _View:
    arch: _Architecture


def _load_provider(
    monkeypatch: pytest.MonkeyPatch,
    values: tuple[int, ...] | None,
    executable: Callable[[int], bool],
    policy: str | _InitializedDataPolicy | None = "snapshot",
) -> tuple[ModuleType, list[_SampleSemantics]]:
    registered: list[_SampleSemantics] = []

    def evaluate_values(
        _view: str,
        _il: _SsaIl,
        _expression: _Expression,
        _budget: _AnalysisBudget,
        _policy: str,
    ) -> _CompleteValues | _Inconclusive:
        if values is None:
            return _Inconclusive("required SSA definition is unavailable")
        return _CompleteValues(values)

    def register_provider(semantics: _SampleSemantics) -> bool:
        registered.append(semantics)
        return True

    dispatch_this = ModuleType("DispatchThis")
    dispatch_this.AnalysisBudget = _AnalysisBudget
    dispatch_this.BranchTargetFact = _BranchTargetFact
    dispatch_this.BranchTargetQuery = _Query
    dispatch_this.CompleteBatch = _CompleteBatch
    dispatch_this.CompleteValues = _CompleteValues
    dispatch_this.CORE_API_VERSION = 4
    dispatch_this.Inconclusive = _Inconclusive
    dispatch_this.SampleSemantics = _SampleSemantics
    dispatch_this.evaluate_values = evaluate_values
    dispatch_this.register_provider = register_provider

    helpers = ModuleType("DispatchThis.helpers")
    helpers.memory = SimpleNamespace(
        initialized_data_policy=lambda _view: policy,
        is_executable_target=lambda _view, target: executable(target),
    )
    monkeypatch.setitem(sys.modules, "DispatchThis", dispatch_this)
    monkeypatch.setitem(sys.modules, "DispatchThis.helpers", helpers)

    spec = importlib.util.spec_from_file_location("ppsp_provider_test", _PROVIDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the ppsp provider")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, registered


def _jump() -> _Jump:
    return _Jump(
        operation=_Operation("LLIL_JUMP"),
        dest=_Expression(_Operation("LLIL_REG")),
        ssa_form=_SsaJump(_Expression(_Operation("LLIL_REG_SSA"))),
        address=0x97E5E4,
    )


def _query(jump: _Jump) -> _Query:
    return _Query(view="view", llil=_Llil((jump,), _SsaIl("ssa")))


def test_jump_targets_accepts_a_block_start_normalized_before_its_first_llil_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _ = _load_provider(monkeypatch, (0x1000,), lambda _target: True)
    jump_to = _Instruction(
        _Operation("LLIL_JUMP_TO"),
        None,
        None,
        0,
        targets={0x1000: 1},
    )
    first_instruction = _Instruction(
        _Operation("LLIL_NOP"),
        None,
        None,
        1,
        address=0x1004,
    )
    llil = _Llil(
        (jump_to, first_instruction),
        _SsaIl("ssa"),
        (_BasicBlock(0, 1), _BasicBlock(1, 2)),
    )

    # Given Binary Ninja normalized the first visible LLIL instruction past the CFG address.
    # When the recovered target map still names that basic-block start index.
    # Then the provider retains the current CFG edge for path replay.
    assert module._jump_targets(llil, jump_to) == (1,)
    assert module._indirect_jumps(llil) == ()


def _literal_table_query() -> tuple[_Query, _Jump]:
    sp = _Register("sp", 1)
    fp = _Register("fp", 2)
    x8 = _Register("x8", 8)
    w8 = _Register("w8", 108)
    x9 = _Register("x9", 9)
    x10 = _Register("x10", 10)
    x23 = _Register("x23", 23)

    def register(register_value: _Register, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_REG"), src=register_value, size=size)

    def constant(value: int, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_CONST_PTR"), constant=value, size=size)

    def add(left: _Expression, right: _Expression, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_ADD"), operands=(left, right), size=size)

    def subtract(left: _Expression, right: _Expression) -> _Expression:
        return _Expression(_Operation("LLIL_SUB"), operands=(left, right))

    def load(address: _Expression, size: int) -> _Expression:
        return _Expression(_Operation("LLIL_LOAD"), src=address, size=size)

    instructions: tuple[_Instruction | _Jump, ...] = (
        _Instruction(
            _Operation("LLIL_SET_REG"), sp, subtract(register(sp), constant(0x20)), 0
        ),
        _Instruction(_Operation("LLIL_SET_REG"), fp, register(sp), 1),
        _Instruction(
            _Operation("LLIL_SET_REG"), x23, subtract(register(fp), constant(0x10)), 2
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x8, constant(0x1000), 3),
        _Instruction(
            _Operation("LLIL_STORE"), add(register(x23), constant(0)), register(x8), 4
        ),
        _Instruction(_Operation("LLIL_SET_REG"), w8, constant(2, 4), 5, size=4),
        _Instruction(
            _Operation("LLIL_STORE"),
            add(register(x23), constant(8)),
            register(w8, 4),
            6,
            size=4,
        ),
        _Instruction(_Operation("LLIL_CALL"), constant(0x9000), None, 7),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            w8,
            load(add(register(x23), constant(8)), 4),
            8,
            size=4,
        ),
        _Instruction(
            _Operation("LLIL_SET_REG"), x9, load(add(register(x23), constant(0)), 8), 9
        ),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            x8,
            _Expression(_Operation("LLIL_SX"), src=register(w8, 4)),
            10,
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x10, constant(8), 11),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            x8,
            add(
                register(x9),
                _Expression(
                    _Operation("LLIL_MUL"), operands=(register(x8), register(x10))
                ),
            ),
            12,
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x8, load(register(x8), 8), 13),
        _Jump(
            operation=_Operation("LLIL_JUMP"),
            dest=register(x8),
            ssa_form=_SsaJump(_Expression(_Operation("LLIL_REG_SSA"))),
            address=0x97E5E4,
            instr_index=14,
        ),
    )
    jump = instructions[-1]
    assert isinstance(jump, _Jump)
    return (
        _Query(
            view=_View(_Architecture()),
            llil=_Llil(
                instructions, _SsaIl("ssa"), (_BasicBlock(0, len(instructions)),)
            ),
        ),
        jump,
    )


def _selector_table_query(
    comparison_operation: str = "LLIL_CMP_SLE",
) -> tuple[_Query, _Jump]:
    sp = _Register("sp", 1)
    w8 = _Register("w8", 108)
    x9 = _Register("x9", 9)
    w9 = _Register("w9", 109)
    x10 = _Register("x10", 10)
    w10 = _Register("w10", 110)
    x11 = _Register("x11", 11)
    x22 = _Register("x22", 22)

    def register(register_value: _Register, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_REG"), src=register_value, size=size)

    def constant(value: int, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_CONST_PTR"), constant=value, size=size)

    def add(left: _Expression, right: _Expression, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_ADD"), operands=(left, right), size=size)

    def subtract(left: _Expression, right: _Expression) -> _Expression:
        return _Expression(_Operation("LLIL_SUB"), operands=(left, right))

    def load(address: _Expression, size: int) -> _Expression:
        return _Expression(_Operation("LLIL_LOAD"), src=address, size=size)

    index_slot = add(register(x22), constant(0x10))
    table_slot = add(register(x22), constant(0x20))
    instructions: tuple[_Instruction | _Jump, ...] = (
        _Instruction(
            _Operation("LLIL_SET_REG"), sp, subtract(register(sp), constant(0x20)), 0
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x22, register(sp), 1),
        _Instruction(_Operation("LLIL_SET_REG"), x9, constant(0x3000), 2),
        _Instruction(_Operation("LLIL_STORE"), table_slot, register(x9), 3),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            w9,
            _Expression(
                _Operation("LLIL_BOOL_TO_INT"),
                operands=(
                    _Expression(
                        _Operation(comparison_operation),
                        operands=(register(w8, 4), constant(0xB, 4)),
                        size=4,
                    ),
                ),
                size=4,
            ),
            4,
            size=4,
        ),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            w9,
            _Expression(
                _Operation("LLIL_AND"),
                operands=(register(w9, 4), constant(1, 4)),
                size=4,
            ),
            5,
            size=4,
        ),
        _Instruction(_Operation("LLIL_STORE"), index_slot, register(w9, 4), 6, size=4),
        _Instruction(_Operation("LLIL_SET_REG"), x9, load(table_slot, 8), 7),
        _Instruction(_Operation("LLIL_SET_REG"), w10, load(index_slot, 4), 8, size=4),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            x10,
            _Expression(_Operation("LLIL_SX"), src=register(w10, 4)),
            9,
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x11, constant(8), 10),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            x9,
            add(
                register(x9),
                _Expression(
                    _Operation("LLIL_MUL"), operands=(register(x10), register(x11))
                ),
            ),
            11,
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x9, load(register(x9), 8), 12),
        _Jump(
            operation=_Operation("LLIL_JUMP"),
            dest=register(x9),
            ssa_form=_SsaJump(_Expression(_Operation("LLIL_REG_SSA"))),
            address=0x986A6C,
            instr_index=13,
        ),
    )
    jump = instructions[-1]
    assert isinstance(jump, _Jump)
    return (
        _Query(
            view=_View(_Architecture()),
            llil=_Llil(
                instructions, _SsaIl("ssa"), (_BasicBlock(0, len(instructions)),)
            ),
        ),
        jump,
    )


def _frozen_selector_table_query(
    duplicate_selector_store: bool = False,
) -> tuple[_Query, _Jump]:
    query, jump = _selector_table_query()
    instructions = query.llil.instructions
    stack_pointer = instructions[0].dest
    selector_base = instructions[1].dest
    table_value = instructions[2].dest
    assert isinstance(stack_pointer, _Register)
    assert isinstance(selector_base, _Register)
    assert isinstance(table_value, _Register)
    table_base = _Register("x23", 23)
    selector_slot = instructions[6].dest
    selector_register = instructions[4].dest
    assert isinstance(selector_slot, _Expression)
    assert isinstance(selector_register, _Register)

    def register(register_value: _Register, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_REG"), src=register_value, size=size)

    def constant(value: int, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_CONST_PTR"), constant=value, size=size)

    def add(left: _Expression, right: _Expression, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_ADD"), operands=(left, right), size=size)

    def load(address: _Expression, size: int) -> _Expression:
        return _Expression(_Operation("LLIL_LOAD"), src=address, size=size)

    table_slot = add(register(table_base), constant(0x20))
    prefix = (
        replace(instructions[0], instr_index=0),
        replace(instructions[1], instr_index=1),
        _Instruction(
            _Operation("LLIL_SET_REG"),
            table_base,
            add(register(stack_pointer), constant(0x40)),
            2,
        ),
        replace(instructions[2], instr_index=3),
        replace(instructions[3], dest=table_slot, instr_index=4),
        _Instruction(
            _Operation("LLIL_JUMP_TO"),
            None,
            None,
            5,
            targets={0x1018: 6, 0x101C: 7},
        ),
        _Instruction(_Operation("LLIL_JUMP_TO"), None, None, 6, targets={0x1020: 8}),
        _Instruction(_Operation("LLIL_JUMP_TO"), None, None, 7, targets={0x1020: 8}),
    )
    duplicate_store = ()
    if duplicate_selector_store:
        duplicate_store = (
            _Instruction(
                _Operation("LLIL_STORE"),
                selector_slot,
                register(selector_register, 4),
                8,
                size=4,
            ),
        )
    tail_offset = 5 if duplicate_selector_store else 4
    tail = []
    for instruction in instructions[4:]:
        updated = replace(
            instruction, instr_index=instruction.instr_index + tail_offset
        )
        if instruction.instr_index == 7:
            assert isinstance(updated, _Instruction)
            updated = replace(updated, src=load(table_slot, 8))
        tail.append(updated)
    jump = tail[-1]
    assert isinstance(jump, _Jump)
    recovered_instructions = (*prefix, *duplicate_store, *tail)
    return (
        _Query(
            view=query.view,
            llil=_Llil(
                recovered_instructions,
                query.llil.ssa_form,
                (
                    _BasicBlock(0, 6),
                    _BasicBlock(6, 7),
                    _BasicBlock(7, 8),
                    _BasicBlock(8, len(recovered_instructions)),
                ),
            ),
        ),
        jump,
    )


def _frozen_frame_table_query(overwrite_index: bool = False) -> tuple[_Query, _Jump]:
    sp = _Register("sp", 1)
    x8 = _Register("x8", 8)
    w8 = _Register("w8", 108)
    x9 = _Register("x9", 9)
    x10 = _Register("x10", 10)
    x22 = _Register("x22", 22)

    def register(register_value: _Register, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_REG"), src=register_value, size=size)

    def constant(value: int, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_CONST_PTR"), constant=value, size=size)

    def add(left: _Expression, right: _Expression, size: int = 8) -> _Expression:
        return _Expression(_Operation("LLIL_ADD"), operands=(left, right), size=size)

    def subtract(left: _Expression, right: _Expression) -> _Expression:
        return _Expression(_Operation("LLIL_SUB"), operands=(left, right))

    def load(address: _Expression, size: int) -> _Expression:
        return _Expression(_Operation("LLIL_LOAD"), src=address, size=size)

    index_slot = add(register(x22), constant(0x10))
    table_slot = add(register(x22), constant(0x20))
    instructions = [
        _Instruction(
            _Operation("LLIL_SET_REG"), sp, subtract(register(sp), constant(0x80)), 0
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x22, register(sp), 1),
        _Instruction(_Operation("LLIL_SET_REG"), x9, constant(0x3000), 2),
        _Instruction(_Operation("LLIL_STORE"), table_slot, register(x9), 3),
        _Instruction(_Operation("LLIL_SET_REG"), w8, constant(1, 4), 4, size=4),
        _Instruction(_Operation("LLIL_STORE"), index_slot, register(w8, 4), 5, size=4),
        _Instruction(_Operation("LLIL_JUMP_TO"), None, None, 6, targets={0x101C: 7}),
        _Instruction(
            _Operation("LLIL_STORE"),
            add(register(x22), constant(0x40)),
            register(w8, 4),
            7,
            size=4,
        ),
    ]
    if overwrite_index:
        instructions.append(
            _Instruction(
                _Operation("LLIL_STORE"),
                index_slot,
                register(w8, 4),
                len(instructions),
                size=4,
            )
        )
    first_tail_index = len(instructions)
    instructions.extend(
        (
            _Instruction(
                _Operation("LLIL_SET_REG"),
                w8,
                load(index_slot, 4),
                first_tail_index,
                size=4,
            ),
            _Instruction(
                _Operation("LLIL_SET_REG"),
                x9,
                load(table_slot, 8),
                first_tail_index + 1,
            ),
            _Instruction(
                _Operation("LLIL_SET_REG"),
                x8,
                _Expression(_Operation("LLIL_SX"), src=register(w8, 4)),
                first_tail_index + 2,
            ),
            _Instruction(
                _Operation("LLIL_SET_REG"),
                x10,
                constant(8),
                first_tail_index + 3,
            ),
            _Instruction(
                _Operation("LLIL_SET_REG"),
                x8,
                add(
                    register(x9),
                    _Expression(
                        _Operation("LLIL_MUL"),
                        operands=(register(x8), register(x10)),
                    ),
                ),
                first_tail_index + 4,
            ),
            _Instruction(
                _Operation("LLIL_SET_REG"),
                x8,
                load(register(x8), 8),
                first_tail_index + 5,
            ),
            _Jump(
                operation=_Operation("LLIL_JUMP"),
                dest=register(x8),
                ssa_form=_SsaJump(_Expression(_Operation("LLIL_REG_SSA"))),
                address=0x98219C,
                instr_index=first_tail_index + 6,
            ),
        )
    )
    jump = instructions[-1]
    assert isinstance(jump, _Jump)
    return (
        _Query(
            view=_View(_Architecture()),
            llil=_Llil(
                tuple(instructions),
                _SsaIl("ssa"),
                (
                    _BasicBlock(0, 7, (_Edge(_BlockTarget(7)),)),
                    _BasicBlock(7, len(instructions)),
                ),
            ),
        ),
        jump,
    )


def _shared_unreachable_dag() -> tuple[_Llil, _CountingBlocks, int]:
    levels = tuple((1 + level * 2, 2 + level * 2) for level in range(12))
    stop_index = 1 + len(levels) * 2

    def jump_to(index: int, targets: tuple[int, int]) -> _Instruction:
        return _Instruction(
            _Operation("LLIL_JUMP_TO"),
            None,
            None,
            index,
            targets={0x1000 + target * 4: target for target in targets},
            address=0x1000 + index * 4,
        )

    instructions = [jump_to(0, levels[0])]
    for level, starts in enumerate(levels):
        next_starts = None if level + 1 == len(levels) else levels[level + 1]
        for index in starts:
            instruction = (
                _Instruction(_Operation("LLIL_NOP"), None, None, index)
                if next_starts is None
                else jump_to(index, next_starts)
            )
            instructions.append(instruction)
    instructions.append(_Instruction(_Operation("LLIL_NOP"), None, None, stop_index))
    blocks = _CountingBlocks(
        tuple(_BasicBlock(index, index + 1) for index in range(len(instructions)))
    )
    return _Llil(tuple(instructions), _SsaIl("ssa"), blocks), blocks, stop_index


def test_branch_targets_emits_a_proven_single_executable_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a current indirect jump with one executable, evaluator-proven value.
    jump = _jump()
    provider, registered = _load_provider(
        monkeypatch, (0x986A24,), lambda target: target == 0x986A24
    )

    # When: the provider scans the current LLIL query.
    result = provider.branch_targets(_query(jump))

    # Then: it registers once and returns the exact current jump witness and target.
    assert registered == [provider.provider]
    assert result == _CompleteBatch((_BranchTargetFact(jump, (0x986A24,)),))


def test_branch_targets_rejects_a_multi_target_value_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a current indirect jump whose complete value set has multiple targets.
    jump = _jump()
    provider, _registered = _load_provider(
        monkeypatch, (0x986A24, 0x986B00), lambda _target: True
    )

    # When: the provider scans the current LLIL query.
    result = provider.branch_targets(_query(jump))

    # Then: it preserves the jump instead of selecting an arbitrary target.
    assert result == _CompleteBatch(())


def test_branch_targets_rejects_a_non_executable_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a singleton value that is not a current executable target.
    jump = _jump()
    provider, _registered = _load_provider(
        monkeypatch, (0x986A24,), lambda _target: False
    )

    # When: the provider scans the current LLIL query.
    result = provider.branch_targets(_query(jump))

    # Then: it preserves the jump rather than submitting invalid branch metadata.
    assert result == _CompleteBatch(())


def test_branch_targets_is_inconclusive_without_an_initialized_data_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an indirect jump but no safe initialized-data snapshot.
    jump = _jump()
    provider, _registered = _load_provider(
        monkeypatch, (0x986A24,), lambda _target: True, policy=None
    )

    # When: the provider scans the current LLIL query.
    result = provider.branch_targets(_query(jump))

    # Then: it reports the batch-level proof failure without returning a partial fact.
    assert result == _Inconclusive("could not snapshot initialized static data")


def test_branch_targets_recovers_a_linear_literal_table_jump_without_ssa_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a linear stack-local literal calculation ending in an initialized table load.
    query, jump = _literal_table_query()
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target == 0x2000,
        policy=_InitializedDataPolicy(table_slot=0x1010, target=0x2000),
    )

    # When: the workflow-time SSA graph has not exposed the required definitions.
    result = provider.branch_targets(query)

    # Then: the exact current jump receives only the proven table value.
    assert result == _CompleteBatch((_BranchTargetFact(jump, (0x2000,)),))


def test_branch_targets_recovers_both_boolean_selector_table_targets_without_ssa_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a boolean selector that indexes a two-entry initialized target table.
    query, jump = _selector_table_query()
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target in (0x4100, 0x4200),
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4200,),
        ),
    )

    # When: the selector's value is runtime-dependent but structurally one bit.
    result = provider.branch_targets(query)

    # Then: table slot zero and one preserve the false and true arms respectively.
    condition = query.llil.instructions[4].src
    assert result == _CompleteBatch(
        (_BranchTargetFact(jump, (0x4100, 0x4200), condition, 0x4200, 0x4100),)
    )


def test_branch_targets_recovers_an_equality_boolean_selector_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, jump = _selector_table_query("LLIL_CMP_E")
    jump = replace(jump, operation=_Operation("LLIL_JUMP_TO"))
    query = replace(
        query,
        llil=replace(query.llil, instructions=(*query.llil.instructions[:-1], jump)),
    )
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target in (0x4100, 0x4200),
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4200,
            extra_targets=(0x4100,),
        ),
    )

    condition = query.llil.instructions[4].src
    assert provider.branch_targets(query) == _CompleteBatch(
        (_BranchTargetFact(jump, (0x4100, 0x4200), condition, 0x4100, 0x4200),)
    )


def test_branch_targets_recovers_a_frozen_boolean_selector_after_ambiguous_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, jump = _frozen_selector_table_query()
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target in (0x4100, 0x4200),
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4200,),
        ),
    )

    result = provider.branch_targets(query)

    condition = query.llil.instructions[8].src
    assert result == _CompleteBatch(
        (_BranchTargetFact(jump, (0x4100, 0x4200), condition, 0x4200, 0x4100),)
    )


def test_branch_targets_rejects_an_unsupported_boolean_selector_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, _jump = _selector_table_query("LLIL_CMP_ULT")
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target in (0x4100, 0x4200),
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4200,),
        ),
    )

    assert provider.branch_targets(query) == _CompleteBatch(())


def test_branch_targets_rejects_a_frozen_selector_with_an_extra_slot_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, _jump = _frozen_selector_table_query(duplicate_selector_store=True)
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target in (0x4100, 0x4200),
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4200,),
        ),
    )

    assert provider.branch_targets(query) == _CompleteBatch(())


def test_branch_targets_collapses_duplicate_boolean_selector_table_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, jump = _selector_table_query()
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target == 0x4100,
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4100,),
        ),
    )

    assert provider.branch_targets(query) == _CompleteBatch(
        (_BranchTargetFact(jump, (0x4100,)),)
    )


def test_branch_targets_recovers_a_frozen_frame_table_after_the_first_route_jump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, jump = _frozen_frame_table_query()
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target == 0x4200,
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4200,),
        ),
    )

    result = provider.branch_targets(query)

    assert result == _CompleteBatch((_BranchTargetFact(jump, (0x4200,)),))


def test_branch_targets_rejects_a_frozen_frame_table_after_a_slot_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, _jump = _frozen_frame_table_query(overwrite_index=True)
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target == 0x4200,
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4200,),
        ),
    )

    result = provider.branch_targets(query)

    assert result == _CompleteBatch(())


def test_branch_targets_reuses_a_current_instruction_snapshot_for_selector_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a selector shape whose current LLIL instructions can be snapshotted exactly once.
    query, jump = _selector_table_query()
    provider, _registered = _load_provider(
        monkeypatch,
        None,
        lambda target: target in (0x4100, 0x4200),
        policy=_InitializedDataPolicy(
            table_slot=0x3000,
            target=0x4100,
            extra_targets=(0x4200,),
        ),
    )

    # When the selector proof reuses that snapshot for all current-index reads.
    result = provider.branch_targets(query)

    # Then it returns the same facts without invoking the IL index accessor repeatedly.
    condition = query.llil.instructions[4].src
    assert result == _CompleteBatch(
        (_BranchTargetFact(jump, (0x4100, 0x4200), condition, 0x4200, 0x4100),)
    )
    assert query.llil.read_count == [0]


def test_reaches_stop_visits_a_shared_unreachable_dag_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _registered = _load_provider(monkeypatch, None, lambda _target: False)
    llil, blocks, stop_index = _shared_unreachable_dag()

    # Given a dispatcher DAG whose many paths share every downstream block.
    # When a target block is not reachable through any current JUMP_TO route.
    # Then path proof visits the shared graph once instead of expanding all paths.
    assert not module._reaches_stop(llil, 0, stop_index, set())
    assert blocks.iterations == 1


def test_jump_target_for_stop_reuses_the_current_reachability_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _registered = _load_provider(monkeypatch, None, lambda _target: False)
    llil, _blocks, stop_index = _shared_unreachable_dag()
    blocks_by_instruction = module._blocks_by_instruction(llil)
    predecessors = module._jump_predecessors(llil, blocks_by_instruction)
    reachable_by_stop: dict[int, set[int]] = {}
    llil.read_count[0] = 0

    # Given one current dispatcher graph and a shared unreachable target block.
    # When several arm proofs ask whether the same target can reach that block.
    # Then they reuse one reverse-reachability result rather than replaying the graph.
    for _ in range(10):
        assert (
            module._jump_target_for_stop(
                llil,
                llil[0],
                stop_index,
                blocks_by_instruction,
                predecessors,
                reachable_by_stop,
            )
            is None
        )
    assert llil.read_count[0] < 100


def test_path_state_before_reuses_a_proven_linear_route_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _registered = _load_provider(monkeypatch, None, lambda _target: False)
    sp = _Register("sp", 1)
    x22 = _Register("x22", 22)

    def register(value: _Register) -> _Expression:
        return _Expression(_Operation("LLIL_REG"), src=value)

    def constant(value: int) -> _Expression:
        return _Expression(_Operation("LLIL_CONST_PTR"), constant=value)

    instructions = (
        _Instruction(
            _Operation("LLIL_SET_REG"),
            sp,
            _Expression(
                _Operation("LLIL_SUB"), operands=(register(sp), constant(0x20))
            ),
            0,
        ),
        _Instruction(_Operation("LLIL_SET_REG"), x22, register(sp), 1),
        _Instruction(_Operation("LLIL_JUMP_TO"), None, None, 2, targets={0x100C: 3}),
        _Instruction(_Operation("LLIL_NOP"), None, None, 3),
    )
    view = _View(_Architecture())
    llil = _Llil(
        instructions,
        _SsaIl("ssa"),
        (_BasicBlock(0, 3), _BasicBlock(3, 4)),
    )
    instructions_by_index = {
        instruction.instr_index: replace(instruction)
        for instruction in llil.instructions
    }
    prefix_state = module._route_prefix_state(view, llil, instructions_by_index)
    blocks_by_instruction = module._blocks_by_instruction(llil)
    predecessors = module._jump_predecessors(
        llil, blocks_by_instruction, instructions_by_index
    )
    observed_steps: list[int] = []
    original_step = module._step_literal

    def count_steps(architecture, instruction, registers, stack_values):
        observed_steps.append(instruction.instr_index)
        return original_step(architecture, instruction, registers, stack_values)

    monkeypatch.setattr(module, "_step_literal", count_steps)

    # Given a fully modeled straight-line prefix before the first current routing jump.
    assert prefix_state is not None

    # When a later target reuses its copied prefix state for path proof.
    state = module._path_state_before(
        view,
        llil,
        3,
        blocks_by_instruction,
        predecessors,
        {},
        instructions_by_index,
        prefix_state,
    )

    # Then it retains the exact state without reinterpreting prefix instructions.
    assert state == (list(prefix_state[1]), list(prefix_state[2]))
    assert observed_steps == []


def test_vector_lane_duplicate_preserves_proven_stack_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _registered = _load_provider(monkeypatch, None, lambda _target: False)
    v0 = _Register("v0", 1000)
    x8 = _Register("x8", 8)
    intrinsic = _Instruction(
        _Operation("LLIL_INTRINSIC"),
        None,
        None,
        0,
        params=(
            _Expression(_Operation("LLIL_REG"), src=v0),
            _Expression(_Operation("LLIL_CONST"), constant=2),
        ),
        intrinsic=_Intrinsic("vdupq_laneq_s32"),
    )
    registers = [(x8, 0x1234)]
    stack_values = [(0x20, 8, 0x4000)]

    # Given a ppsp vector-lane duplicate with only register and constant operands.
    # When it occurs between a proven stack-table store and its later table load.
    # Then it preserves the stack proof while forgetting volatile register values.
    assert module._step_literal(_Architecture(), intrinsic, registers, stack_values)
    assert registers == []
    assert stack_values == [(0x20, 8, 0x4000)]


def test_other_intrinsics_still_invalidate_stack_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _registered = _load_provider(monkeypatch, None, lambda _target: False)
    v0 = _Register("v0", 1000)
    intrinsic = _Instruction(
        _Operation("LLIL_INTRINSIC"),
        None,
        None,
        0,
        params=(
            _Expression(_Operation("LLIL_REG"), src=v0),
            _Expression(_Operation("LLIL_CONST"), constant=2),
        ),
        intrinsic=_Intrinsic("_ReadMSR"),
    )
    registers = []
    stack_values = [(0x20, 8, 0x4000)]

    # Given an intrinsic whose memory behavior is not the ppsp vector duplicate.
    # When it appears during literal table reconstruction.
    # Then the provider keeps the conservative stack invalidation rule.
    assert module._step_literal(_Architecture(), intrinsic, registers, stack_values)
    assert stack_values == []
