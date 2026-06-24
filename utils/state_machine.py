"""

State-machine analyzer for the indirect-jump control-flow flattener.

Recovers, for one flattened function:

  * the dispatcher **state variable** -- the 32-bit value the compare tree
    routes on (the variable compared against the most ``==`` constants);
  * the **backbone map** ``{state_value -> dispatcher comparator block}`` -- one
    entry per leaf of the compare tree, i.e. the block that dispatches to a real
    block when ``state == state_value``;
  * the **CFG links** ``OBB -> next real block(s)``

The analyzer is read-only and does not assemble or patch anything. Consumers
(the deflatten pass) turn these links into MLIL rewrites via replace_expr()

"""

from collections import Counter

from binaryninja import MediumLevelILOperation

from .log import log_warn, log_error, log_info, log_debug

U32 = 0xFFFFFFFF


class CFGLink:
    """
    
    Resolved transition(s) out of an original block.

    Unconditional: the block sets the state to a single constant, so it has one
    ``true_block`` successor.

    Conditional: the flattener selected the next state
    from a set of constants via cmov(s); ``cases`` holds the resolved
    ``(state_value, real_block)`` pairs.
    """

    def __init__(self, block, true_block=None, cases=None, def_il=None):
        self.block = block
        self.true_block = true_block
        self.cases = cases
        self.il = def_il

    @property
    def is_uncond(self):
        return self.cases is None

    @property
    def is_cond(self):
        return not self.is_uncond

    @property
    def is_reconstructable(self):
        # A conditional we can rebuild: exactly two distinct next-states
        return self.is_cond and len(dict(self.cases)) == 2

    def __repr__(self):
        if self.is_uncond:
            tgt_idx = hex(self.true_block.start) if self.true_block else "None"
            return f"<U {self.block.start} => {tgt_idx}>"
        return "<C {} => {}>".format(
            self.block.start,
            ", ".join(f"{s:#x}:{hex(b.start)}" for s, b in self.cases),
        )


def get_most_compared_eq_var(mlil):
    """Return the variable with the most ``MLIL_CMP_E`` comparisons (the dispatcher's state variable)."""
    c = Counter(
        side.src
        for block in mlil
        for instr in block
        for expr in instr.traverse(lambda x: x)
        if expr.operation == MediumLevelILOperation.MLIL_CMP_E
        for side in (expr.left, expr.right)
        if side.operation
        in (MediumLevelILOperation.MLIL_VAR, MediumLevelILOperation.MLIL_VAR_FIELD)
    )
    return max(c, key=c.get, default=None)


def _resolve_if_condition(if_il):
    """Resolve an ``MLIL_IF`` condition to its underlying comparison.

    BN often spills the compare into a temp (``temp = state == STATE; if (temp)``),
    so the je/jne sense lives in the temp's definition, not on the ``MLIL_IF`` itself.
    Follows the SSA definition one step to recover the real comparison.
    """
    cond = if_il.condition
    if cond.operation != MediumLevelILOperation.MLIL_VAR:
        return cond
    ssa_var = cond.ssa_form.src
    def_site = cond.function.ssa_form.get_ssa_var_definition(ssa_var)
    if def_site is not None:
        return def_site.src
    return cond


def match_successor(bv, comparator_bb):
    """Return the real successor of a backbone comparator block (the match edge of its MLIL_IF).

    CMP_E -> match is the true branch; CMP_NE -> match is the false branch.
    """
    mlil = comparator_bb.il_function
    if_il = comparator_bb[-1]
    if if_il.operation != MediumLevelILOperation.MLIL_IF:
        log_warn(f"[match_successor] {comparator_bb.start} not an MLIL_IF, returning self")
        return comparator_bb

    # The comparison sense may be on the MLIL_IF itself or, when BN spilled the
    # compare into a temp (`temp = state != STATE ; if (temp)`), in the temp's
    # definition -- resolve it before reading the je/jne sense.
    cond = _resolve_if_condition(if_il)
    if cond.operation not in (
        MediumLevelILOperation.MLIL_CMP_E,
        MediumLevelILOperation.MLIL_CMP_NE,
    ):
        log_warn(
            f"[match_successor] {comparator_bb.start} ({comparator_bb[0].address:#x}) "
            f"condition did not resolve to CMP_E/CMP_NE (got {cond.operation.name}); "
            f"defaulting to true branch"
        )

    # CMP_NE matches on the false branch; everything else (CMP_E) on the true.
    is_ne = cond.operation == MediumLevelILOperation.MLIL_CMP_NE
    match_idx = if_il.false if is_ne else if_il.true
    target = mlil[match_idx].il_basic_block
    log_warn(
        f"[match_successor] {comparator_bb.start} ({comparator_bb[0].address:#x}) "
        f"=> {target.start} ({target[0].address:#x})"
    )
    return target


