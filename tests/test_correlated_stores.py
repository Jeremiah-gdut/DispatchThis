import types

from binaryninja import MediumLevelILOperation as M

from conftest import load_plugin_module


semantics = load_plugin_module("plugins.DispatchThis.semantics")
correlated_stores = load_plugin_module("plugins.DispatchThis.passes.medium.correlated_stores")


class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target


class Block:
    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.incoming_edges = []
        self.outgoing_edges = []


class Expr:
    def __init__(self, expr_index, instr_index, operation, address, size=8, constant=None):
        self.expr_index = expr_index
        self.instr_index = instr_index
        self.operation = operation
        self.address = address
        self.size = size
        self.constant = constant

    def traverse(self, callback):
        return [callback(self)]


class Instruction(Expr):
    def __init__(self, instr_index, operation, *, dest=None, size=None):
        super().__init__(100 + instr_index, instr_index, operation, 0x4000 + instr_index, size or 8)
        self.children = []
        if operation == M.MLIL_GOTO:
            self.dest = dest
        if operation == M.MLIL_STORE:
            self.dest = Expr(1000 + instr_index * 2, instr_index, M.MLIL_VAR, self.address)
            self.src = Expr(1001 + instr_index * 2, instr_index, M.MLIL_VAR, self.address, size)
            self.children = [self.dest, self.src]


class OldMLIL:
    def __init__(self, diamonds):
        self.instructions = {}
        self.expressions = {}
        for diamond in diamonds:
            for instruction in diamond.instructions:
                self.instructions[instruction.instr_index] = instruction
                self.expressions[instruction.expr_index] = instruction
                for child in instruction.children:
                    self.expressions[child.expr_index] = child
            for expression in diamond.value_exprs:
                self.expressions[expression.expr_index] = expression
        for expression in self.expressions.values():
            expression.function = self

    def __getitem__(self, index):
        return self.instructions[index]

    def get_expr(self, index):
        return self.expressions.get(index)


class NewMLIL:
    arch = types.SimpleNamespace(address_size=8)

    def const_pointer(self, size, value, loc=None):
        return ("const_pointer", size, value, loc)

    def load(self, size, src, loc=None):
        return ("load", size, src, loc)

    def store(self, size, dest, src, loc=None):
        return ("store", size, dest, src, loc)

    def nop(self, loc=None):
        return ("nop", loc)


class Diamond:
    def __init__(self, start=0, *, impure=False, external_entry=False):
        self.head = Block(start, start + 1)
        self.left = Block(start + 1, start + 2)
        self.right = Block(start + 2, start + 3)
        self.join = Block(start + 3, start + 5)
        self.left_edge = Edge(self.head, self.left)
        self.right_edge = Edge(self.head, self.right)
        self.left_join = Edge(self.left, self.join)
        self.right_join = Edge(self.right, self.join)
        self.head.outgoing_edges = [self.left_edge, self.right_edge]
        self.left.incoming_edges = [self.left_edge]
        self.right.incoming_edges = [self.right_edge]
        self.left.outgoing_edges = [self.left_join]
        self.right.outgoing_edges = [self.right_join]
        self.join.incoming_edges = [self.left_join, self.right_join]
        if external_entry:
            external = Block(start + 5, start + 6)
            external_edge = Edge(external, self.left)
            external.outgoing_edges = [external_edge]
            self.left.incoming_edges.append(external_edge)
        self.left_goto = Instruction(start + 1, M.MLIL_GOTO, dest=self.join.start)
        self.right_goto = Instruction(start + 2, M.MLIL_GOTO, dest=self.join.start)
        self.prefix = Instruction(start + 3, M.MLIL_CALL if impure else M.MLIL_SET_VAR)
        self.store = Instruction(start + 4, M.MLIL_STORE, size=4)
        for instruction, block in (
            (self.left_goto, self.left),
            (self.right_goto, self.right),
            (self.prefix, self.join),
            (self.store, self.join),
        ):
            instruction.il_basic_block = block
        for child in self.store.children:
            child.il_basic_block = self.join
        self.left_dest = Expr(2000 + start, self.left_goto.instr_index, M.MLIL_CONST_PTR, self.left_goto.address, constant=0x1000)
        self.left_src = Expr(2100 + start, self.left_goto.instr_index, M.MLIL_CONST_PTR, self.left_goto.address, constant=0x2000)
        self.right_dest = Expr(2200 + start, self.right_goto.instr_index, M.MLIL_CONST_PTR, self.right_goto.address, constant=0x2000)
        self.right_src = Expr(2300 + start, self.right_goto.instr_index, M.MLIL_CONST_PTR, self.right_goto.address, constant=0x1000)
        for expression, block in (
            (self.left_dest, self.left),
            (self.left_src, self.left),
            (self.right_dest, self.right),
            (self.right_src, self.right),
        ):
            expression.il_basic_block = block
        self.instructions = [self.left_goto, self.right_goto, self.prefix, self.store]
        self.value_exprs = [self.left_dest, self.left_src, self.right_dest, self.right_src]

    def plan(self, *, store=None, reverse=False):
        left = semantics.CorrelatedStoreArm(
            predecessor=self.left,
            incoming_edge=self.left_join,
            goto_il=self.left_goto,
            dest_expr=self.left_dest,
            dest_addr=self.left_dest.constant,
            src_expr=self.left_src,
            src_addr=self.left_src.constant,
        )
        right = semantics.CorrelatedStoreArm(
            predecessor=self.right,
            incoming_edge=self.right_join,
            goto_il=self.right_goto,
            dest_expr=self.right_dest,
            dest_addr=self.right_dest.constant,
            src_expr=self.right_src,
            src_addr=self.right_src.constant,
        )
        return semantics.CorrelatedStorePlan(
            store_il=self.store if store is None else store,
            join_block=self.join,
            size=4,
            arms=(right, left) if reverse else (left, right),
        )


