import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.DispatchThis.workflow_state import (
    CLEANUP_RECEIPT_VERSION,
    FunctionWorkflowState,
    ROOT_KEY,
)


class FakeBranch:
    def __init__(self, source_addr, dest_addr=0x2000, auto_defined=False):
        self.source_addr = source_addr
        self.dest_addr = dest_addr
        self.auto_defined = auto_defined


class NativeType:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, NativeType) and self.name == other.name


class FakeFunction:
    def __init__(self):
        self.session_data = {}
        self.unresolved_indirect_branches = []
        self.indirect_branches = []
        self.call_adjustments = {}

    def get_call_type_adjustment(self, call_addr):
        return self.call_adjustments.get(call_addr)

    def set_call_type_adjustment(self, call_addr, adjust_type=None):
        if adjust_type is None:
            self.call_adjustments.pop(call_addr, None)
        else:
            self.call_adjustments[call_addr] = adjust_type


def test_branch_receipts_gate_repeated_mutations_and_invalidate_calls():
    func = FakeFunction()
    func.unresolved_indirect_branches = [("aarch64", 0x1000)]
    state = FunctionWorkflowState(func)

    assert state.unmapped_unresolved_sources(func) == {0x1000}

    first_plan = {0x1000: (0x2000, 0x3000)}
    assert state.branch_updates_for(first_plan) == first_plan
    assert state.mark_branch_applied(0x1000, first_plan[0x1000]) is False
    assert state.branch_updates_for(first_plan) == first_plan

    func.indirect_branches = [FakeBranch(0x1000, 0x2000), FakeBranch(0x1000, 0x3000)]
    state.mark_branch_stable()
    assert state.branch_updates_for(first_plan) == {}
    assert state.branch_stable(func)

    func.set_call_type_adjustment(0x4000, "type-a")
    state.mark_call_adjusted(0x4000, 0x5000)
    assert not state.call_adjustment_needed(0x4000, "type-a")

    changed_plan = {0x1000: (0x2000, 0x4000)}
    assert state.branch_updates_for(changed_plan) == changed_plan
    assert state.mark_branch_applied(0x1000, changed_plan[0x1000]) is True
    assert state.call_receipts == {}
    assert not state.branch_stable(func)


def test_call_receipts_gate_repeated_adjustments():
    func = FakeFunction()
    state = FunctionWorkflowState(func)

    assert not state.call_stable()
    assert state.call_adjustment_needed(0x1111, "type-a")
    func.set_call_type_adjustment(0x1111, "type-a")
    assert state.mark_call_adjusted(0x1111, 0x2222) is False
    assert not state.call_adjustment_needed(0x1111, "type-a")
    assert state.call_adjustment_needed(0x1111, "type-b")
    func.set_call_type_adjustment(0x1111, "type-b")
    assert state.mark_call_adjusted(0x1111, 0x3333) is True
    assert not state.call_adjustment_needed(0x1111, "type-b")
    state.mark_call_stable()
    assert state.call_stable()


def test_call_target_receipts_feed_cleanup_without_gating_type_adjustments():
    func = FakeFunction()
    state = FunctionWorkflowState(func)

    state.mark_call_cleanup_done()
    assert state.mark_call_target(0x1111, 0x2222) is False
    assert state.call_target_receipts == {0x1111: 0x2222}
    assert state.call_adjustment_needed(0x1111, "type-a")
    assert state.call_cleanup_needed()

    state.mark_call_adjusted(0x1111, 0x2222)
    func.set_call_type_adjustment(0x1111, "type-a")
    state.mark_call_cleanup_done()
    assert state.mark_call_target(0x1111, 0x3333) is True
    assert state.call_receipts == {}
    assert not state.call_adjustment_needed(0x1111, "type-a")
    assert state.call_cleanup_needed()


