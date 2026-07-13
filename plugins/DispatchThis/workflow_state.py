"""Function-scoped workflow phase state for DispatchThis."""


ROOT_KEY = "dispatchthis_workflow_state"
CLEANUP_RECEIPT_VERSION = 6


class ProfileStateMismatch(RuntimeError):
    pass


def _fresh_state():
    return {
        "profile_id": None,
        "branch": {
            "stable": False,
            "receipts": {},
            "cleanup_done": False,
            "cleanup_overlay_ready": False,
            "cleanup_version": CLEANUP_RECEIPT_VERSION,
        },
        "call": {
            "stable": False,
            "receipts": {},
            "targets": {},
            "cleanup_done": False,
            "cleanup_version": CLEANUP_RECEIPT_VERSION,
        },
        "global": {
            "stable": False,
            "receipts": {},
        },
    }


def _has_recovery_evidence(data):
    return any((
        data.get("branch", {}).get("stable", False),
        data.get("branch", {}).get("receipts"),
        data.get("call", {}).get("stable", False),
        data.get("call", {}).get("receipts"),
        data.get("call", {}).get("targets"),
        data.get("global", {}).get("stable", False),
        data.get("global", {}).get("receipts"),
    ))


def function_has_recovery_evidence(func):
    return _has_recovery_evidence(func.session_data.get(ROOT_KEY, {}))


def _targets_tuple(targets):
    if targets is None:
        return ()
    if isinstance(targets, int):
        return (targets,)
    return tuple(sorted(set(targets)))


def _user_branch_targets(func):
    by_source = {}
    for branch in getattr(func, "indirect_branches", ()):
        if getattr(branch, "auto_defined", False):
            continue
        source = getattr(branch, "source_addr", None)
        target = getattr(branch, "dest_addr", None)
        if source is None or target is None:
            continue
        by_source.setdefault(source, set()).add(target)
    return {source: _targets_tuple(targets) for source, targets in by_source.items()}


def _normalize_state(data):
    branch = data.setdefault("branch", {})
    branch.setdefault("stable", False)
    branch.setdefault("receipts", {})
    # An instruction index only identifies one MLIL generation. Never reuse a
    # persisted cleanup root after reanalysis can assign that index to other IL.
    branch.pop("cleanup_roots", None)
    branch.setdefault("cleanup_done", False)
    branch.setdefault("cleanup_overlay_ready", False)
    if branch.get("cleanup_version") != CLEANUP_RECEIPT_VERSION:
        branch["cleanup_done"] = False
        branch["cleanup_overlay_ready"] = False
        branch["cleanup_version"] = CLEANUP_RECEIPT_VERSION
    call = data.setdefault("call", {})
    call.setdefault("stable", False)
    call.setdefault("receipts", {})
    call.setdefault("targets", {})
    call.setdefault("cleanup_done", False)
    if call.get("cleanup_version") != CLEANUP_RECEIPT_VERSION:
        call["cleanup_done"] = False
        call["cleanup_version"] = CLEANUP_RECEIPT_VERSION
    global_ = data.setdefault("global", {})
    global_.setdefault("stable", False)
    receipts = global_.setdefault("receipts", {})
    global_["receipts"] = {slot: str(type_name) for slot, type_name in receipts.items()}
    return data


