"""MLIL-stage indirect-call backend: rewrites decode-gadget call(reg) into call(const).

Folds the decode add (``target = (encoded + KEY) mod 2^48``) and rewrites only the
call destination to a ``MLIL_CONST_PTR``. The exact SSA target slice separately owns
dead decode cleanup; profile-provided definition witnesses are never mutation targets.
"""

from binaryninja import MediumLevelILOperation as M

from ...helpers.facts import call_fact
from ...helpers.memory import is_known_callee
from ...helpers.mlil import (
    CALL_OPERATIONS,
    CONST_OPERATIONS,
    LOAD_OPERATIONS,
    current_non_ssa_instruction,
    fold_constant_value,
    expression_scalar_value,
    iter_indirect_calls,
    peel_var_definitions,
    walk_expr,
)
from ...utils.log import log_info, log_warn, log_debug


U48 = 0xFFFFFFFFFFFF

# --------------------------------------------------------------------------- #
# gadget parse + decode
# --------------------------------------------------------------------------- #


def validate_current_call_plans(mlil, plans):
    """Bind one complete, unambiguous batch to current non-SSA MLIL."""
    accepted = []
    for plan in plans or ():
        if not isinstance(plan, dict):
            return None
        recorded_call = plan.get("call_il")
        current_call = _current_indirect_call(mlil, recorded_call)
        call_addr = plan.get("call_addr")
        target = plan.get("target")
        recorded_owner = getattr(recorded_call, "function", None)
        current_owner = getattr(current_call, "function", None)
        if (
            current_call is None
            or recorded_owner is not mlil
            or current_owner is not mlil
            or type(call_addr) is not int
            or call_addr < 0
            or type(target) is not int
            or target < 0
            or getattr(recorded_call, "address", None) != call_addr
            or getattr(current_call, "address", None) != call_addr
        ):
            log_warn(f"[icall] rejected stale call plan @ {call_addr!r}")
            return None

        decode_def = plan.get("decode_def")
        current_decode = None
        if decode_def is not None:
            current_decode = current_non_ssa_instruction(mlil, decode_def)
            decode_owner = getattr(decode_def, "function", None)
            current_decode_owner = getattr(current_decode, "function", None)
            if (
                current_decode is None
                or decode_owner is not mlil
                or current_decode_owner is not mlil
                or getattr(decode_def, "expr_index", None)
                != getattr(current_decode, "expr_index", None)
                or _expression_witness(getattr(decode_def, "src", None))
                != _expression_witness(getattr(current_decode, "src", None))
            ):
                log_warn(f"[icall] rejected stale decode witness @ {call_addr:#x}")
                return None

        current_plan = {
            **plan,
            "call_il": current_call,
            "decode_def": current_decode,
        }
        # Cleanup indices are an ephemeral property of the *current* MLIL.
        # Regenerate the exact SSA reaching-definition slice at the mutation
        # boundary. Failure to prove a slice disables cleanup without
        # invalidating the independently witnessed call rewrite.
        cleanup_slice = _call_target_definition_slice(mlil, current_call)
        current_plan["cleanup_proven"] = cleanup_slice is not None
        cleanup_roots, cleanup_load_roots = cleanup_slice or (set(), set())
        current_plan["cleanup_roots"] = cleanup_roots
        if cleanup_load_roots:
            current_plan["cleanup_load_roots"] = cleanup_load_roots
        else:
            current_plan.pop("cleanup_load_roots", None)
        same_site = next((item for item in accepted if item["call_addr"] == call_addr), None)
        if same_site is not None:
            if _plan_witness(same_site) != _plan_witness(current_plan):
                log_warn(f"[icall] rejected conflicting plans @ {call_addr:#x}")
                return None
            continue
        accepted.append(current_plan)

    rewrites = {}
    for plan in accepted:
        expr_index = plan["call_il"].dest.expr_index
        previous = rewrites.setdefault(expr_index, plan["target"])
        if previous != plan["target"]:
            log_warn(f"[icall] rejected conflicting rewrite for expression {expr_index}")
            return None
    return accepted


