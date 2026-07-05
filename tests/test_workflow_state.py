import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.DispatchThis.workflow_state import FunctionWorkflowState, ROOT_KEY


class FakeBranch:
    def __init__(self, source_addr, dest_addr=0x2000, auto_defined=False):
        self.source_addr = source_addr
        self.dest_addr = dest_addr
        self.auto_defined = auto_defined


class FakeFunction:
    def __init__(self):
        self.session_data = {}
        self.unresolved_indirect_branches = []
        self.indirect_branches = []


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

    state.mark_call_adjusted(0x4000, 0x5000)
    assert not state.call_adjustment_needed(0x4000, 0x5000)

    changed_plan = {0x1000: (0x2000, 0x4000)}
    assert state.branch_updates_for(changed_plan) == changed_plan
    assert state.mark_branch_applied(0x1000, changed_plan[0x1000]) is True
    assert state.call_adjustment_needed(0x4000, 0x5000)
    assert not state.branch_stable(func)


def test_call_receipts_gate_repeated_adjustments():
    state = FunctionWorkflowState(FakeFunction())

    assert not state.call_stable()
    assert state.call_adjustment_needed(0x1111, 0x2222)
    assert state.mark_call_adjusted(0x1111, 0x2222) is False
    assert not state.call_adjustment_needed(0x1111, 0x2222)
    assert state.mark_call_adjusted(0x1111, 0x3333) is True
    assert not state.call_adjustment_needed(0x1111, 0x3333)
    state.mark_call_stable()
    assert state.call_stable()


def test_call_target_receipts_feed_cleanup_without_gating_type_adjustments():
    state = FunctionWorkflowState(FakeFunction())

    state.mark_call_cleanup_done()
    assert state.mark_call_target(0x1111, 0x2222) is False
    assert state.call_target_receipts == {0x1111: 0x2222}
    assert state.call_adjustment_needed(0x1111, 0x2222)
    assert state.call_cleanup_needed()

    state.mark_call_adjusted(0x1111, 0x2222)
    state.mark_call_cleanup_done()
    assert state.mark_call_target(0x1111, 0x3333) is True
    assert state.call_adjustment_needed(0x1111, 0x3333)
    assert state.call_cleanup_needed()


def test_global_phase_defaults_to_unstable_and_marks_verified_fixpoint():
    state = FunctionWorkflowState(FakeFunction())

    assert not state.global_stable()
    assert state.global_receipts == {}
    assert state.global_receipts_verified(lambda *_args: False)

    assert state.mark_global_slot(0xA43D70, "uint64_t") is True
    assert state.global_receipts == {0xA43D70: "uint64_t"}
    assert not state.global_stable()
    assert state.mark_global_slot(0xA43D70, "uint64_t") is False
    assert state.global_receipts_verified(lambda addr, type_name: (addr, type_name) == (0xA43D70, "uint64_t"))

    state.mark_global_stable()
    assert state.global_stable()


def test_global_phase_invalidates_on_new_phase_work():
    state = FunctionWorkflowState(FakeFunction())
    state.mark_global_slot(0xA43D70, "uint64_t")
    state.mark_global_stable()

    state.mark_call_target(0x4000, 0x5000)
    assert not state.global_stable()

    state.mark_global_stable()
    state.mark_branch_applied(0x1000, (0x2000,))
    assert not state.global_stable()


def test_existing_user_branch_metadata_seeds_branch_receipts():
    func = FakeFunction()
    func.indirect_branches = [
        FakeBranch(0x1000, 0x2000),
        FakeBranch(0x1000, 0x3000),
        FakeBranch(0x4000, 0x5000, auto_defined=True),
    ]

    state = FunctionWorkflowState(func)

    assert state.branch_targets() == {0x1000: (0x2000, 0x3000)}
    assert state.branch_updates_for({0x1000: (0x2000, 0x3000)}) == {}
    assert state.branch_updates_for({0x4000: (0x5000,)}) == {0x4000: (0x5000,)}


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
    assert state.call_adjustment_needed(0x4000, 0x5000)
    assert state.call_cleanup_needed()

    state.mark_call_adjusted(0x4000, 0x6000)
    state.mark_call_cleanup_done()
    assert not state.call_cleanup_needed()
    state.mark_call_adjusted(0x4000, 0x7000)
    assert state.call_cleanup_needed()


def test_old_cleanup_receipts_are_invalidated_once():
    func = FakeFunction()
    func.session_data[ROOT_KEY] = {
        "branch": {
            "stable": True,
            "receipts": {},
            "cleanup_done": True,
        },
        "call": {
            "stable": True,
            "receipts": {},
            "targets": {},
            "cleanup_done": True,
        },
    }

    state = FunctionWorkflowState(func)

    assert state.branch_cleanup_needed()
    assert state.call_cleanup_needed()
    assert state.global_receipts == {}
    assert not state.global_stable()


if __name__ == "__main__":
    test_branch_receipts_gate_repeated_mutations_and_invalidate_calls()
    test_call_receipts_gate_repeated_adjustments()
    test_call_target_receipts_feed_cleanup_without_gating_type_adjustments()
    test_global_phase_defaults_to_unstable_and_marks_verified_fixpoint()
    test_global_phase_invalidates_on_new_phase_work()
    test_existing_user_branch_metadata_seeds_branch_receipts()
    test_stale_branch_receipts_reapply_when_bn_metadata_is_missing()
    test_cleanup_receipts_invalidate_with_phase_targets()
    test_old_cleanup_receipts_are_invalidated_once()