def test_global_phase_defaults_to_unstable_and_marks_verified_fixpoint():
    state = FunctionWorkflowState(FakeFunction())
    data_type = NativeType("uint64")

    assert not state.global_stable()
    assert state.global_receipts == {}
    assert state.global_receipts_verified(lambda *_args: False)

    assert state.mark_global_slot(0xA43D70, data_type) is True
    assert state.global_receipts == {0xA43D70: data_type}
    assert not state.global_stable()
    assert state.mark_global_slot(0xA43D70, data_type) is False
    assert state.global_receipts_verified(
        lambda addr, actual_type: (addr, actual_type) == (0xA43D70, data_type)
    )

    state.mark_global_stable()
    assert state.global_stable()


def test_global_phase_invalidates_on_new_phase_work():
    state = FunctionWorkflowState(FakeFunction())
    state.mark_global_slot(0xA43D70, NativeType("uint64"))
    state.mark_global_stable()

    state.mark_call_target(0x4000, 0x5000)
    assert not state.global_stable()

    state.mark_global_stable()
    state.mark_branch_applied(0x1000, (0x2000,))
    assert not state.global_stable()


def test_global_slot_changes_invalidate_phase_cleanup_receipts():
    state = FunctionWorkflowState(FakeFunction())
    data_type = NativeType("uint64")

    state.mark_branch_cleanup_done()
    state.mark_call_cleanup_done()
    assert not state.branch_cleanup_needed()
    assert not state.call_cleanup_needed()

    assert state.mark_global_slot(0xA43D70, data_type) is True
    assert state.branch_cleanup_needed()
    assert state.call_cleanup_needed()

    state.mark_branch_cleanup_done()
    state.mark_call_cleanup_done()
    assert state.mark_global_slot(0xA43D70, data_type) is False
    assert not state.branch_cleanup_needed()
    assert not state.call_cleanup_needed()


def test_legacy_string_global_receipts_are_discarded():
    func = FakeFunction()
    func.session_data[ROOT_KEY] = {
        "global": {
            "stable": True,
            "receipts": {0xA43D70: "uint64_t"},
        },
    }

    state = FunctionWorkflowState(func)

    assert state.global_receipts == {}
    assert not state.global_stable()


def test_existing_user_branch_metadata_is_not_promoted_to_a_receipt():
    func = FakeFunction()
    func.indirect_branches = [
        FakeBranch(0x1000, 0x2000),
        FakeBranch(0x1000, 0x3000),
        FakeBranch(0x4000, 0x5000, auto_defined=True),
    ]

    state = FunctionWorkflowState(func)

    assert state.branch_targets() == {}
    assert state.branch_updates_for({0x1000: (0x2000, 0x3000)}) == {0x1000: (0x2000, 0x3000)}
    assert state.branch_updates_for({0x4000: (0x5000,)}) == {0x4000: (0x5000,)}


def test_branch_cleanup_overlay_exception_is_scoped_to_one_translation_attempt():
    state = FunctionWorkflowState(FakeFunction())

    state.mark_branch_cleanup_overlay_ready()
    assert state.branch_cleanup_needed()
    assert state.branch_cleanup_overlay_ready()

    state.clear_branch_cleanup_overlay_ready()
    assert not state.branch_cleanup_overlay_ready()

    state.mark_branch_cleanup_overlay_ready()
    state.invalidate_branch_cleanup()
    assert state.branch_cleanup_needed()
    assert not state.branch_cleanup_overlay_ready()

    state.mark_branch_cleanup_overlay_ready()
    state.mark_branch_cleanup_done()
    assert not state.branch_cleanup_needed()
    assert not state.branch_cleanup_overlay_ready()


def test_legacy_branch_cleanup_indices_are_discarded():
    func = FakeFunction()
    func.session_data[ROOT_KEY] = {
        "branch": {
            "stable": True,
            "receipts": {0x1000: (0x2000,)},
            "cleanup_roots": {0x1000: {11, 12}},
            "cleanup_done": True,
            "cleanup_version": CLEANUP_RECEIPT_VERSION - 1,
        },
    }

    state = FunctionWorkflowState(func)

    assert "cleanup_roots" not in state.data["branch"]
    assert state.branch_cleanup_needed()


