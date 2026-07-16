import types

from binaryninja import LowLevelILOperation, MediumLevelILOperation

from conftest import load_plugin_module


branch_conditions = load_plugin_module("plugins.DispatchThis.passes.medium.branch_conditions")


class LLILExpr:
    _next_index = 1

    def __init__(self, op, address, source_operand, *, size=1, operands=()):
        self.operation = LowLevelILOperation[op]
        self.address = address
        self.source_operand = source_operand
        self.size = size
        self.expr_index = LLILExpr._next_index
        LLILExpr._next_index += 1
        self.detailed_operands = [
            (name, value, "LowLevelILInstruction") for name, value in operands
        ]
        self.mlils = []


class MLILExpr:
    _next_index = 100

    def __init__(self, op, *, address=0, instr_index=None, children=(), **attrs):
        self.operation = MediumLevelILOperation[op]
        self.address = address
        self.instr_index = MLILExpr._next_index if instr_index is None else instr_index
        self.expr_index = self.instr_index
        MLILExpr._next_index += 1
        self.children = list(children)
        self.function = None
        for key, value in attrs.items():
            setattr(self, key, value)

    def traverse(self, visit):
        out = [visit(self)]
        for child in self.children:
            out.extend(child.traverse(visit))
        return out

    def copy_to(self, new_mlil):
        new_mlil.copied.append(self)
        return ("copied", self.expr_index)


class FakeLLIL:
    def __init__(self, *instructions):
        self.instructions = list(instructions)


class FakeMLIL:
    def __init__(self, *instructions):
        self.instructions = list(instructions)
        self._by_index = {instruction.instr_index: instruction for instruction in instructions}
        self.basic_blocks = [
            types.SimpleNamespace(start=instruction.instr_index)
            for instruction in instructions
        ]
        for instruction in instructions:
            instruction.function = self

    def __getitem__(self, index):
        return self._by_index[index]

    @staticmethod
    def get_var_definitions(_variable):
        return []


class FakeCopiedMLIL:
    def __init__(self):
        self.copied = []
        self.replacements = []

    def if_expr(self, condition, true_label, false_label, location):
        replacement = (condition, true_label, false_label, location)
        self.replacements.append(replacement)
        return replacement


def _condition_tree(address=0x1000, source_operand=0):
    register = LLILExpr("LLIL_REG", address, source_operand)
    condition = LLILExpr(
        "LLIL_CMP_NE",
        address,
        source_operand,
        operands=(("left", register),),
    )
    root = LLILExpr(
        "LLIL_JUMP",
        address,
        source_operand,
        operands=(("dest", condition),),
    )
    return root, condition


def _jump_site(
    source=0x2000,
    true_target=0x3000,
    false_target=0x4000,
    true_index=31,
    false_index=32,
    true_block_address=None,
    false_block_address=None,
):
    destination = MLILExpr("MLIL_CONST")
    true_block = MLILExpr(
        "MLIL_NOP",
        address=true_target if true_block_address is None else true_block_address,
        instr_index=true_index,
    )
    false_block = MLILExpr(
        "MLIL_NOP",
        address=false_target if false_block_address is None else false_block_address,
        instr_index=false_index,
    )
    jump = MLILExpr(
        "MLIL_JUMP_TO",
        address=source,
        instr_index=30,
        dest=destination,
        targets={true_target: true_block.instr_index, false_target: false_block.instr_index},
    )
    return jump, true_block, false_block


def _copy_backend(monkeypatch, source_mlil):
    copied = FakeCopiedMLIL()

    def copy(_ctx, replacements, mlil=None):
        assert mlil is source_mlil
        for instruction_index, replacement in replacements.items():
            replacement(copied, source_mlil[instruction_index])
        return copied, len(replacements)

    monkeypatch.setattr(branch_conditions, "copy_mlil_with_instruction_rewrites", copy)
    monkeypatch.setattr(
        branch_conditions,
        "copied_label_for_source",
        lambda _mlil, index: f"label:{index}",
    )
    return copied


