"""Receipt-driven restoration of provider-proven branch conditions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from binaryninja import ILSourceLocation, MediumLevelILOperation as M

from ...helpers.mlil import cleanup_roots_for_expr
from .rewrite import copied_label_for_source, copy_mlil_with_instruction_rewrites


CONDITION_FAILURE_TAG = "DispatchThis Condition Failure"


class ConditionTranslationStatus(str, Enum):
    """The only current-MLIL outcomes for one condition receipt."""

    REWRITE_READY = "rewrite_ready"
    ALREADY_SATISFIED = "already_satisfied"
    FAILED = "failed"


class ConditionFailureReason(str, Enum):
    """Stable failure categories; details deliberately remain ephemeral."""

    ANCHOR_MISSING = "anchor_missing"
    ANCHOR_AMBIGUOUS = "anchor_ambiguous"
    MLIL_MAPPING_MISSING = "mlil_mapping_missing"
    MLIL_MAPPING_AMBIGUOUS = "mlil_mapping_ambiguous"
    SITE_MISSING = "site_missing"
    SITE_AMBIGUOUS = "site_ambiguous"
    TARGET_MISMATCH = "target_mismatch"
    CONDITION_MISMATCH = "condition_mismatch"
    COPY_FAILED = "copy_failed"
    INSTALL_FAILED = "install_failed"


@dataclass(frozen=True, slots=True)
class ILAnchor:
    """A narrow, reanalysis-safe position for one current LLIL expression."""

    owner_source: int
    source_operand: int
    operand_path: tuple[tuple[str, int], ...]
    operation: str
    width: int

    def as_data(self):
        return {
            "owner_source": self.owner_source,
            "source_operand": self.source_operand,
            "operand_path": self.operand_path,
            "operation": self.operation,
            "width": self.width,
        }

    @classmethod
    def from_data(cls, data):
        if not _valid_anchor_data(data):
            return None
        return cls(
            data["owner_source"],
            data["source_operand"],
            tuple(data["operand_path"]),
            data["operation"],
            data["width"],
        )


@dataclass(frozen=True, slots=True)
class ConditionReceipt:
    """Provider-owned condition identity coupled to one resolved branch site."""

    source: int
    anchor: ILAnchor
    true_target: int
    false_target: int

    def as_data(self):
        return {
            "anchor": self.anchor.as_data(),
            "true_target": self.true_target,
            "false_target": self.false_target,
        }

    @classmethod
    def from_data(cls, source, data):
        if not _valid_uint(source) or type(data) is not dict:
            return None
        anchor = ILAnchor.from_data(data.get("anchor"))
        true_target = data.get("true_target")
        false_target = data.get("false_target")
        if (
            anchor is None
            or not _valid_uint(true_target)
            or not _valid_uint(false_target)
            or true_target == false_target
        ):
            return None
        return cls(source, anchor, true_target, false_target)


@dataclass(frozen=True, slots=True)
class ConditionTranslationFailure:
    source: int
    reason: ConditionFailureReason
    detail: str


@dataclass(frozen=True, slots=True)
class ConditionTranslationResult:
    source: int
    status: ConditionTranslationStatus
    failure: ConditionTranslationFailure | None = None


@dataclass(frozen=True, slots=True)
class ConditionTranslationBatch:
    """One all-or-nothing MLIL copy attempt plus per-receipt outcomes."""

    new_mlil: object
    results: tuple[ConditionTranslationResult, ...]
    rewrite_sources: frozenset[int]
    cleanup_roots: frozenset[int]
    backend_failed: bool = False

    def with_rewrite_failure(self, reason, detail):
        failed = tuple(
            _failed(result.source, reason, detail)
            if result.status is ConditionTranslationStatus.REWRITE_READY
            else result
            for result in self.results
        )
        return ConditionTranslationBatch(
            self.new_mlil,
            failed,
            frozenset(),
            frozenset(),
            True,
        )


@dataclass(frozen=True, slots=True)
class _RewritePlan:
    source: int
    site: object
    condition: object
    true_index: int
    false_index: int
    cleanup_roots: frozenset[int]


def _valid_uint(value):
    return type(value) is int and value >= 0


def _operation_name(instruction):
    name = getattr(getattr(instruction, "operation", None), "name", None)
    return name if type(name) is str else None


def _location(instruction):
    source = getattr(instruction, "address", None)
    operand = getattr(instruction, "source_operand", None)
    return (source, operand) if _valid_uint(source) and _valid_uint(operand) else None


def _detailed_operands(instruction):
    try:
        operands = getattr(instruction, "detailed_operands", ())
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — IL wrapper boundary.
        return ()
    result = []
    for operand in operands or ():
        if type(operand) is not tuple or len(operand) != 3:
            continue
        name, value, _kind = operand
        if type(name) is str:
            result.append((name, value))
    return tuple(result)


def _is_instruction(value):
    return getattr(value, "operation", None) is not None


def _walk_operand_paths(instruction, path=(), ancestors=()):
    """Yield all expression children without serializing their identities."""

    yield path, instruction
    ancestor_ids = {*ancestors, id(instruction)}
    for name, value in _detailed_operands(instruction):
        if _is_instruction(value):
            if id(value) not in ancestor_ids:
                yield from _walk_operand_paths(value, path + ((name, -1),), ancestor_ids)
            continue
        if type(value) not in (list, tuple):
            continue
        for index, item in enumerate(value):
            if _is_instruction(item) and id(item) not in ancestor_ids:
                yield from _walk_operand_paths(item, path + ((name, index),), ancestor_ids)


def _same_owner(left, right):
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja wrapper equality boundary.
        return False


def _same_current_expression(left, right):
    if left is right:
        return True
    left_index = getattr(left, "expr_index", None)
    right_index = getattr(right, "expr_index", None)
    left_function = getattr(left, "function", None)
    right_function = getattr(right, "function", None)
    return (
        _valid_uint(left_index)
        and left_index == right_index
        and left_function is not None
        and right_function is not None
        and _same_owner(left_function, right_function)
    )


def _current_instructions(il):
    try:
        return tuple(getattr(il, "instructions", ()) or ())
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — current IL enumeration boundary.
        return ()


def _unique_current_expressions(expressions):
    unique = []
    for expression in expressions:
        if not any(_same_current_expression(expression, prior) for prior in unique):
            unique.append(expression)
    return tuple(unique)


def _contains_current_expression(root, target):
    for path, candidate in _walk_operand_paths(root):
        if path and _same_current_expression(candidate, target):
            return True
    return False


def _direct_mlil_mapping(candidates):
    """Select one direct mapping while rejecting independent alternatives."""

    current = _unique_current_expressions(candidates)
    if len(current) == 1:
        return current[0]
    direct = [
        candidate
        for candidate in current
        if not any(
            _contains_current_expression(candidate, other)
            for other in current
            if not _same_current_expression(candidate, other)
        )
    ]
    return direct[0] if len(direct) == 1 else None


def _follow_operand_path(instruction, path):
    current = instruction
    for name, item_index in path:
        matches = [value for candidate, value in _detailed_operands(current) if candidate == name]
        if len(matches) != 1:
            return None
        value = matches[0]
        if item_index == -1:
            if not _is_instruction(value):
                return None
            current = value
            continue
        if type(value) not in (list, tuple) or item_index < 0 or item_index >= len(value):
            return None
        current = value[item_index]
        if not _is_instruction(current):
            return None
    return current


def _valid_anchor_data(data):
    if type(data) is not dict:
        return False
    path = data.get("operand_path")
    if type(path) is not tuple:
        return False
    if not all(
        type(step) is tuple
        and len(step) == 2
        and type(step[0]) is str
        and type(step[1]) is int
        and step[1] >= -1
        for step in path
    ):
        return False
    return (
        _valid_uint(data.get("owner_source"))
        and _valid_uint(data.get("source_operand"))
        and type(data.get("operation")) is str
        and bool(data["operation"])
        and type(data.get("width")) is int
        and data["width"] >= 0
    )


def capture_condition_receipt(llil, source, condition, true_target, false_target):
    """Capture one scalar receipt before UIDF can regenerate the IL."""

    if (
        not _valid_uint(source)
        or not _valid_uint(true_target)
        or not _valid_uint(false_target)
        or true_target == false_target
    ):
        return None
    location = _location(condition)
    operation = _operation_name(condition)
    width = getattr(condition, "size", None)
    if location is None or operation is None or type(width) is not int or width < 0:
        return None

    candidates = []
    for root in _current_instructions(llil):
        if _location(root) != location:
            continue
        for path, candidate in _walk_operand_paths(root):
            if _same_current_expression(candidate, condition):
                candidates.append(path)
    if len(candidates) != 1:
        return None

    return ConditionReceipt(
        source,
        ILAnchor(location[0], location[1], candidates[0], operation, width),
        true_target,
        false_target,
    )


def _direct_comparison_operands(condition):
    operands = _detailed_operands(condition)
    if len(operands) != 2:
        return None
    by_name = {name: value for name, value in operands}
    if set(by_name) != {"left", "right"}:
        return None
    return by_name["left"], by_name["right"]


def _same_direct_llil_operand(left, right):
    operation = _operation_name(left)
    if operation != _operation_name(right):
        return False
    left_width = getattr(left, "size", None)
    right_width = getattr(right, "size", None)
    if (
        type(left_width) is not int
        or left_width < 0
        or left_width != right_width
    ):
        return False
    if operation in {"LLIL_REG", "LLIL_REG_SSA"}:
        left_source = getattr(left, "src", None)
        right_source = getattr(right, "src", None)
        return left_source is not None and _same_owner(left_source, right_source)
    if operation in {"LLIL_CONST", "LLIL_CONST_PTR"}:
        left_value = getattr(left, "constant", None)
        right_value = getattr(right, "constant", None)
        return type(left_value) is int and left_value == right_value
    return False


def _same_direct_llil_comparison(left, right):
    operation = _operation_name(left)
    left_width = getattr(left, "size", None)
    right_width = getattr(right, "size", None)
    if (
        operation is None
        or not operation.startswith("LLIL_CMP_")
        or operation != _operation_name(right)
        or type(left_width) is not int
        or left_width < 0
        or left_width != right_width
    ):
        return False
    left_operands = _direct_comparison_operands(left)
    right_operands = _direct_comparison_operands(right)
    return (
        left_operands is not None
        and right_operands is not None
        and _same_direct_llil_operand(left_operands[0], right_operands[0])
        and _same_direct_llil_operand(left_operands[1], right_operands[1])
    )


def _rebind_anchor(llil, anchor):
    candidates = []
    for root in _current_instructions(llil):
        if _location(root) != (anchor.owner_source, anchor.source_operand):
            continue
        candidate = _follow_operand_path(root, anchor.operand_path)
        if (
            candidate is not None
            and _operation_name(candidate) == anchor.operation
            and getattr(candidate, "size", None) == anchor.width
        ):
            candidates.append(candidate)
    candidates = _unique_current_expressions(candidates)
    if not candidates:
        return None, ConditionFailureReason.ANCHOR_MISSING, "condition anchor is absent from current LLIL"
    if len(candidates) != 1 and not all(
        _same_direct_llil_comparison(candidates[0], candidate)
        for candidate in candidates[1:]
    ):
        return None, ConditionFailureReason.ANCHOR_AMBIGUOUS, "condition anchor matches multiple current LLIL expressions"
    return candidates[0], None, ""


def _current_mlil_mapping(llil_condition, mlil):
    try:
        candidates = tuple(getattr(llil_condition, "mlils", ()) or ())
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — LLIL-to-MLIL mapping boundary.
        candidates = ()
    current = [
        candidate
        for candidate in candidates
        if getattr(candidate, "function", None) is not None
        and _same_owner(candidate.function, mlil)
    ]
    if not current:
        return None, ConditionFailureReason.MLIL_MAPPING_MISSING, "condition has no current MLIL mapping"
    condition = _direct_mlil_mapping(current)
    if condition is None:
        return None, ConditionFailureReason.MLIL_MAPPING_AMBIGUOUS, "condition maps to multiple current MLIL expressions"
    return condition, None, ""


def _site_for_source(mlil, source):
    sites = [
        instruction
        for instruction in _current_instructions(mlil)
        if getattr(instruction, "address", None) == source
        and getattr(instruction, "operation", None) in (M.MLIL_JUMP_TO, M.MLIL_IF)
    ]
    if not sites:
        return None, ConditionFailureReason.SITE_MISSING, "source has no current switch-like branch site"
    if len(sites) != 1:
        return None, ConditionFailureReason.SITE_AMBIGUOUS, "source has multiple current branch sites"
    return sites[0], None, ""


def _target_block_for_index(mlil, index):
    if not _valid_uint(index):
        return None
    try:
        instruction = mlil[index]
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — MLIL instruction lookup boundary.
        return None
    if getattr(instruction, "instr_index", None) != index:
        return None
    try:
        blocks = tuple(getattr(mlil, "basic_blocks", ()) or ())
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — MLIL basic-block boundary.
        return None
    starts = [
        block
        for block in blocks
        if type(getattr(block, "start", None)) is int and block.start == index
    ]
    return instruction if len(starts) == 1 else None


def _directed_target_indexes(mlil, receipt, targets):
    if type(targets) is not dict or set(targets) != {receipt.true_target, receipt.false_target}:
        return None
    true_index = targets[receipt.true_target]
    false_index = targets[receipt.false_target]
    if true_index == false_index:
        return None
    if (
        _target_block_for_index(mlil, true_index) is None
        or _target_block_for_index(mlil, false_index) is None
    ):
        return None
    return true_index, false_index


def _failed(source, reason, detail):
    return ConditionTranslationResult(
        source,
        ConditionTranslationStatus.FAILED,
        ConditionTranslationFailure(source, reason, detail),
    )


def _classify_receipt(llil, mlil, receipt):
    llil_condition, reason, detail = _rebind_anchor(llil, receipt.anchor)
    if reason is not None:
        return _failed(receipt.source, reason, detail), None
    condition, reason, detail = _current_mlil_mapping(llil_condition, mlil)
    if reason is not None:
        return _failed(receipt.source, reason, detail), None
    site, reason, detail = _site_for_source(mlil, receipt.source)
    if reason is not None:
        return _failed(receipt.source, reason, detail), None

    if site.operation is M.MLIL_JUMP_TO:
        targets = _directed_target_indexes(mlil, receipt, getattr(site, "targets", None))
        if targets is None:
            return _failed(
                receipt.source,
                ConditionFailureReason.TARGET_MISMATCH,
                "current JUMP_TO targets do not match the directed receipt",
            ), None
        cleanup_roots = frozenset(cleanup_roots_for_expr(mlil, getattr(site, "dest", None)))
        return (
            ConditionTranslationResult(receipt.source, ConditionTranslationStatus.REWRITE_READY),
            _RewritePlan(receipt.source, site, condition, targets[0], targets[1], cleanup_roots),
        )

    targets = _directed_target_indexes(
        mlil,
        receipt,
        {
            receipt.true_target: getattr(site, "true", None),
            receipt.false_target: getattr(site, "false", None),
        },
    )
    if targets is None:
        return _failed(
            receipt.source,
            ConditionFailureReason.TARGET_MISMATCH,
            "current IF targets do not match the directed receipt",
        ), None
    if not _same_current_expression(getattr(site, "condition", None), condition):
        return _failed(
            receipt.source,
            ConditionFailureReason.CONDITION_MISMATCH,
            "current IF does not use the rebound condition root",
        ), None
    return ConditionTranslationResult(receipt.source, ConditionTranslationStatus.ALREADY_SATISFIED), None


def _replacement_for(plan):
    def replace(new_mlil, rewrite_il):
        copy_to = getattr(plan.condition, "copy_to", None)
        condition = copy_to(new_mlil) if callable(copy_to) else new_mlil.copy_expr(plan.condition)
        return new_mlil.if_expr(
            condition,
            copied_label_for_source(new_mlil, plan.true_index),
            copied_label_for_source(new_mlil, plan.false_index),
            ILSourceLocation.from_instruction(rewrite_il),
        )

    return replace


def translate_indirect_branch_conditions(ctx, llil, mlil, receipts):
    """Translate only persisted provider receipts, never rediscovered candidates."""

    results = []
    plans = []
    for receipt in sorted(receipts, key=lambda item: item.source):
        result, plan = _classify_receipt(llil, mlil, receipt)
        results.append(result)
        if plan is not None:
            plans.append(plan)

    if not plans:
        return ConditionTranslationBatch(mlil, tuple(results), frozenset(), frozenset())

    replacements = {}
    for plan in plans:
        index = getattr(plan.site, "instr_index", None)
        if not _valid_uint(index) or index in replacements:
            return ConditionTranslationBatch(mlil, tuple(results), frozenset(), frozenset(), True).with_rewrite_failure(
                ConditionFailureReason.COPY_FAILED,
                "current branch site cannot be copied exactly once",
            )
        replacements[index] = _replacement_for(plan)

    try:
        new_mlil, applied = copy_mlil_with_instruction_rewrites(ctx, replacements, mlil=mlil)
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — atomic MLIL copy boundary.
        new_mlil, applied = None, 0
    if new_mlil is None or applied != len(replacements):
        return ConditionTranslationBatch(mlil, tuple(results), frozenset(), frozenset(), True).with_rewrite_failure(
            ConditionFailureReason.COPY_FAILED,
            "atomic branch-condition copy-transform did not install every selected site",
        )

    return ConditionTranslationBatch(
        new_mlil,
        tuple(results),
        frozenset(plan.source for plan in plans),
        frozenset(root for plan in plans for root in plan.cleanup_roots),
    )


def clear_condition_failure_tags(func, sources):
    """Remove core-owned diagnostics when their receipt succeeds or expires."""

    remove = getattr(func, "remove_auto_address_tags_of_type", None)
    if not callable(remove):
        return
    for source in sources:
        if not _valid_uint(source):
            continue
        try:
            remove(source, CONDITION_FAILURE_TAG)
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja tag boundary.
            continue


def publish_condition_failure_tag(bv, func, failure):
    """Publish one automatic, source-local failure diagnostic when its reason changes."""

    clear_condition_failure_tags(func, (failure.source,))
    try:
        get_tag_type = getattr(bv, "get_tag_type", None)
        tag_type = get_tag_type(CONDITION_FAILURE_TAG) if callable(get_tag_type) else None
        if tag_type is None:
            create_tag_type = getattr(bv, "create_tag_type", None)
            if callable(create_tag_type):
                create_tag_type(CONDITION_FAILURE_TAG, "!")
        add_tag = getattr(func, "add_tag", None)
        if callable(add_tag):
            add_tag(CONDITION_FAILURE_TAG, failure.detail, addr=failure.source, auto=True)
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja tag boundary.
        return


__all__ = (
    "ConditionFailureReason",
    "ConditionReceipt",
    "ConditionTranslationBatch",
    "ConditionTranslationFailure",
    "ConditionTranslationResult",
    "ConditionTranslationStatus",
    "CONDITION_FAILURE_TAG",
    "ILAnchor",
    "capture_condition_receipt",
    "clear_condition_failure_tags",
    "publish_condition_failure_tag",
    "translate_indirect_branch_conditions",
)