def test_stale_branch_receipts_reapply_when_bn_metadata_is_missing():
    func = FakeFunction()
    state = FunctionWorkflowState(func)
    state.mark_branch_applied(0x1000, (0x2000, 0x3000))
    state.mark_branch_stable()

    plan = {0x1000: (0x2000, 0x3000)}

    assert not state.branch_stable(func)
    assert state.branch_updates_for(plan) == plan

    func.indirect_branches = [
        FakeBranch(0x1000, 0x2000),
        FakeBranch(0x1000, 0x3000),
    ]

    assert state.branch_updates_for(plan) == {}
    assert state.branch_stable(func)

    func.set_call_type_adjustment(0x4000, "type-a")
    state.mark_call_adjusted(0x4000, 0x5000)
    state.mark_call_stable()
    state.mark_branch_applied(0x1000, plan[0x1000])

    assert not state.branch_stable(func)
    assert not state.call_stable()
    assert state.call_receipts == {}


def test_verified_branch_targets_require_exact_current_user_metadata():
    func = FakeFunction()
    state = FunctionWorkflowState(func)
    state.mark_branch_applied(0x1000, (0x2000, 0x3000))
    state.mark_branch_applied(0x4000, (0x5000,))
    state.mark_branch_applied(0x6000, (0x7000, 0x8000))
    state.mark_branch_applied(0x9000, (0xA000,))
    state.mark_branch_applied(0xB000, (0xC000,))
    func.indirect_branches = [
        FakeBranch(0x1000, 0x3000),
        FakeBranch(0x1000, 0x2000),
        FakeBranch(0x6000, 0x7000),
        FakeBranch(0x9000, 0xA000, auto_defined=True),
        FakeBranch(0xB000, 0xC000),
        FakeBranch(0xB000, 0xD000),
    ]

    assert state.verified_branch_targets() == {
        0x1000: (0x2000, 0x3000),
    }


def test_branch_stable_rejects_a_current_user_source_without_a_receipt():
    func = FakeFunction()
    state = FunctionWorkflowState(func)
    state.mark_branch_applied(0x1000, (0x2000,))
    func.indirect_branches = [FakeBranch(0x1000, 0x2000)]
    state.mark_branch_stable()

    assert state.branch_stable(func)

    func.indirect_branches.append(FakeBranch(0x3000, 0x4000))

    assert not state.branch_stable(func)


def test_cleanup_receipts_invalidate_with_phase_targets():
    state = FunctionWorkflowState(FakeFunction())

    state.mark_branch_cleanup_done()
    state.mark_call_target(0x4000, 0x5000)
    state.mark_call_adjusted(0x4000, 0x5000)
    state.mark_call_stable()
    state.mark_call_cleanup_done()
    assert not state.branch_cleanup_needed()
    assert not state.call_cleanup_needed()

    state.mark_branch_applied(0x1000, (0x2000,))
    assert state.branch_cleanup_needed()
    assert state.call_target_receipts == {}
    assert state.call_adjustment_needed(0x4000, "type-a")
    assert state.call_cleanup_needed()

    state.mark_call_adjusted(0x4000, 0x6000)
    state.mark_call_cleanup_done()
    assert not state.call_cleanup_needed()
    state.mark_call_adjusted(0x4000, 0x7000)
    assert state.call_cleanup_needed()


def test_stale_call_receipt_never_hides_a_changed_bn_adjustment():
    func = FakeFunction()
    func.set_call_type_adjustment(0x4000, "old-type")
    state = FunctionWorkflowState(func)
    state.mark_call_adjusted(0x4000, 0x5000)
    state.mark_call_stable()
    state.mark_call_cleanup_done()
    state.mark_global_stable()

    assert state.call_receipts == {0x4000: 0x5000}
    assert state.call_adjustment_needed(0x4000, "new-type")
    assert not state.call_stable()
    assert state.call_cleanup_needed()
    assert not state.global_stable()