def validate_current_call_facts(mlil, facts):
    """Bind complete provider target sets to exact current indirect calls."""
    accepted = []
    for recorded_call, targets in facts:
        current_call = _current_indirect_call(mlil, recorded_call)
        call_addr = getattr(recorded_call, "address", None)
        if (
            current_call is None
            or type(call_addr) is not int
            or call_addr < 0
            or type(targets) is not tuple
            or not targets
            or any(type(target) is not int or target < 0 for target in targets)
            or tuple(sorted(set(targets))) != targets
        ):
            log_warn(f"[icall] rejected stale call fact @ {call_addr!r}")
            return None
        same_site = next(
            (item for item in accepted if item[0].address == call_addr),
            None,
        )
        if same_site is not None:
            if same_site[0] is not current_call or same_site[1] != targets:
                log_warn(f"[icall] rejected conflicting facts @ {call_addr:#x}")
                return None
            continue
        accepted.append((current_call, targets))
    return accepted


def _current_indirect_call(mlil, recorded_call):
    current_call = current_non_ssa_instruction(mlil, recorded_call)
    recorded_owner = getattr(recorded_call, "function", None)
    current_owner = getattr(current_call, "function", None)
    if (
        current_call is None
        or recorded_owner is not mlil
        or current_owner is not mlil
        or getattr(recorded_call, "operation", None) != current_call.operation
        or current_call.operation not in CALL_OPERATIONS
        or getattr(getattr(current_call, "dest", None), "operation", None) in CONST_OPERATIONS
        or getattr(recorded_call, "expr_index", None)
        != getattr(current_call, "expr_index", None)
        or getattr(recorded_call, "address", None)
        != getattr(current_call, "address", None)
        or _expression_witness(getattr(recorded_call, "dest", None))
        != _expression_witness(getattr(current_call, "dest", None))
        or _parameter_witness(recorded_call) != _parameter_witness(current_call)
    ):
        return None
    return current_call


def _expression_witness(expr):
    return (
        getattr(expr, "expr_index", None),
        getattr(expr, "operation", None),
    )


def _parameter_witness(call_il):
    params = getattr(call_il, "params", None)
    if params is None:
        return None
    try:
        return tuple(_expression_witness(param) for param in params)
    except TypeError:
        return _expression_witness(params)


def _plan_witness(plan):
    decode = plan.get("decode_def")
    return (
        plan["target"],
        _expression_witness(plan["call_il"]),
        _expression_witness(plan["call_il"].dest),
        _parameter_witness(plan["call_il"]),
        None if decode is None else _expression_witness(decode),
        None if decode is None else _expression_witness(decode.src),
        frozenset(plan.get("cleanup_roots", ())),
        frozenset(plan.get("cleanup_load_roots", ())),
    )


_UNSUPPORTED_SSA_VAR_READS = {
    M.MLIL_VAR,
    M.MLIL_VAR_FIELD,
    M.MLIL_VAR_SPLIT,
    M.MLIL_VAR_SSA_FIELD,
    M.MLIL_VAR_ALIASED,
    M.MLIL_VAR_ALIASED_FIELD,
    M.MLIL_VAR_SPLIT_SSA,
}


def _ssa_reads(expression):
    """Return exact whole SSA-variable reads, or ``None`` at an alias boundary."""
    try:
        nodes = walk_expr(expression)
    except (AttributeError, TypeError):
        return None
    if any(node.operation in _UNSUPPORTED_SSA_VAR_READS for node in nodes):
        return None
    reads = []
    for node in nodes:
        if node.operation != M.MLIL_VAR_SSA:
            continue
        variable = getattr(node, "src", None)
        if variable is None or not hasattr(variable, "version"):
            return None
        if not any(variable == seen for seen in reads):
            reads.append(variable)
    return reads


