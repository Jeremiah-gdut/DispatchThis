"""Function-scoped workflow phase state for DispatchThis."""


ROOT_KEY = "dispatchthis_workflow_state"
CLEANUP_RECEIPT_VERSION = 7


def _fresh_state():
    return {
        "branch": {
            "stable": False,
            "receipts": {},
            "conditions": {},
            "condition_failures": {},
            "cleanup_done": False,
            "cleanup_overlay_ready": False,
            "cleanup_overlay_sources": (),
            "cleanup_version": CLEANUP_RECEIPT_VERSION,
        },
        "call": {
            "stable": False,
            "receipts": {},
            "targets": {},
            "adjustments": {},
            "cleanup_done": False,
            "cleanup_version": CLEANUP_RECEIPT_VERSION,
        },
        "global": {
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


def _valid_uint(value):
    return type(value) is int and value >= 0


def _valid_condition_receipt(data):
    if type(data) is not dict:
        return False
    anchor = data.get("anchor")
    if type(anchor) is not dict:
        return False
    path = anchor.get("operand_path")
    if type(path) is not tuple or not all(
        type(step) is tuple
        and len(step) == 2
        and type(step[0]) is str
        and type(step[1]) is int
        and step[1] >= -1
        for step in path
    ):
        return False
    true_target = data.get("true_target")
    false_target = data.get("false_target")
    return (
        _valid_uint(anchor.get("owner_source"))
        and _valid_uint(anchor.get("source_operand"))
        and type(anchor.get("operation")) is str
        and bool(anchor["operation"])
        and type(anchor.get("width")) is int
        and anchor["width"] >= 0
        and _valid_uint(true_target)
        and _valid_uint(false_target)
        and true_target != false_target
    )


def _overlay_sources(sources):
    if type(sources) not in (tuple, list, set, frozenset):
        return ()
    if not all(_valid_uint(source) for source in sources):
        return ()
    return tuple(sorted(set(sources)))


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
    # Provider binding is BinaryView-scoped.  Older versions persisted profile
    # provenance here; discard it rather than carrying an identity into a
    # function-level receipt store.
    data.pop("profile_id", None)
    branch = data.setdefault("branch", {})
    branch.setdefault("stable", False)
    branch.setdefault("receipts", {})
    branch.setdefault("conditions", {})
    branch.setdefault("condition_failures", {})
    # An instruction index only identifies one MLIL generation. Never reuse a
    # persisted cleanup root after reanalysis can assign that index to other IL.
    branch.pop("cleanup_roots", None)
    branch.setdefault("cleanup_done", False)
    branch.setdefault("cleanup_overlay_ready", False)
    branch.setdefault("cleanup_overlay_sources", ())
    if branch.get("cleanup_version") != CLEANUP_RECEIPT_VERSION:
        branch["stable"] = False
        branch["conditions"] = {}
        branch["condition_failures"] = {}
        branch["cleanup_done"] = False
        branch["cleanup_overlay_ready"] = False
        branch["cleanup_overlay_sources"] = ()
        branch["cleanup_version"] = CLEANUP_RECEIPT_VERSION
    conditions = branch["conditions"]
    if (
        type(conditions) is not dict
        or any(
            not _valid_uint(source) or not _valid_condition_receipt(receipt)
            for source, receipt in conditions.items()
        )
    ):
        branch["conditions"] = {}
        branch["condition_failures"] = {}
        branch["stable"] = False
    failures = branch["condition_failures"]
    if (
        type(failures) is not dict
        or any(
            source not in branch["conditions"] or type(reason) is not str or not reason
            for source, reason in failures.items()
        )
    ):
        branch["condition_failures"] = {}
    branch["cleanup_overlay_sources"] = _overlay_sources(branch["cleanup_overlay_sources"])
    call = data.setdefault("call", {})
    call.setdefault("stable", False)
    call.setdefault("receipts", {})
    call.setdefault("targets", {})
    adjustments = call.setdefault("adjustments", {})
    if (
        type(adjustments) is not dict
        or any(
            type(call_addr) is not int
            or call_addr < 0
            or adjustment is None
            or type(adjustment) is str
            for call_addr, adjustment in adjustments.items()
        )
    ):
        call["adjustments"] = {}
    call.setdefault("cleanup_done", False)
    if call.get("cleanup_version") != CLEANUP_RECEIPT_VERSION:
        call["cleanup_done"] = False
        call["cleanup_version"] = CLEANUP_RECEIPT_VERSION
    global_ = data.setdefault("global", {})
    global_.setdefault("stable", False)
    receipts = global_.setdefault("receipts", {})
    if (
        type(receipts) is not dict
        or any(
            type(slot) is not int
            or slot < 0
            or data_type is None
            or type(data_type) is str
            for slot, data_type in receipts.items()
        )
    ):
        global_["receipts"] = {}
        global_["stable"] = False
    return data


class FunctionWorkflowState:
    """Phase semantics over one function's session_data."""

    def __init__(self, func, seed_legacy_branch_receipts=False):
        self.func = func
        raw_state = func.session_data.setdefault(ROOT_KEY, _fresh_state())
        self.data = _normalize_state(raw_state)
        if seed_legacy_branch_receipts:
            self._seed_legacy_branch_receipts()

    @staticmethod
    def unmapped_unresolved_sources(func):
        unresolved = {source for _, source in func.unresolved_indirect_branches}
        mapped = set(_user_branch_targets(func))
        return unresolved - mapped

    @property
    def branch_receipts(self):
        return self.data["branch"]["receipts"]

    @property
    def condition_receipts(self):
        return self.data["branch"]["conditions"]

    @property
    def condition_failures(self):
        return self.data["branch"]["condition_failures"]

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

    def current_user_branch_targets(self):
        """Read current non-auto user metadata without promoting it to a receipt."""
        return _user_branch_targets(self.func)

    def branch_metadata_matches(self, source, targets):
        """Require an exact current non-auto user-branch target tuple."""
        return _user_branch_targets(self.func).get(source) == _targets_tuple(targets)

    def _seed_legacy_branch_receipts(self):
        """Preserve bundled-profile receipts during the private migration path."""
        for source, targets in _user_branch_targets(self.func).items():
            self.branch_receipts.setdefault(source, targets)

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
        if previous is not None and previous != targets:
            self.condition_receipts.pop(source, None)
            self.condition_failures.pop(source, None)
        self.data["branch"]["stable"] = False
        self.invalidate_branch_cleanup()
        self.invalidate_calls()
        return previous is not None and previous != targets

    def mark_branch_stable(self):
        self.data["branch"]["stable"] = True

    def branch_stable(self, func=None):
        func = func or self.func
        if not self.data["branch"]["stable"] or self.unmapped_unresolved_sources(func):
            return False
        applied_targets = _user_branch_targets(func)
        receipts = self.branch_targets()
        return (
            set(applied_targets) <= set(receipts)
            and all(applied_targets.get(source) == targets for source, targets in receipts.items())
        )

    def branch_cleanup_needed(self):
        return not self.data["branch"]["cleanup_done"]

    def mark_branch_cleanup_done(self):
        self.data["branch"]["cleanup_done"] = True
        self.data["branch"]["cleanup_overlay_ready"] = False
        self.data["branch"]["cleanup_overlay_sources"] = ()
        self.data["branch"]["cleanup_version"] = CLEANUP_RECEIPT_VERSION

    def invalidate_branch_cleanup(self):
        self.data["branch"]["cleanup_done"] = False
        self.data["branch"]["cleanup_overlay_ready"] = False
        self.data["branch"]["cleanup_overlay_sources"] = ()

    def mark_branch_cleanup_overlay_ready(self, sources=()):
        """Permit one downstream current-MLIL cleanup fixed-point check.

        This is set only after the branch translator NOPs and settles decode
        assignments in its current installed overlay. It is deliberately not a
        receipt: the next translator attempt or any phase invalidation clears
        it before a new MLIL generation can be trusted.
        """
        self.data["branch"]["cleanup_done"] = False
        self.data["branch"]["cleanup_overlay_ready"] = True
        self.data["branch"]["cleanup_overlay_sources"] = _overlay_sources(sources)

    def clear_branch_cleanup_overlay_ready(self):
        self.data["branch"]["cleanup_overlay_ready"] = False
        self.data["branch"]["cleanup_overlay_sources"] = ()

    def branch_cleanup_overlay_ready(self):
        return bool(self.data["branch"].get("cleanup_overlay_ready", False))

    def branch_cleanup_overlay_sources(self):
        return self.data["branch"]["cleanup_overlay_sources"]

    def set_condition_receipt(self, source, receipt):
        """Replace one condition receipt only when the provider fact truly changed."""
        if not _valid_uint(source):
            return False
        if receipt is None:
            changed = source in self.condition_receipts or source in self.condition_failures
            self.condition_receipts.pop(source, None)
            self.condition_failures.pop(source, None)
        elif not _valid_condition_receipt(receipt):
            return False
        elif self.condition_receipts.get(source) == receipt:
            return False
        else:
            self.condition_receipts[source] = receipt
            self.condition_failures.pop(source, None)
            changed = True
        if changed:
            self.invalidate_branch_cleanup()
        return changed

    def record_condition_failure(self, source, reason):
        """Store only the stable reason used to deduplicate user diagnostics."""
        if (
            source not in self.condition_receipts
            or type(reason) is not str
            or not reason
            or self.condition_failures.get(source) == reason
        ):
            return False
        self.condition_failures[source] = reason
        self.invalidate_branch_cleanup()
        return True

    def clear_condition_failure(self, source):
        if source not in self.condition_failures:
            return False
        self.condition_failures.pop(source, None)
        return True

    def conditions_complete(self):
        """All active condition receipts must still match their branch evidence."""
        for source, receipt in self.condition_receipts.items():
            if source in self.condition_failures:
                return False
            if self.branch_receipts.get(source) != _targets_tuple(
                (receipt["true_target"], receipt["false_target"])
            ):
                return False
        return True

    @property
    def call_receipts(self):
        return self.data["call"]["receipts"]

    @property
    def call_target_receipts(self):
        return self.data["call"]["targets"]

    @property
    def call_adjustment_receipts(self):
        return self.data["call"]["adjustments"]

    def call_adjustment_needed(self, call_addr, adjust_type):
        """Compare the desired override with Binary Ninja's current fact."""
        try:
            needed = self.func.get_call_type_adjustment(call_addr) != adjust_type
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja adjustment lookup boundary.
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
            self.call_adjustment_receipts.pop(call_addr, None)
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
        self.call_adjustment_receipts.pop(call_addr, None)
        self.call_receipts[call_addr] = target
        self.data["call"]["stable"] = False
        self.data["call"]["cleanup_done"] = False
        self.invalidate_globals()
        return previous is not None

    def mark_call_adjustment(self, call_addr, adjust_type):
        self.call_adjustment_receipts[call_addr] = adjust_type

    def discard_call_site(self, call_addr):
        """Drop a singleton receipt when current evidence says it is unsupported."""
        adjustment = self.call_adjustment_receipts.pop(call_addr, None)
        had_receipt = (
            call_addr in self.call_receipts
            or call_addr in self.call_target_receipts
            or adjustment is not None
        )
        self.call_receipts.pop(call_addr, None)
        self.call_target_receipts.pop(call_addr, None)
        if had_receipt:
            self.data["call"]["stable"] = False
            self.data["call"]["cleanup_done"] = False
            self.invalidate_globals()
        return adjustment

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

    def mark_global_slot(self, slot_addr, data_type):
        previous = self.global_receipts.get(slot_addr)
        try:
            unchanged = previous == data_type
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja type comparison boundary.
            unchanged = False
        if unchanged:
            return False
        self.global_receipts[slot_addr] = data_type
        self.data["global"]["stable"] = False
        self.invalidate_cleanup()
        return True

    def mark_global_stable(self):
        self.data["global"]["stable"] = True

    def global_stable(self):
        return self.data["global"]["stable"]

    def global_receipts_verified(self, verifier):
        return all(
            verifier(slot_addr, data_type)
            for slot_addr, data_type in self.global_receipts.items()
        )

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
        self.call_adjustment_receipts.clear()
        self.invalidate_globals()