def compute_backbone_map(bv, func, mlil, state_var):
    """Build ``{state_value -> dispatcher comparator MLIL block}`` from all uses of ``state_var``."""
    backbone = {}
    for il in mlil.get_var_uses(state_var):
        if il.operation == MediumLevelILOperation.MLIL_IF:
            cmp_il = il.condition
            if cmp_il.operation.name.startswith("MLIL_CMP") and cmp_il.right.operation.name == "MLIL_CONST":
                state = cmp_il.right.constant & U32
                backbone[state] = il.il_basic_block
        elif il.operation == MediumLevelILOperation.MLIL_SET_VAR:
            cmp_il = il.src
            if cmp_il.operation.name.startswith("MLIL_CMP") and cmp_il.right.operation.name == "MLIL_CONST":
                state = cmp_il.right.constant & U32
                backbone[state] = il.il_basic_block
    return backbone


def resolve_to_constants(func, instr, visited=None, scope=None):
    """
    Resolve a state-variable definition back to the set of constant values it
    can hold, following ``MLIL_VAR`` definitions.

    ``scope`` -- optional set of MLIL basic-block *start* indices. When given,
    only ``MLIL_VAR`` definitions whose defining instruction lives in a block in
    ``scope`` are followed. This makes resolution **path-sensitive**: when the
    obfuscator shares the ``cmove``/state-store tail across several OBBs, the
    stored value variable has reaching definitions from *every* OBB that funnels
    through the shared block, so a global resolve returns far more than two
    constants and ``resolve_cfg_link`` discards the transition. Scoping to one
    OBB's forward chain (the blocks reachable from its head up to the
    dispatcher) restricts the result to exactly that OBB's own constants --
    sibling OBB bodies are not forward-reachable and so drop out -- while the
    shared ``cmove`` block (in scope) still contributes the alternate value.
    """
    if visited is None:
        visited = set()
    if instr in visited:
        return []
    visited.add(instr)

    if not hasattr(instr, "src"):
        return []
    src = instr.src
    op = src.operation.name

    if op == "MLIL_CONST" and src.size == 4:
        return [src.constant & U32]
    if op == "MLIL_VAR":
        results = []
        for defn in func.mlil.get_var_definitions(src.src):
            if scope is not None and defn.il_basic_block.start not in scope:
                continue
            results.extend(resolve_to_constants(func, defn, visited, scope))
        return results
    return []


def _get_stores_through_aliases(func, alias_vars):
    stores = []
    for var in alias_vars:
        for instr in func.mlil.get_var_uses(var):
            if instr.operation.name == "MLIL_STORE":
                stores.append(instr)
    return stores


def _get_all_var_and_alias_writes(func, var, visited=None):
    if visited is None:
        visited = set()
    if var in visited:
        return []
    visited.add(var)

    results = []
    for instr in func.mlil.get_var_definitions(var):
        try:
            results.append(instr)
            op = instr.src.operation.name
            if op in ("MLIL_VAR", "MLIL_ADDRESS_OF"):
                results.extend(_get_all_var_and_alias_writes(func, instr.src.src, visited))
            elif op == "MLIL_LOAD":
                results.extend(
                    _get_all_var_and_alias_writes(func, instr.src.src.src, visited)
                )
        except AttributeError:
            continue

    # Variables that point TO this var (pointer aliases).
    for instr in func.mlil.get_var_uses(var):
        if instr.operation.name != "MLIL_SET_VAR":
            continue
        op = instr.src.operation.name
        if op in ("MLIL_VAR", "MLIL_ADDRESS_OF") and instr.src.src == var:
            results.extend(_get_all_var_and_alias_writes(func, instr.dest, visited))
    return results


def get_state_write_insns(bv, func, state_var):
    """Every instruction that writes the state variable (directly or through an alias)."""
    root_vars = set()
    for d in func.mlil.get_var_definitions(state_var):
        try:
            op = d.src.operation.name
            if op == "MLIL_VAR":
                root_vars.add(d.src.src)
            elif op == "MLIL_LOAD":
                root_vars.add(d.src.src.src)
            else:
                root_vars.add(state_var)
        except AttributeError:
            continue
    if not root_vars:
        root_vars.add(state_var)

    writes = []
    for root in root_vars:
        writes.extend(_get_all_var_and_alias_writes(func, root))

    alias_vars = set()
    for w in writes:
        alias_vars.update(w.vars_written)

    insns = list(writes)
    insns.extend(_get_stores_through_aliases(func, alias_vars))
    return insns