def test_captures_nested_scalar_anchor_and_rebinds_after_reanalysis(monkeypatch):
    old_root, old_condition = _condition_tree()
    receipt = branch_conditions.capture_condition_receipt(
        FakeLLIL(old_root),
        0x2000,
        old_condition,
        0x3000,
        0x4000,
    )

    assert receipt is not None
    assert receipt.anchor.owner_source == 0x1000
    assert receipt.anchor.source_operand == 0
    assert receipt.anchor.operand_path == (("dest", -1),)

    current_root, current_condition = _condition_tree()
    jump, true_block, false_block = _jump_site()
    mlil = FakeMLIL(jump, true_block, false_block)
    current_condition_mlil = MLILExpr("MLIL_CMP_NE")
    current_condition_mlil.function = mlil
    current_condition.mlils = [current_condition_mlil]
    copied = _copy_backend(monkeypatch, mlil)

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(current_root),
        mlil,
        (receipt,),
    )

    assert result.results[0].status is branch_conditions.ConditionTranslationStatus.REWRITE_READY
    assert result.rewrite_sources == frozenset({0x2000})
    assert copied.copied == [current_condition_mlil]
    assert copied.replacements[0][1:3] == ("label:31", "label:32")


def test_rejects_ambiguous_current_mlil_mapping_without_fallback():
    root, condition = _condition_tree()
    receipt = branch_conditions.capture_condition_receipt(
        FakeLLIL(root),
        0x2000,
        condition,
        0x3000,
        0x4000,
    )
    jump, true_block, false_block = _jump_site()
    mlil = FakeMLIL(jump, true_block, false_block)
    first = MLILExpr("MLIL_CMP_NE")
    second = MLILExpr("MLIL_CMP_NE")
    first.function = mlil
    second.function = mlil
    condition.mlils = [first, second]

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(root),
        mlil,
        (receipt,),
    )

    translated = result.results[0]
    assert translated.status is branch_conditions.ConditionTranslationStatus.FAILED
    assert translated.failure.reason is branch_conditions.ConditionFailureReason.MLIL_MAPPING_AMBIGUOUS
    assert result.rewrite_sources == frozenset()


def test_selects_unique_nested_condition_mapping_over_its_parent(monkeypatch):
    root, condition = _condition_tree()
    receipt = branch_conditions.capture_condition_receipt(
        FakeLLIL(root),
        0x2000,
        condition,
        0x3000,
        0x4000,
    )
    jump, true_block, false_block = _jump_site()
    mlil = FakeMLIL(jump, true_block, false_block)
    current_condition = MLILExpr("MLIL_CMP_NE")
    parent = MLILExpr(
        "MLIL_SET_VAR",
        detailed_operands=[("src", current_condition, "MediumLevelILInstruction")],
    )
    current_condition.function = mlil
    parent.function = mlil
    condition.mlils = [current_condition, parent]
    copied = _copy_backend(monkeypatch, mlil)

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(root),
        mlil,
        (receipt,),
    )

    assert result.results[0].status is branch_conditions.ConditionTranslationStatus.REWRITE_READY
    assert result.rewrite_sources == frozenset({0x2000})
    assert copied.copied == [current_condition]


def test_uses_target_map_block_starts_when_instruction_addresses_differ(monkeypatch):
    root, condition = _condition_tree()
    receipt = branch_conditions.capture_condition_receipt(
        FakeLLIL(root),
        0x2000,
        condition,
        0x3000,
        0x4000,
    )
    jump, true_block, false_block = _jump_site(
        true_block_address=0x3010,
        false_block_address=0x4010,
    )
    mlil = FakeMLIL(jump, true_block, false_block)
    current_condition = MLILExpr("MLIL_CMP_NE")
    current_condition.function = mlil
    condition.mlils = [current_condition]
    copied = _copy_backend(monkeypatch, mlil)

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(root),
        mlil,
        (receipt,),
    )

    assert result.results[0].status is branch_conditions.ConditionTranslationStatus.REWRITE_READY
    assert result.rewrite_sources == frozenset({0x2000})
    assert copied.replacements[0][1:3] == ("label:31", "label:32")


