import types

import binaryninja

from conftest import load_plugin_module


class OldInstruction:
    def __init__(self, index):
        self.instr_index = index
        self.address = 0x1000 + index

    def copy_to(self, _new_mlil):
        return ("copy", self.instr_index)


class OldMLIL:
    arch = "aarch64"
    llil = "old-llil"

    def __init__(self):
        self.basic_blocks = [types.SimpleNamespace(start=0, end=3, arch="aarch64")]
        self.instructions = [OldInstruction(index) for index in range(3)]

    def __getitem__(self, index):
        return self.instructions[index]


class NewMLIL:
    def __init__(self, arch, handle=None, source_func=None, low_level_il=None):
        self.arch = arch
        self.handle = handle
        self.source_func = source_func
        self.low_level_il = low_level_il
        self.appended = []
        self.finalized = False
        self.ssa = False

    def prepare_to_copy_function(self, old_mlil):
        self.old_mlil = old_mlil

    def prepare_to_copy_block(self, old_block):
        self.old_block = old_block

    def set_current_address(self, address, arch=None):
        self.current_address = (address, arch)

    def append(self, expr, loc=None):
        self.appended.append((expr, loc))

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        self.ssa = True


def test_copy_mlil_with_instruction_rewrites_copies_unchanged_instructions(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    ctx = types.SimpleNamespace(mlil=OldMLIL(), llil="context-llil")

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        ctx,
        {1: lambda _new_mlil, old_ins: ("rewrite", old_ins.instr_index)},
    )

    assert applied == 1
    assert new_mlil.low_level_il == "context-llil"
    assert new_mlil.appended == [
        (("copy", 0), ("loc", None)),
        (("rewrite", 1), ("loc", None)),
        (("copy", 2), ("loc", None)),
    ]
    assert new_mlil.finalized
    assert new_mlil.ssa
