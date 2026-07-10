import types

from conftest import load_plugin_module


correlated_stores = load_plugin_module("plugins.DispatchThis.passes.medium.correlated_stores")


class Instruction:
    def __init__(self, instr_index):
        self.instr_index = instr_index
        self.expr_index = 100 + instr_index


class OldMLIL:
    def __init__(self, instructions):
        self.instructions = {instruction.instr_index: instruction for instruction in instructions}

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
    join_store = Instruction(10)
    goto = Instruction(1)
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
    goto_a = Instruction(1)
    goto_b = Instruction(2)
    join_store = Instruction(10)
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


if __name__ == "__main__":
    class MonkeyPatch:
        @staticmethod
        def setattr(obj, name, value):
            setattr(obj, name, value)

    test_apply_correlated_stores_rejects_malformed_plan(MonkeyPatch())
    test_apply_correlated_stores_emits_arm_local_stores_and_nops_join(MonkeyPatch())
