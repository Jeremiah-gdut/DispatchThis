"""Function-scoped workflow phase state for DispatchThis."""


ROOT_KEY = "dispatchthis_workflow_state"


def _fresh_state():
    return {
        "branch": {
            "stable": False,
            "receipts": {},
        },
        "call": {
            "stable": False,
            "receipts": {},
        },
    }


def _targets_tuple(targets):
    if targets is None:
        return ()
    if isinstance(targets, int):
        return (targets,)
    return tuple(sorted(set(targets)))


def _normalize_state(data):
    branch = data.setdefault("branch", {})
    branch.setdefault("stable", False)
    branch.setdefault("receipts", {})
    call = data.setdefault("call", {})
    call.setdefault("stable", False)
    call.setdefault("receipts", {})
    return data


class FunctionWorkflowState:
    """Phase semantics over one function's session_data."""

    def __init__(self, func):
        self.func = func
        self.data = _normalize_state(func.session_data.setdefault(ROOT_KEY, _fresh_state()))
        self.seed_branch_receipts_from_user_metadata()

    @staticmethod
    def unmapped_unresolved_sources(func):
        unresolved = {source for _, source in func.unresolved_indirect_branches}
        mapped = {branch.source_addr for branch in func.indirect_branches}
        return unresolved - mapped

    @property
    def branch_receipts(self):
        return self.data["branch"]["receipts"]

    def branch_target_receipts(self):
        return {source: _targets_tuple(targets) for source, targets in self.branch_receipts.items()}

    def seed_branch_receipts_from_user_metadata(self, func=None):
        """Import existing user indirect-branch metadata as branch receipts.

        This keeps hot-reload or reopened BNDB sessions from resubmitting the
        same set_user_indirect_branches mutations just because session_data was
        empty.
        """
        func = func or self.func
        by_source = {}
        for branch in getattr(func, "indirect_branches", ()):
            if getattr(branch, "auto_defined", False):
                continue
            source = getattr(branch, "source_addr", None)
            target = getattr(branch, "dest_addr", None)
            if source is None or target is None:
                continue
            by_source.setdefault(source, set()).add(target)

        seeded = 0
        for source, targets in by_source.items():
            if source in self.branch_receipts:
                continue
            self.branch_receipts[source] = _targets_tuple(targets)
            seeded += 1
        return seeded

    def branch_mutations_for(self, resolved_targets):
        mutations = {}
        for source, targets in resolved_targets.items():
            targets = _targets_tuple(targets)
            if not targets:
                continue
            if self.branch_receipts.get(source) != targets:
                mutations[source] = targets
        return mutations

    def mark_branch_mutation_applied(self, source, targets):
        targets = _targets_tuple(targets)
        previous = self.branch_receipts.get(source)
        if previous == targets:
            return False
        self.branch_receipts[source] = targets
        self.data["branch"]["stable"] = False
        self.invalidate_indirect_call_resolving()
        return previous is not None

    def mark_branch_resolving_stable(self):
        self.data["branch"]["stable"] = True

    def branch_resolving_is_stable(self, func=None):
        func = func or self.func
        return self.data["branch"]["stable"] and not self.unmapped_unresolved_sources(func)

    @property
    def call_receipts(self):
        return self.data["call"]["receipts"]

    def call_adjustment_needed(self, call_addr, target):
        return self.call_receipts.get(call_addr) != target

    def mark_call_adjustment_applied(self, call_addr, target):
        previous = self.call_receipts.get(call_addr)
        if previous == target:
            return False
        self.call_receipts[call_addr] = target
        self.data["call"]["stable"] = False
        return previous is not None

    def mark_indirect_call_resolving_stable(self):
        self.data["call"]["stable"] = True

    def invalidate_indirect_call_resolving(self):
        self.data["call"]["stable"] = False
        self.call_receipts.clear()