def _copy_capture(monkeypatch, copied=None):
    built = {}
    new = NewMLIL()

    def copy(_ctx, replacements, *, mlil, preludes):
        built["replacements"] = {
            index: replacement(new, mlil[index]) for index, replacement in replacements.items()
        }
        built["preludes"] = {
            index: tuple(prelude(new, mlil[index])) for index, prelude in preludes.items()
        }
        return new, len(set(replacements) | set(preludes)) if copied is None else copied

    monkeypatch.setattr(correlated_stores, "copy_mlil_with_instruction_rewrites", copy)
    return built, new


def test_applies_explicit_edge_pairs_without_operand_order_or_value_deduplication(monkeypatch):
    diamond = Diamond()
    mlil = OldMLIL([diamond])
    built, new = _copy_capture(monkeypatch)

    result, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(mlil=mlil), mlil, (diamond.plan(reverse=True),)
    )

    assert (result, applied) == (new, 1)
    assert built["replacements"] == {diamond.store.instr_index: ("nop", ("loc", diamond.store.expr_index))}
    assert {
        index: (stores[0][2][2], stores[0][3][2][2])
        for index, stores in built["preludes"].items()
    } == {
        diamond.left_goto.instr_index: (0x1000, 0x2000),
        diamond.right_goto.instr_index: (0x2000, 0x1000),
    }


def test_rejects_raw_stale_or_ambiguous_plans_without_copying(monkeypatch):
    diamond = Diamond()
    mlil = OldMLIL([diamond])
    calls = []
    monkeypatch.setattr(correlated_stores, "copy_mlil_with_instruction_rewrites", lambda *_args, **_kwargs: calls.append(True))
    stale = Instruction(diamond.store.instr_index, M.MLIL_STORE, size=4)
    stale.address += 1
    stale.function = mlil
    stale.il_basic_block = diamond.join
    plan = diamond.plan()
    ambiguous = semantics.CorrelatedStorePlan(
        store_il=diamond.store,
        join_block=diamond.join,
        size=4,
        arms=(plan.arms[0], plan.arms[0]),
    )
    malformed = diamond.plan()
    object.__setattr__(malformed, "arms", [])
    boolean_address = diamond.plan()
    object.__setattr__(boolean_address.arms[0], "dest_addr", True)
    diamond.left_dest.constant = 1

    assert correlated_stores.apply_correlated_stores_mlil(types.SimpleNamespace(), mlil, ({},)) == (None, 0)
    assert correlated_stores.apply_correlated_stores_mlil(types.SimpleNamespace(), mlil, (diamond.plan(store=stale),)) == (None, 0)
    assert correlated_stores.apply_correlated_stores_mlil(types.SimpleNamespace(), mlil, (ambiguous,)) == (None, 0)
    assert correlated_stores.apply_correlated_stores_mlil(types.SimpleNamespace(), mlil, (malformed,)) == (None, 0)
    assert correlated_stores.apply_correlated_stores_mlil(types.SimpleNamespace(), mlil, (boolean_address,)) == (None, 0)
    assert calls == []


def test_rejects_external_entry_into_a_predecessor_arm(monkeypatch):
    diamond = Diamond(external_entry=True)
    mlil = OldMLIL([diamond])
    calls = []
    monkeypatch.setattr(correlated_stores, "copy_mlil_with_instruction_rewrites", lambda *_args, **_kwargs: calls.append(True))

    assert correlated_stores.apply_correlated_stores_mlil(types.SimpleNamespace(), mlil, (diamond.plan(),)) == (None, 0)
    assert calls == []


def test_rejects_partial_copy_atomically(monkeypatch):
    diamond = Diamond()
    mlil = OldMLIL([diamond])
    _built, _new = _copy_capture(monkeypatch, copied=2)

    assert correlated_stores.apply_correlated_stores_mlil(types.SimpleNamespace(), mlil, (diamond.plan(),)) == (None, 0)


def test_impure_candidate_does_not_block_an_independent_valid_plan(monkeypatch):
    impure = Diamond(0, impure=True)
    valid = Diamond(10)
    mlil = OldMLIL([impure, valid])
    built, new = _copy_capture(monkeypatch)

    result, applied = correlated_stores.apply_correlated_stores_mlil(
        types.SimpleNamespace(), mlil, (impure.plan(), valid.plan())
    )

    assert (result, applied) == (new, 1)
    assert set(built["replacements"]) == {valid.store.instr_index}
