import types

import binaryninja

from conftest import load_plugin_module


class OldInstruction:
    def __init__(self, index):
        self.instr_index = index
        self.expr_index = 100 + index
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
    instances = []

    def __init__(self, arch, handle=None, source_func=None, low_level_il=None):
        self.arch = arch
        self.handle = handle
        self.source_func = source_func
        self.low_level_il = low_level_il
        self.appended = []
        self.prepared_blocks = []
        self.current_addresses = []
        self.finalized = False
        self.ssa = False
        self.instances.append(self)

    def prepare_to_copy_function(self, old_mlil):
        self.old_mlil = old_mlil

    def prepare_to_copy_block(self, old_block):
        self.prepared_blocks.append(old_block)

    def set_current_address(self, address, arch=None):
        self.current_addresses.append((address, arch))

    def append(self, expr, loc=None):
        self.appended.append((expr, loc))

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        self.ssa = True


def test_copy_mlil_with_instruction_rewrites_copies_unchanged_instructions(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    old_mlil = OldMLIL()
    ctx = types.SimpleNamespace(mlil=old_mlil, llil="context-llil")

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        ctx,
        {1: lambda _new_mlil, old_ins: ("rewrite", old_ins.instr_index)},
    )

    assert applied == 1
    assert new_mlil.old_mlil is old_mlil
    assert new_mlil.prepared_blocks == old_mlil.basic_blocks
    assert new_mlil.low_level_il == "context-llil"
    assert new_mlil.current_addresses == [
        (0x1000, "aarch64"),
        (0x1001, "aarch64"),
        (0x1002, "aarch64"),
    ]
    assert new_mlil.appended == [
        (("copy", 0), ("loc", 100)),
        (("rewrite", 1), ("loc", 101)),
        (("copy", 2), ("loc", 102)),
    ]
    assert new_mlil.finalized
    assert new_mlil.ssa


def test_copy_mlil_with_instruction_rewrites_appends_preludes_before_source_instruction(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    old_mlil = OldMLIL()

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        types.SimpleNamespace(mlil=old_mlil, llil="context-llil"),
        {1: lambda _new_mlil, old_ins: ("rewrite", old_ins.instr_index)},
        preludes={
            1: lambda _new_mlil, old_ins: [("prelude", old_ins.instr_index)],
            2: lambda _new_mlil, old_ins: [("prelude", old_ins.instr_index)],
        },
    )

    assert applied == 2
    assert new_mlil.appended == [
        (("copy", 0), ("loc", 100)),
        (("prelude", 1), ("loc", 101)),
        (("rewrite", 1), ("loc", 101)),
        (("prelude", 2), ("loc", 102)),
        (("copy", 2), ("loc", 102)),
    ]


def test_copy_mlil_with_instruction_rewrites_rejects_missing_replacement(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    old_mlil = OldMLIL()
    NewMLIL.instances.clear()

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        types.SimpleNamespace(mlil=old_mlil, llil="context-llil"),
        {
            1: lambda _new_mlil, _old_ins: "rewrite",
            99: lambda _new_mlil, _old_ins: "missing",
        },
    )

    assert new_mlil is old_mlil
    assert applied == 0
    assert NewMLIL.instances[-1].finalized is False
    assert NewMLIL.instances[-1].ssa is False


def test_copy_mlil_with_instruction_rewrites_rejects_callback_failure(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    old_mlil = OldMLIL()
    NewMLIL.instances.clear()

    def fail(_new_mlil, _old_ins):
        raise RuntimeError("rewrite failed")

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        types.SimpleNamespace(mlil=old_mlil, llil="context-llil"),
        {
            0: lambda _new_mlil, _old_ins: "rewrite",
            1: fail,
        },
    )

    assert new_mlil is old_mlil
    assert applied == 0
    assert NewMLIL.instances[-1].finalized is False
    assert NewMLIL.instances[-1].ssa is False


def test_copy_mlil_with_instruction_rewrites_rejects_missing_prelude(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    old_mlil = OldMLIL()
    NewMLIL.instances.clear()

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        types.SimpleNamespace(mlil=old_mlil, llil="context-llil"),
        {},
        preludes={99: lambda _new_mlil, _old_ins: ["prelude"]},
    )

    assert new_mlil is old_mlil
    assert applied == 0
    assert NewMLIL.instances[-1].finalized is False
    assert NewMLIL.instances[-1].ssa is False


def test_copy_mlil_with_instruction_rewrites_rejects_empty_prelude(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    old_mlil = OldMLIL()
    NewMLIL.instances.clear()

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        types.SimpleNamespace(mlil=old_mlil, llil="context-llil"),
        {},
        preludes={1: lambda _new_mlil, _old_ins: []},
    )

    assert new_mlil is old_mlil
    assert applied == 0
    assert NewMLIL.instances[-1].finalized is False
    assert NewMLIL.instances[-1].ssa is False


def test_copy_mlil_with_instruction_rewrites_rejects_failing_prelude(monkeypatch):
    monkeypatch.setattr(binaryninja, "MediumLevelILFunction", NewMLIL, raising=False)
    rewrite = load_plugin_module("plugins.DispatchThis.passes.medium.rewrite")
    old_mlil = OldMLIL()
    NewMLIL.instances.clear()

    def fail(_new_mlil, _old_ins):
        raise RuntimeError("prelude failed")

    new_mlil, applied = rewrite.copy_mlil_with_instruction_rewrites(
        types.SimpleNamespace(mlil=old_mlil, llil="context-llil"),
        {},
        preludes={1: fail},
    )

    assert new_mlil is old_mlil
    assert applied == 0
    assert NewMLIL.instances[-1].finalized is False
    assert NewMLIL.instances[-1].ssa is False
