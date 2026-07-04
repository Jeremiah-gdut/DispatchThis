import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.DispatchThis.workflow_state import FunctionWorkflowState


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
    assert state.branch_mutations_for(first_plan) == first_plan
    assert state.mark_branch_mutation_applied(0x1000, first_plan[0x1000]) is False
    assert state.branch_mutations_for(first_plan) == {}

    func.indirect_branches = [FakeBranch(0x1000)]
    state.mark_branch_resolving_stable()
    assert state.branch_resolving_is_stable(func)

    state.mark_call_adjustment_applied(0x4000, 0x5000)
    assert not state.call_adjustment_needed(0x4000, 0x5000)

    changed_plan = {0x1000: (0x2000, 0x4000)}
    assert state.branch_mutations_for(changed_plan) == changed_plan
    assert state.mark_branch_mutation_applied(0x1000, changed_plan[0x1000]) is True
    assert state.call_adjustment_needed(0x4000, 0x5000)
    assert not state.branch_resolving_is_stable(func)


def test_call_receipts_gate_repeated_adjustments():
    state = FunctionWorkflowState(FakeFunction())

    assert not state.indirect_call_resolving_is_stable()
    assert state.call_adjustment_needed(0x1111, 0x2222)
    assert state.mark_call_adjustment_applied(0x1111, 0x2222) is False
    assert not state.call_adjustment_needed(0x1111, 0x2222)
    assert state.mark_call_adjustment_applied(0x1111, 0x3333) is True
    assert not state.call_adjustment_needed(0x1111, 0x3333)
    state.mark_indirect_call_resolving_stable()
    assert state.indirect_call_resolving_is_stable()


def test_call_target_receipts_feed_cleanup_without_gating_type_adjustments():
    state = FunctionWorkflowState(FakeFunction())

    state.mark_call_cleanup_done()
    assert state.mark_call_target_resolved(0x1111, 0x2222) is False
    assert state.call_target_receipts == {0x1111: 0x2222}
    assert state.call_adjustment_needed(0x1111, 0x2222)
    assert state.call_cleanup_needed()

    state.mark_call_adjustment_applied(0x1111, 0x2222)
    state.mark_call_cleanup_done()
    assert state.mark_call_target_resolved(0x1111, 0x3333) is True
    assert state.call_adjustment_needed(0x1111, 0x3333)
    assert state.call_cleanup_needed()


def test_existing_user_branch_metadata_seeds_branch_receipts():
    func = FakeFunction()
    func.indirect_branches = [
        FakeBranch(0x1000, 0x2000),
        FakeBranch(0x1000, 0x3000),
        FakeBranch(0x4000, 0x5000, auto_defined=True),
    ]

    state = FunctionWorkflowState(func)

    assert state.branch_target_receipts() == {0x1000: (0x2000, 0x3000)}
    assert state.branch_mutations_for({0x1000: (0x2000, 0x3000)}) == {}
    assert state.branch_mutations_for({0x4000: (0x5000,)}) == {0x4000: (0x5000,)}


def test_cleanup_receipts_invalidate_with_phase_targets():
    state = FunctionWorkflowState(FakeFunction())

    state.mark_branch_cleanup_done()
    state.mark_call_target_resolved(0x4000, 0x5000)
    state.mark_call_adjustment_applied(0x4000, 0x5000)
    state.mark_indirect_call_resolving_stable()
    state.mark_call_cleanup_done()
    assert not state.branch_cleanup_needed()
    assert not state.call_cleanup_needed()

    state.mark_branch_mutation_applied(0x1000, (0x2000,))
    assert state.branch_cleanup_needed()
    assert state.call_target_receipts == {}
    assert state.call_adjustment_needed(0x4000, 0x5000)
    assert state.call_cleanup_needed()

    state.mark_call_adjustment_applied(0x4000, 0x6000)
    state.mark_call_cleanup_done()
    assert not state.call_cleanup_needed()
    state.mark_call_adjustment_applied(0x4000, 0x7000)
    assert state.call_cleanup_needed()


if __name__ == "__main__":
    test_branch_receipts_gate_repeated_mutations_and_invalidate_calls()
    test_call_receipts_gate_repeated_adjustments()
    test_call_target_receipts_feed_cleanup_without_gating_type_adjustments()
    test_existing_user_branch_metadata_seeds_branch_receipts()
    test_cleanup_receipts_invalidate_with_phase_targets()