def test_old_cleanup_receipts_are_invalidated_once():
    func = FakeFunction()
    func.session_data[ROOT_KEY] = {
        "branch": {
            "stable": True,
            "receipts": {},
            "cleanup_done": True,
            "cleanup_version": CLEANUP_RECEIPT_VERSION - 1,
        },
        "call": {
            "stable": True,
            "receipts": {},
            "targets": {},
            "cleanup_done": True,
            "cleanup_version": CLEANUP_RECEIPT_VERSION - 1,
        },
    }

    state = FunctionWorkflowState(func)

    assert state.branch_cleanup_needed()
    assert state.call_cleanup_needed()
    assert state.global_receipts == {}
    assert not state.global_stable()


def test_legacy_profile_provenance_is_removed_from_function_state():
    func = FakeFunction()
    state = FunctionWorkflowState(func)
    state.mark_branch_applied(0x1000, (0x2000,))
    func.session_data[ROOT_KEY]["profile_id"] = "dyzznb"

    refreshed = FunctionWorkflowState(func)

    assert "profile_id" not in refreshed.data
    assert refreshed.branch_targets() == {0x1000: (0x2000,)}


def _condition_receipt_data(owner_source=0x900, operand=1, true_target=0x2000, false_target=0x3000):
    return {
        "anchor": {
            "owner_source": owner_source,
            "source_operand": operand,
            "operand_path": (("dest", -1),),
            "operation": "LLIL_CMP_NE",
            "width": 1,
        },
        "true_target": true_target,
        "false_target": false_target,
    }


def test_condition_receipts_only_gate_current_matching_branch_evidence():
    state = FunctionWorkflowState(FakeFunction())
    state.mark_branch_applied(0x1000, (0x2000, 0x3000))
    receipt = _condition_receipt_data()

    assert state.set_condition_receipt(0x1000, receipt)
    assert state.condition_receipts == {0x1000: receipt}
    assert state.conditions_complete()

    assert state.record_condition_failure(0x1000, "mlil_mapping_missing")
    assert not state.conditions_complete()
    assert not state.record_condition_failure(0x1000, "mlil_mapping_missing")
    assert state.record_condition_failure(0x1000, "target_mismatch")
    assert state.clear_condition_failure(0x1000)
    assert state.conditions_complete()

    assert state.set_condition_receipt(0x1000, None)
    assert state.condition_receipts == {}
    assert state.conditions_complete()


def test_condition_receipt_and_overlay_sources_invalidate_with_branch_change():
    state = FunctionWorkflowState(FakeFunction())
    state.mark_branch_applied(0x1000, (0x2000, 0x3000))
    state.set_condition_receipt(0x1000, _condition_receipt_data())

    state.mark_branch_cleanup_overlay_ready((0x1000,))
    assert state.branch_cleanup_overlay_sources() == (0x1000,)

    assert state.mark_branch_applied(0x1000, (0x2000, 0x4000))
    assert state.condition_receipts == {}
    assert state.condition_failures == {}
    assert state.branch_cleanup_overlay_sources() == ()
    assert state.branch_cleanup_needed()


if __name__ == "__main__":
    test_branch_receipts_gate_repeated_mutations_and_invalidate_calls()
    test_call_receipts_gate_repeated_adjustments()
    test_call_target_receipts_feed_cleanup_without_gating_type_adjustments()
    test_global_phase_defaults_to_unstable_and_marks_verified_fixpoint()
    test_global_phase_invalidates_on_new_phase_work()
    test_global_slot_changes_invalidate_phase_cleanup_receipts()
    test_existing_user_branch_metadata_is_not_promoted_to_a_receipt()
    test_legacy_branch_cleanup_indices_are_discarded()
    test_stale_branch_receipts_reapply_when_bn_metadata_is_missing()
    test_branch_stable_rejects_a_current_user_source_without_a_receipt()
    test_cleanup_receipts_invalidate_with_phase_targets()
    test_old_cleanup_receipts_are_invalidated_once()