def test_rejects_target_map_index_that_is_not_a_current_block_start():
    root, condition = _condition_tree()
    receipt = branch_conditions.capture_condition_receipt(
        FakeLLIL(root),
        0x2000,
        condition,
        0x3000,
        0x4000,
    )
    jump, true_block, false_block = _jump_site()
    mlil = FakeMLIL(jump, true_block, false_block)
    mlil.basic_blocks = [types.SimpleNamespace(start=true_block.instr_index)]
    current_condition = MLILExpr("MLIL_CMP_NE")
    current_condition.function = mlil
    condition.mlils = [current_condition]

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(root),
        mlil,
        (receipt,),
    )

    translated = result.results[0]
    assert translated.status is branch_conditions.ConditionTranslationStatus.FAILED
    assert translated.failure.reason is branch_conditions.ConditionFailureReason.TARGET_MISMATCH


def test_exact_current_if_is_already_satisfied_without_old_cleanup_ownership():
    root, condition = _condition_tree()
    receipt = branch_conditions.capture_condition_receipt(
        FakeLLIL(root),
        0x2000,
        condition,
        0x3000,
        0x4000,
    )
    true_block = MLILExpr("MLIL_NOP", address=0x3000, instr_index=31)
    false_block = MLILExpr("MLIL_NOP", address=0x4000, instr_index=32)
    current_condition = MLILExpr("MLIL_CMP_NE")
    current_if = MLILExpr(
        "MLIL_IF",
        address=0x2000,
        instr_index=30,
        condition=current_condition,
        true=true_block.instr_index,
        false=false_block.instr_index,
    )
    mlil = FakeMLIL(current_if, true_block, false_block)
    current_condition.function = mlil
    condition.mlils = [current_condition]

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(root),
        mlil,
        (receipt,),
    )

    assert result.results[0].status is branch_conditions.ConditionTranslationStatus.ALREADY_SATISFIED
    assert result.rewrite_sources == frozenset()
    assert result.cleanup_roots == frozenset()


def test_mixed_ready_and_failed_sites_use_one_copy_batch_for_ready_sites(monkeypatch):
    first_root, first_condition = _condition_tree(0x1000, 0)
    second_root, second_condition = _condition_tree(0x1100, 1)
    first = branch_conditions.capture_condition_receipt(
        FakeLLIL(first_root), 0x2000, first_condition, 0x3000, 0x4000
    )
    second = branch_conditions.capture_condition_receipt(
        FakeLLIL(second_root), 0x2100, second_condition, 0x5000, 0x6000
    )
    first_jump, first_true, first_false = _jump_site(0x2000, 0x3000, 0x4000)
    second_jump, second_true, second_false = _jump_site(
        0x2100,
        0x5000,
        0x6000,
        41,
        42,
    )
    mlil = FakeMLIL(
        first_jump,
        first_true,
        first_false,
        second_jump,
        second_true,
        second_false,
    )
    first_current = MLILExpr("MLIL_CMP_NE")
    first_current.function = mlil
    first_condition.mlils = [first_current]
    second_condition.mlils = []
    copied = _copy_backend(monkeypatch, mlil)

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(first_root, second_root),
        mlil,
        (first, second),
    )

    by_source = {item.source: item for item in result.results}
    assert by_source[0x2000].status is branch_conditions.ConditionTranslationStatus.REWRITE_READY
    assert by_source[0x2100].failure.reason is branch_conditions.ConditionFailureReason.MLIL_MAPPING_MISSING
    assert result.rewrite_sources == frozenset({0x2000})
    assert len(copied.replacements) == 1


def test_no_receipt_means_no_condition_translation_work(monkeypatch):
    jump, true_block, false_block = _jump_site()
    mlil = FakeMLIL(jump, true_block, false_block)
    copied = _copy_backend(monkeypatch, mlil)

    result = branch_conditions.translate_indirect_branch_conditions(
        types.SimpleNamespace(),
        FakeLLIL(),
        mlil,
        (),
    )

    assert result.results == ()
    assert result.rewrite_sources == frozenset()
    assert copied.replacements == []
