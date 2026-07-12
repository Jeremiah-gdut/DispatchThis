import types

import pytest
from binaryninja import MediumLevelILOperation as M

from conftest import load_plugin_module


correlated_stores = load_plugin_module("plugins.DispatchThis.passes.medium.correlated_stores")


class Instruction:
    def __init__(self, instr_index, operation, *, address=None, size=None, **attrs):
        self.instr_index = instr_index
        self.expr_index = 100 + instr_index
        self.address = 0x1000 + instr_index if address is None else address
        self.operation = operation
        if size is not None:
            self.size = size
        if operation == M.MLIL_GOTO:
            self.dest = attrs.pop("dest", instr_index)
        elif operation == M.MLIL_STORE:
            self.dest = attrs.pop("dest", types.SimpleNamespace(
                expr_index=1000 + instr_index * 2,
                operation=M.MLIL_CONST_PTR,
                size=8,
            ))
            self.src = attrs.pop("src", types.SimpleNamespace(
                expr_index=1001 + instr_index * 2,
                operation=M.MLIL_CONST,
                size=size,
            ))
        self.__dict__.update(attrs)


class OldMLIL:
    def __init__(self, instructions):
        self.instructions = {instruction.instr_index: instruction for instruction in instructions}
        for instruction in instructions:
            instruction.function = self

    def __getitem__(self, instr_index):
        return self.instructions[instr_index]


class NewMLIL:
    arch = types.SimpleNamespace(address_size=8)

    def const_pointer(self, size, value, loc=None):
        return ("const_pointer", size, value, loc)

    def load(self, size, src, loc=None):
        return ("load", size, src, loc)

    def store(self, size, dest, src, loc=None):
        return ("store", size, dest, src, loc)

    def nop(self, loc=None):
        return ("nop", loc)


def test_apply_correlated_stores_rejects_malformed_plan(monkeypatch):
    join_store = Instruction(10, M.MLIL_STORE, size=4)
    goto = Instruction(1, M.MLIL_GOTO)
    mlil = OldMLIL([goto, join_store])
    calls = []

    monkeypatch.setattr(
        correlated_stores,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: calls.append(True),
    )

    new_mlil, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil),
        mlil,
        [{"store": join_store, "size": 4, "arms": [{"goto": goto, "dest": 0x1000}]}],
    )

    assert new_mlil is None
    assert applied == 0
    assert calls == []


def test_apply_correlated_stores_emits_arm_local_stores_and_nops_join(monkeypatch):
    goto_a = Instruction(1, M.MLIL_GOTO)
    goto_b = Instruction(2, M.MLIL_GOTO)
    join_store = Instruction(10, M.MLIL_STORE, size=4)
    mlil = OldMLIL([goto_a, goto_b, join_store])
    built = {}
    new = NewMLIL()

    def fake_copy(_ctx, replacements, mlil=None, preludes=None):
        built["replacements"] = {
            index: replacement(new, mlil[index])
            for index, replacement in replacements.items()
        }
        built["preludes"] = {
            index: tuple(prelude(new, mlil[index]))
            for index, prelude in preludes.items()
        }
        return new, len(set(replacements) | set(preludes))

    monkeypatch.setattr(correlated_stores, "copy_mlil_with_instruction_rewrites", fake_copy)

    result, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil),
        mlil,
        [
            {
                "store": join_store,
                "size": 4,
                "arms": [
                    {"goto": goto_a, "dest": 0x1000, "src": 0x2000},
                    {"goto": goto_b, "dest": 0x1004, "src": 0x2004},
                ],
            }
        ],
    )

    assert result is new
    assert applied == 1
    assert built["replacements"] == {10: ("nop", ("loc", 110))}
    assert built["preludes"] == {
        1: (
            (
                "store",
                4,
                ("const_pointer", 8, 0x1000, ("loc", 101)),
                (
                    "load",
                    4,
                    ("const_pointer", 8, 0x2000, ("loc", 101)),
                    ("loc", 101),
                ),
                ("loc", 101),
            ),
        ),
        2: (
            (
                "store",
                4,
                ("const_pointer", 8, 0x1004, ("loc", 102)),
                (
                    "load",
                    4,
                    ("const_pointer", 8, 0x2004, ("loc", 102)),
                    ("loc", 102),
                ),
                ("loc", 102),
            ),
        ),
    }


def test_apply_correlated_stores_rejects_duplicate_arm_goto(monkeypatch):
    goto = Instruction(1, M.MLIL_GOTO)
    join_store = Instruction(10, M.MLIL_STORE, size=4)
    mlil = OldMLIL([goto, join_store])
    calls = []
    monkeypatch.setattr(
        correlated_stores,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: calls.append(True),
    )

    new_mlil, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil),
        mlil,
        [{
            "store": join_store,
            "size": 4,
            "arms": (
                {"goto": goto, "dest": 0x1000, "src": 0x2000},
                {"goto": goto, "dest": 0x1004, "src": 0x2004},
            ),
        }],
    )

    assert new_mlil is None
    assert applied == 0
    assert calls == []