def _call_target_definition_slice(mlil, call_il):
    """Return the exact current SSA definitions feeding one call destination.

    PHIs are connectors only.  Cleanup ownership is granted exclusively to
    whole ``SET_VAR_SSA`` definitions that map back to exact current non-SSA
    ``SET_VAR`` instructions.  Field, split, and aliased variables fail closed.
    """
    try:
        ssa = getattr(mlil, "ssa_form", None)
    except Exception:  # noqa: BLE001
        return None
    if ssa is None:
        return None
    # Real BN asserts inside instruction.ssa_form when the owning function has
    # no SSA product. Cleanup proof is optional, so contain that API boundary.
    try:
        ssa_call = getattr(call_il, "ssa_form", None)
    except Exception:  # noqa: BLE001
        return None
    if ssa_call is None:
        return None
    current_call = current_non_ssa_instruction(mlil, ssa_call)
    if (
        current_call is None
        or getattr(current_call, "instr_index", None)
        != getattr(call_il, "instr_index", None)
        or getattr(current_call, "expr_index", None)
        != getattr(call_il, "expr_index", None)
    ):
        return None

    initial_reads = _ssa_reads(getattr(ssa_call, "dest", None))
    if initial_reads is None:
        return None

    definitions = {}
    pending = list(initial_reads)
    seen_variables = []
    while pending:
        variable = pending.pop()
        if any(variable == seen for seen in seen_variables):
            continue
        seen_variables.append(variable)
        try:
            definition = ssa.get_ssa_var_definition(variable)
        except Exception:  # noqa: BLE001
            return None
        if definition is None:
            # BN documents only version-zero/input variables as legitimately
            # lacking a definition. A missing later version is incomplete proof.
            if variable.version != 0:
                return None
            continue
        if getattr(definition, "dest", None) != variable:
            return None
        if definition.operation == M.MLIL_VAR_PHI:
            sources = tuple(getattr(definition, "src", ()) or ())
            if not sources or any(
                source is None or not hasattr(source, "version")
                for source in sources
            ):
                return None
            pending.extend(sources)
            continue
        if definition.operation != M.MLIL_SET_VAR_SSA:
            return None

        current_definition = current_non_ssa_instruction(mlil, definition)
        if (
            current_definition is None
            or current_definition.operation != M.MLIL_SET_VAR
            or getattr(current_definition, "function", None) is not mlil
        ):
            return None
        definitions[current_definition.instr_index] = current_definition
        source_reads = _ssa_reads(getattr(definition, "src", None))
        if source_reads is None:
            return None
        pending.extend(source_reads)

    roots = set(definitions)
    load_roots = {
        index
        for index, definition in definitions.items()
        if any(node.operation in LOAD_OPERATIONS for node in walk_expr(definition.src))
    }
    return roots, load_roots


def current_call_receipt_plans(mlil, receipts):
    """Rebind stored target receipts only when current MLIL proves them."""
    if mlil is None:
        return None if receipts else []
    if any(
        type(call_addr) is not int
        or call_addr < 0
        or type(target) is not int
        or target < 0
        for call_addr, target in receipts.items()
    ):
        return None

    calls_by_address = {}
    for call_il in getattr(mlil, "instructions", ()) or ():
        if call_il.operation in CALL_OPERATIONS:
            calls_by_address.setdefault(call_il.address, []).append(call_il)

    plans = []
    for call_addr, target in receipts.items():
        calls = calls_by_address.get(call_addr, ())
        if len(calls) != 1:
            return None
        call_il = calls[0]
        dest = getattr(call_il, "dest", None)
        if (
            getattr(call_il, "function", None) is not mlil
            or getattr(dest, "operation", None) not in CONST_OPERATIONS
            or expression_scalar_value(mlil, dest) != target
        ):
            return None
        plans.append(call_fact(call_il, target))
    return plans