class FunctionWorkflowState:
    """Phase semantics over one function's session_data."""

    def __init__(self, func, profile_id=None):
        self.func = func
        raw_state = func.session_data.setdefault(ROOT_KEY, _fresh_state())
        had_evidence = _has_recovery_evidence(raw_state)
        self.data = _normalize_state(raw_state)
        recorded_profile = self.data.get("profile_id")
        if profile_id is not None:
            if recorded_profile is None:
                if had_evidence:
                    raise ProfileStateMismatch(
                        "legacy recovery state has no resolver profile provenance"
                    )
                self.data["profile_id"] = profile_id
            elif recorded_profile != profile_id:
                if had_evidence:
                    raise ProfileStateMismatch(
                        f"function state belongs to profile {recorded_profile!r}, "
                        f"not {profile_id!r}"
                    )
                self.data["profile_id"] = profile_id
        self.seed_branch_receipts()

    @staticmethod
    def unmapped_unresolved_sources(func):
        unresolved = {source for _, source in func.unresolved_indirect_branches}
        mapped = {branch.source_addr for branch in func.indirect_branches}
        return unresolved - mapped

    @property
    def branch_receipts(self):
        return self.data["branch"]["receipts"]

    def branch_targets(self):
        return {source: _targets_tuple(targets) for source, targets in self.branch_receipts.items()}

    def verified_branch_targets(self):
        """Return receipts that exactly match current BN user branch metadata."""
        applied_targets = _user_branch_targets(self.func)
        return {
            source: targets
            for source, targets in self.branch_targets().items()
            if applied_targets.get(source) == targets
        }

    def seed_branch_receipts(self, func=None):
        """Import existing user indirect-branch metadata as branch receipts.

        This keeps hot-reload or reopened BNDB sessions from resubmitting the
        same set_user_indirect_branches mutations just because session_data was
        empty.
        """
        func = func or self.func
        by_source = _user_branch_targets(func)

        seeded = 0
        for source, targets in by_source.items():
            if source in self.branch_receipts:
                continue
            self.branch_receipts[source] = _targets_tuple(targets)
            seeded += 1
        return seeded

    def branch_updates_for(self, resolved_targets):
        mutations = {}
        applied_targets = _user_branch_targets(self.func)
        for source, targets in resolved_targets.items():
            targets = _targets_tuple(targets)
            if not targets:
                continue
            if self.branch_receipts.get(source) != targets or applied_targets.get(source) != targets:
                mutations[source] = targets
        return mutations

    def mark_branch_applied(self, source, targets):
        targets = _targets_tuple(targets)
        previous = self.branch_receipts.get(source)
        self.branch_receipts[source] = targets
        self.data["branch"]["stable"] = False
        self.data["branch"]["cleanup_done"] = False
        self.data["branch"]["cleanup_overlay_ready"] = False
        self.invalidate_calls()
        return previous is not None and previous != targets

    def mark_branch_stable(self):
        self.data["branch"]["stable"] = True

    def branch_stable(self, func=None):
        func = func or self.func
        if not self.data["branch"]["stable"] or self.unmapped_unresolved_sources(func):
            return False
        applied_targets = _user_branch_targets(func)
        return all(applied_targets.get(source) == targets for source, targets in self.branch_targets().items())

    def branch_cleanup_needed(self):
        return not self.data["branch"]["cleanup_done"]

    def mark_branch_cleanup_done(self):
        self.data["branch"]["cleanup_done"] = True
        self.data["branch"]["cleanup_overlay_ready"] = False
        self.data["branch"]["cleanup_version"] = CLEANUP_RECEIPT_VERSION

    def invalidate_branch_cleanup(self):
        self.data["branch"]["cleanup_done"] = False
        self.data["branch"]["cleanup_overlay_ready"] = False

    def mark_branch_cleanup_overlay_ready(self):
        """Permit one downstream current-MLIL cleanup fixed-point check.

        This is set only after the branch translator NOPs and settles decode
        assignments in its current installed overlay. It is deliberately not a
        receipt: the next translator attempt or any phase invalidation clears
        it before a new MLIL generation can be trusted.
        """
        self.data["branch"]["cleanup_done"] = False
        self.data["branch"]["cleanup_overlay_ready"] = True

    def clear_branch_cleanup_overlay_ready(self):
        self.data["branch"]["cleanup_overlay_ready"] = False

    def branch_cleanup_overlay_ready(self):
        return bool(self.data["branch"].get("cleanup_overlay_ready", False))

    @property
    def call_receipts(self):
        return self.data["call"]["receipts"]

    @property
    def call_target_receipts(self):
        return self.data["call"]["targets"]

    def call_adjustment_needed(self, call_addr, adjust_type):
        """Compare the desired override with Binary Ninja's current fact."""
        try:
            needed = self.func.get_call_type_adjustment(call_addr) != adjust_type
        except Exception:  # noqa: BLE001
            needed = True
        if needed:
            self.invalidate_call_cleanup()
            self.invalidate_call_stable()
        return needed

    def invalidate_call_stable(self):
        self.data["call"]["stable"] = False
        self.invalidate_globals()

    def mark_call_target(self, call_addr, target):
        previous = self.call_target_receipts.get(call_addr)
        stale_adjustment = (
            call_addr in self.call_receipts
            and self.call_receipts[call_addr] != target
        )
        if stale_adjustment:
            self.call_receipts.pop(call_addr)
        if previous == target and not stale_adjustment:
            return False
        self.call_target_receipts[call_addr] = target
        self.data["call"]["stable"] = False
        self.data["call"]["cleanup_done"] = False
        self.invalidate_globals()
        return previous is not None

    def mark_call_adjusted(self, call_addr, target):
        previous = self.call_receipts.get(call_addr)
        if previous == target:
            return False
        self.call_receipts[call_addr] = target
        self.data["call"]["stable"] = False
        self.data["call"]["cleanup_done"] = False
        self.invalidate_globals()
        return previous is not None

    def mark_call_stable(self):
        self.data["call"]["stable"] = True

    def call_stable(self):
        return self.data["call"]["stable"]

    def call_cleanup_needed(self):
        return not self.data["call"]["cleanup_done"]

    def mark_call_cleanup_done(self):
        self.data["call"]["cleanup_done"] = True
        self.data["call"]["cleanup_version"] = CLEANUP_RECEIPT_VERSION

    def invalidate_call_cleanup(self):
        self.data["call"]["cleanup_done"] = False

    @property
    def global_receipts(self):
        return self.data["global"]["receipts"]

    def mark_global_slot(self, slot_addr, type_name):
        type_name = str(type_name)
        previous = self.global_receipts.get(slot_addr)
        if previous == type_name:
            return False
        self.global_receipts[slot_addr] = type_name
        self.data["global"]["stable"] = False
        self.invalidate_cleanup()
        return True

    def mark_global_stable(self):
        self.data["global"]["stable"] = True

    def global_stable(self):
        return self.data["global"]["stable"]

    def global_receipts_verified(self, verifier):
        return all(verifier(slot_addr, type_name) for slot_addr, type_name in self.global_receipts.items())

    def invalidate_globals(self):
        self.data["global"]["stable"] = False

    def invalidate_cleanup(self):
        self.invalidate_branch_cleanup()
        self.invalidate_call_cleanup()

    def invalidate_calls(self):
        self.data["call"]["stable"] = False
        self.data["call"]["cleanup_done"] = False
        self.call_receipts.clear()
        self.call_target_receipts.clear()
        self.invalidate_globals()