@pytest.mark.parametrize("field", ["operation", "expr_index", "address"])
def test_apply_correlated_stores_rejects_stale_store_witness(monkeypatch, field):
    goto_a = Instruction(1, M.MLIL_GOTO)
    goto_b = Instruction(2, M.MLIL_GOTO)
    current_store = Instruction(10, M.MLIL_STORE, size=4)
    recorded_store = Instruction(10, M.MLIL_STORE, size=4)
    if field == "operation":
        recorded_store.operation = M.MLIL_SET_VAR
    else:
        setattr(recorded_store, field, getattr(recorded_store, field) + 1)
    mlil = OldMLIL([goto_a, goto_b, current_store])
    recorded_store.function = mlil
    calls = []
    monkeypatch.setattr(
        correlated_stores,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: calls.append(True),
    )

    new_mlil, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil),
        mlil,
        [{
            "store": recorded_store,
            "size": 4,
            "arms": (
                {"goto": goto_a, "dest": 0x1000, "src": 0x2000},
                {"goto": goto_b, "dest": 0x1004, "src": 0x2004},
            ),
        }],
    )

    assert new_mlil is None
    assert applied == 0
    assert calls == []


@pytest.mark.parametrize("field", ["address", "dest"])
def test_apply_correlated_stores_rejects_stale_goto_witness(monkeypatch, field):
    goto_a = Instruction(1, M.MLIL_GOTO)
    stale_goto_a = Instruction(1, M.MLIL_GOTO)
    setattr(stale_goto_a, field, getattr(stale_goto_a, field) + 1)
    goto_b = Instruction(2, M.MLIL_GOTO)
    join_store = Instruction(10, M.MLIL_STORE, size=4)
    mlil = OldMLIL([goto_a, goto_b, join_store])
    stale_goto_a.function = mlil
    calls = []
    monkeypatch.setattr(
        correlated_stores,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: calls.append(True),
    )

    new_mlil, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil),
        mlil,
        [{
            "store": join_store,
            "size": 4,
            "arms": (
                {"goto": stale_goto_a, "dest": 0x1000, "src": 0x2000},
                {"goto": goto_b, "dest": 0x1004, "src": 0x2004},
            ),
        }],
    )

    assert new_mlil is None
    assert applied == 0
    assert calls == []


def test_apply_correlated_stores_rejects_foreign_owner(monkeypatch):
    goto_a = Instruction(1, M.MLIL_GOTO)
    goto_b = Instruction(2, M.MLIL_GOTO)
    current_store = Instruction(10, M.MLIL_STORE, size=4)
    recorded_store = Instruction(10, M.MLIL_STORE, size=4)
    mlil = OldMLIL([goto_a, goto_b, current_store])
    recorded_store.function = object()
    calls = []
    monkeypatch.setattr(
        correlated_stores,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: calls.append(True),
    )

    new_mlil, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil),
        mlil,
        [{
            "store": recorded_store,
            "size": 4,
            "arms": (
                {"goto": goto_a, "dest": 0x1000, "src": 0x2000},
                {"goto": goto_b, "dest": 0x1004, "src": 0x2004},
            ),
        }],
    )

    assert new_mlil is None
    assert applied == 0
    assert calls == []


@pytest.mark.parametrize("field", ["dest", "src"])
def test_apply_correlated_stores_rejects_stale_store_operand(monkeypatch, field):
    goto_a = Instruction(1, M.MLIL_GOTO)
    goto_b = Instruction(2, M.MLIL_GOTO)
    current_store = Instruction(10, M.MLIL_STORE, size=4)
    recorded_store = Instruction(10, M.MLIL_STORE, size=4)
    mlil = OldMLIL([goto_a, goto_b, current_store])
    recorded_store.function = mlil
    operand = getattr(recorded_store, field)
    setattr(recorded_store, field, types.SimpleNamespace(
        expr_index=operand.expr_index + 1,
        operation=operand.operation,
        size=operand.size,
    ))
    calls = []
    monkeypatch.setattr(
        correlated_stores,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: calls.append(True),
    )

    new_mlil, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil),
        mlil,
        [{
            "store": recorded_store,
            "size": 4,
            "arms": (
                {"goto": goto_a, "dest": 0x1000, "src": 0x2000},
                {"goto": goto_b, "dest": 0x1004, "src": 0x2004},
            ),
        }],
    )

    assert new_mlil is None
    assert applied == 0
    assert calls == []


if __name__ == "__main__":
    class MonkeyPatch:
        @staticmethod
        def setattr(obj, name, value):
            setattr(obj, name, value)

    test_apply_correlated_stores_rejects_malformed_plan(MonkeyPatch())
    test_apply_correlated_stores_emits_arm_local_stores_and_nops_join(MonkeyPatch())