def resolve_call_target(bv, mlil, call_il):
    """Resolve the concrete target of one indirect call by folding its decode add.
    Returns the target and its descriptive definition witness."""
    dest = call_il.dest

    # Already a direct call (const, or a var that folds to a const pointer).
    if dest.operation in CONST_OPERATIONS:
        return None, None
    trail = []
    resolved = peel_var_definitions(mlil, dest, trail)
    if resolved.operation in CONST_OPERATIONS:
        return None, None
    # A variant calls *through* the decoded slot (`call([rax + KEY])`). BN wraps
    # the decode add in an outer load, but the decoded pointer (the load's address
    # operand) is the real target -- not a dereference of it. Unwrap the load and
    # peel any further var indirection so the inner decode add hits the path below.
    loaded_target = resolved if resolved.operation in LOAD_OPERATIONS else None
    decode_expr = resolved
    if loaded_target is not None:
        decode_expr = peel_var_definitions(mlil, loaded_target.src)
        if decode_expr.operation in CONST_OPERATIONS:
            return None, None
    if decode_expr.operation != M.MLIL_ADD:
        log_debug(f"[icall] {hex(call_il.address)}: dest def is "
                  f"{decode_expr.operation.name}, not a decode add; skipping")
        return None, None
    # The SET_VAR whose source is the decode add (last def walked), if any.
    decode_def = trail[-1] if trail else None

    # The key is the constant operand (the right per the gadget shape); the other
    # operand folds to the encoded target.
    left, right = decode_expr.left, decode_expr.right
    if right.operation in CONST_OPERATIONS:
        key_expr, enc_expr = right, left
    elif left.operation in CONST_OPERATIONS:
        key_expr, enc_expr = left, right
    else:
        # Key may sit behind a var that propagates a constant; default to the
        # right operand as the gadget always places the key there.
        key_expr, enc_expr = right, left

    key = fold_constant_value(bv, mlil, key_expr, load_address_mask=U48)
    if key is None:
        log_debug(f"[icall] {hex(call_il.address)}: could not fold decode key")
        return None, None
    encoded = fold_constant_value(bv, mlil, enc_expr, load_address_mask=U48)
    if encoded is None:
        log_debug(f"[icall] {hex(call_il.address)}: could not fold encoded target")
        return None, None

    if loaded_target is not None:
        target = fold_constant_value(
            bv,
            mlil,
            loaded_target,
            load_address_mask=U48,
        )
    else:
        target = (encoded + key) & U48

    # This gadget explicitly decodes modulo 2^48. An outer LOAD dereferences
    # that exact slot and yields one width-preserving callee value; neither path
    # permits selecting a valid-looking masked alias.
    if target is not None and is_known_callee(bv, target):
        return target, decode_def

    decoded = None if target is None else hex(target)
    log_warn(f"[icall] {hex(call_il.address)}: exact decoded target {decoded} is not a callee")
    return None, None


# --------------------------------------------------------------------------- #
def plan_indirect_calls(bv, mlil):
    """Resolve decode-gadget indirect calls without mutating function state."""
    if mlil is None:
        log_warn("[icall] mlil is None")
        return []

    plans = []
    for call_il in iter_indirect_calls(mlil):
        try:
            target, decode_def = resolve_call_target(
                bv,
                mlil,
                call_il,
            )
        except Exception as e:  # noqa: BLE001
            log_warn(f"[icall] {hex(call_il.address)}: {e}")
            continue
        if target is None:
            continue
        sym = bv.get_symbol_at(target)
        name = sym.name if sym else hex(target)
        log_info(f"[icall] {hex(call_il.address)}: indirect call -> "
                 f"{hex(target)} ({name})")
        plans.append(call_fact(
            call_il,
            target,
            decode_def=decode_def,
        ))

    if not plans:
        log_info("[icall] no indirect call gadgets resolved")
    return plans


def apply_indirect_call_rewrites(ctx, mlil, plans):
    """Replace current call destinations without copying the whole MLIL function."""
    if mlil is None:
        return mlil, 0
    plans = validate_current_call_plans(mlil, plans)
    if plans is None:
        return mlil, 0
    if not plans:
        return mlil, 0

    addr_size = ctx.view.arch.address_size
    replacements = [
        (
            plan["call_il"].dest.expr_index,
            mlil.const_pointer(addr_size, plan["target"]),
        )
        for plan in plans
    ]
    for expr_index, replacement in replacements:
        mlil.replace_expr(expr_index, replacement)

    mlil.finalize()
    mlil.generate_ssa_form()

    for plan in plans:
        call_il = plan["call_il"]
        target = plan["target"]
        log_debug(f"[icall] {hex(call_il.address)} -> call {hex(target)}")

    applied = len(plans)
    log_info(f"[icall] {mlil.source_function.name}: applied {applied} indirect call rewrite(s)")
    return mlil, applied