def _is_shared_state_store(func, il):
    """True if ``il`` looks like a state store shared across OBBs.

    The signature is: the written value is a single variable (e.g. the shared
    ``cmove`` output ``[ptr] = rax_4``) whose global, path-insensitive resolve
    yields more than two constants. A direct ``[ptr] = const`` store, or a copy
    that resolves to <= 2 constants, is a normal per-OBB write and is handled by
    ``resolve_cfg_link``; only the over-collecting var-valued stores land here.
    """
    src = getattr(il, "src", None)
    if src is None or src.operation.name != "MLIL_VAR":
        return False
    return len(set(resolve_to_constants(func, il))) > 2


def resolve_cfg_link(bv, func, il, backbone):
    """
    Resolve the real successor(s) of the block containing state-write ``il``.

    One state -> unconditional link
    Two states (cmov selection) -> conditional link
    """

    bb = il.il_basic_block
    states = set(resolve_to_constants(func, il))
    if not states:
        log_debug(f"[sm] could not resolve constants for state write instruction at: {hex(il.address)}")
        return None
    if len(states) > 2:
        log_warn(f"[sm] resolved more than 2 constants for state write instruction at: {hex(il.address)}")
        return None

    if len(states) == 1:
        state_val = states.pop()
        log_info(f"[sm] Resolved states for unconditional state write: {hex(il.address)} => {[hex(state_val)]}")
        if state_val not in backbone:
            log_debug(f"[sm] {hex(il.address)} => {[hex(state_val)]} does not have a backbone entry")
            return None

        tgt_bb = match_successor(bv, backbone[state_val])
        return CFGLink(bb, true_block=tgt_bb, def_il=il)

    missing = [s for s in states if s not in backbone]
    if missing:
        log_debug(
            "[sm] {} -> states with no backbone entry: {}".format(
                hex(il.address), ", ".join(hex(s) for s in missing)
            )
        )
        return None

    cases = [
        (s, match_successor(bv, backbone[s])) for s in sorted(states)
    ]

    log_info(f"[sm] Resolved states for conditional state write: {hex(il.address)} => {[hex(x) for x in states]}, cases: {cases}")
    if not cases:
        raise Exception(f"[sm] failed to build cases list for conditional state write: {hex(il.address)}")

    return CFGLink(bb, cases=cases, def_il=il)


class StateMachine:
    def __init__(self, bv, func):
        self.bv = bv
        self.func = func
        self.mlil = func.medium_level_il
        self.state_var = None
        self.backbone = {}
        self.links = []
        # State-write instructions whose value resolves to MORE than two
        # constants under a global (path-insensitive) resolve. That is the
        # signature of a state store shared across several OBBs (the obfuscator's
        # shared ``cmove``/state-store tail): the stored value variable has
        # reaching definitions from every OBB that funnels through it. These
        # cannot be resolved here -- they have no single successor -- so they are
        # handed to the deflattener, which re-resolves each one per-OBB with a
        # path-scoped resolve (see deflatten.recover_shared_store_links).
        self.shared_stores = []
        # The state variable plus every alias it is copied through.
        # The cleanup phase NOPs every write to these.
        self.state_write_vars = set()

    def analyze(self):
        # The variable with the most MLIL_CMP_E comparisons is the state variable
        self.state_var = get_most_compared_eq_var(self.mlil)
        if self.state_var is None:
            log_warn("[sm] no state variable found")
            return self
        log_warn(f"[sm] state variable: {self.state_var}")

        # Map in the form: {STATE: MLIL_BASIC_BLOCK}
        self.backbone = compute_backbone_map(self.bv, self.func, self.mlil, self.state_var)
        log_warn(f"[sm] backbone: {len(self.backbone)} states")

        # Collect all MLIL instructions that write to the state var (and aliases)
        state_write_insns = get_state_write_insns(self.bv, self.func, self.state_var)
    
        # Record the state variable and every alias written along the chain so the
        # cleanup can NOP `var_44 = rax_438` and the `rax_438 = ...` feeding it,
        # not just the direct `var_44 = const` writes.
        self.state_write_vars = {self.state_var}
        for il in state_write_insns:
            self.state_write_vars.update(il.vars_written)
        log_warn(f"[sm] state write vars/aliases: {self.state_write_vars}")
        self.links = []
        self.shared_stores = []
        for il in state_write_insns:
            link = resolve_cfg_link(self.bv, self.func, il, self.backbone)
            if link is not None:
                self.links.append(link)
            elif _is_shared_state_store(self.func, il):
                # Dropped for >2 constants and shaped like a shared cmove/store
                # tail -- defer to the deflattener's per-OBB recovery.
                self.shared_stores.append(il)
        log_warn(
            f"[sm] resolved {len(self.links)} CFG links; "
            f"{len(self.shared_stores)} shared/ambiguous state store(s) deferred"
        )
        return self
