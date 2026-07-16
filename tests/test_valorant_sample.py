import importlib.util
import types

from conftest import ROOT, load_plugin_module, temporary_modules


SAMPLE_PATH = ROOT / "sample" / "valorant" / "__init__.py"


class Operation:
    def __init__(self, name):
        self.name = name


class Instruction:
    def __init__(self, address, operation, size=8, **attrs):
        self.address = address
        self.operation = Operation(operation)
        self.size = size
        for name, value in attrs.items():
            setattr(self, name, value)


class Section:
    def __init__(self, start, data, semantics, type_="PROGBITS"):
        self.start = start
        self.end = start + len(data)
        self.semantics = types.SimpleNamespace(name=semantics)
        self.type = type_


class View:
    def __init__(self, executable=()):
        self.start = 0x1000
        self.data = bytes(range(0x80))
        self.sections = {
            ".data": Section(
                self.start,
                self.data,
                "ReadWriteDataSectionSemantics",
            ),
            ".bss": Section(
                0x2000,
                b"\0" * 0x20,
                "ReadWriteDataSectionSemantics",
                "NOBITS",
            ),
        }
        self.endianness = types.SimpleNamespace(name="LittleEndian")
        self.executable = set(executable)
        self.parsed = []
        self.native_types = {}

    def read(self, address, size):
        if self.start <= address and address + size <= self.start + len(self.data):
            offset = address - self.start
            return self.data[offset : offset + size]
        return None

    def is_offset_executable(self, address):
        return address in self.executable

    def parse_type_string(self, declaration):
        self.parsed.append(declaration)
        self.native_types.setdefault(declaration, object())
        return self.native_types[declaration], ""


class Edge:
    def __init__(self, source, target, kind):
        self.source = source
        self.target = target
        self.type = types.SimpleNamespace(name=kind)


class Block:
    def __init__(self, index, instructions):
        self.index = index
        self.instructions = tuple(instructions)
        self.incoming_edges = ()
        self.outgoing_edges = ()
        for instruction in self.instructions:
            instruction.il_basic_block = self

    def __iter__(self):
        return iter(self.instructions)


def _load_sample():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    values = load_plugin_module("plugins.DispatchThis.helpers.values")
    registered = []
    core = types.ModuleType("DispatchThis")
    for name in (
        "AnalysisBudget",
        "BranchTargetFact",
        "CallTargetFact",
        "CompleteBatch",
        "CompleteValues",
        "GlobalDataFact",
        "Handled",
        "Inconclusive",
        "NotHandled",
        "SampleSemantics",
        "StringRecoveryFact",
        "StringRecoveryQuery",
        "evaluate_values",
    ):
        setattr(core, name, getattr(values if hasattr(values, name) else semantics, name))
    core.register_provider = lambda provider: registered.append(provider) or True
    spec = importlib.util.spec_from_file_location("valorant_sample_test", SAMPLE_PATH)
    sample = importlib.util.module_from_spec(spec)
    with temporary_modules({"DispatchThis": core}, clear=("valorant_sample_test",)):
        spec.loader.exec_module(sample)
    return sample, semantics, values, registered


def _complete(values, targets, cases=()):
    return values.CompleteValues(
        tuple(targets),
        tuple(cases),
        values.DefinitionGraph((), (), (), ()),
    )


def _main():
    return types.SimpleNamespace(name="recovered_fragment", symbol=None)


class StackSSA:
    def __init__(self, expressions, memory_definitions, register_definitions=None, arch=None):
        self.expressions = tuple(expressions)
        self.memory_definitions = memory_definitions
        self.register_definitions = register_definitions or {}
        self.source_function = types.SimpleNamespace(arch=arch) if arch is not None else None

    def get_expr_count(self):
        return len(self.expressions)

    def get_expr(self, index):
        return self.expressions[index]

    def get_ssa_memory_definition(self, memory):
        return self.memory_definitions.get(memory)

    def get_ssa_reg_definition(self, variable):
        return self.register_definitions.get(variable)


class SSARegister:
    def __init__(self, index, version):
        self.reg = types.SimpleNamespace(index=index)
        self.version = version


class StackArchitecture:
    stack_pointer = "sp"

    def get_reg_index(self, _register):
        return 31


def _private_stack_spill(escaped=False, missing_call_predecessor=False):
    slot = Instruction(
        0x1000,
        "LLIL_ADD",
        expr_index=0,
        possible_values=types.SimpleNamespace(
            type=types.SimpleNamespace(name="StackFrameOffset"),
            offset=-0x20,
        ),
        detailed_operands=(),
    )
    source = Instruction(
        0x1004,
        "LLIL_CONST",
        expr_index=1,
        constant=0x59,
        detailed_operands=(),
    )
    store = Instruction(
        0x1008,
        "LLIL_STORE_SSA",
        expr_index=2,
        instr_index=2,
        dest=slot,
        src=source,
        size=8,
        src_memory=0,
        dest_memory=1,
        detailed_operands=(("dest", slot, "expr"), ("src", source, "expr")),
    )
    call = Instruction(
        0x100C,
        "LLIL_CALL_SSA",
        expr_index=3,
        stack_memory=None if missing_call_predecessor else 1,
        detailed_operands=(),
    )
    load = Instruction(
        0x1010,
        "LLIL_LOAD_SSA",
        expr_index=4,
        instr_index=4,
        src=slot,
        size=8,
        src_memory=2,
        detailed_operands=(("src", slot, "expr"),),
    )
    expressions = [slot, source, store, call, load]
    if escaped:
        expressions.append(
            Instruction(
                0x1014,
                "LLIL_SET_REG_SSA",
                expr_index=5,
                src=slot,
                detailed_operands=(("src", slot, "expr"),),
            )
        )
    block = types.SimpleNamespace(dominators=())
    store.il_basic_block = block
    load.il_basic_block = block
    return StackSSA(expressions, {1: store, 2: call}), load


def _syntactic_private_stack_spill():
    sp0 = SSARegister(31, 0)
    sp1 = SSARegister(31, 1)
    sp2 = SSARegister(31, 2)
    entry_sp = Instruction(0x1000, "LLIL_REG_SSA", expr_index=0, src=sp0, detailed_operands=())
    first_constant = Instruction(0x1004, "LLIL_CONST", expr_index=1, constant=0x60, detailed_operands=())
    first_sub = Instruction(
        0x1004,
        "LLIL_SUB",
        expr_index=2,
        left=entry_sp,
        right=first_constant,
        detailed_operands=(("left", entry_sp, "expr"), ("right", first_constant, "expr")),
    )
    first_set = Instruction(0x1004, "LLIL_SET_REG_SSA", expr_index=3, dest=sp1, src=first_sub, detailed_operands=(("src", first_sub, "expr"),))
    first_read = Instruction(0x1008, "LLIL_REG_SSA", expr_index=4, src=sp1, detailed_operands=())
    second_constant = Instruction(0x1008, "LLIL_CONST", expr_index=5, constant=0x300, detailed_operands=())
    second_sub = Instruction(
        0x1008,
        "LLIL_SUB",
        expr_index=6,
        left=first_read,
        right=second_constant,
        detailed_operands=(("left", first_read, "expr"), ("right", second_constant, "expr")),
    )
    second_set = Instruction(0x1008, "LLIL_SET_REG_SSA", expr_index=7, dest=sp2, src=second_sub, detailed_operands=(("src", second_sub, "expr"),))
    second_read = Instruction(0x100C, "LLIL_REG_SSA", expr_index=8, src=sp2, detailed_operands=())
    slot_constant = Instruction(0x100C, "LLIL_CONST", expr_index=9, constant=0x120, detailed_operands=())
    slot = Instruction(
        0x100C,
        "LLIL_ADD",
        expr_index=10,
        left=second_read,
        right=slot_constant,
        detailed_operands=(("left", second_read, "expr"), ("right", slot_constant, "expr")),
    )
    source = Instruction(0x1010, "LLIL_CONST", expr_index=11, constant=0x59, detailed_operands=())
    store = Instruction(0x1014, "LLIL_STORE_SSA", expr_index=12, dest=slot, src=source, size=8, src_memory=0, dest_memory=1, detailed_operands=(("dest", slot, "expr"), ("src", source, "expr")))
    call = Instruction(0x1018, "LLIL_CALL_SSA", expr_index=13, stack_memory=1, detailed_operands=())
    load = Instruction(0x101C, "LLIL_LOAD_SSA", expr_index=14, src=slot, size=8, src_memory=2, detailed_operands=(("src", slot, "expr"),))
    return (
        StackSSA(
            (entry_sp, first_constant, first_sub, first_set, first_read, second_constant, second_sub, second_set, second_read, slot_constant, slot, source, store, call, load),
            {1: store, 2: call},
            {sp1: first_set, sp2: second_set},
            StackArchitecture(),
        ),
        load,
    )


def _jump(address, destination):
    jump = Instruction(address, "LLIL_JUMP", dest=Instruction(address, "LLIL_REG"))
    jump.ssa_form = types.SimpleNamespace(dest=destination)
    jump.mlils = ()
    return jump


def _call(address, destination):
    call = Instruction(address, "MLIL_CALL", dest=Instruction(address, "MLIL_VAR"))
    call.ssa_form = types.SimpleNamespace(dest=destination)
    return call


def test_valorant_sample_registers_an_exact_provider():
    sample, _semantics, _values, registered = _load_sample()

    assert registered == [sample.provider]
    assert sample.provider.provider_id == "valorant-emdqx-0927cb886ad9a706"
    assert sample.provider.api_version == 4
    assert sample.provider.branch_targets is sample.branch_targets
    assert sample.provider.call_targets is sample.call_targets
    assert sample.provider.global_data is sample.global_data
    assert sample.provider.string_recovery is sample.string_recovery


def test_branch_scan_collects_every_current_jump_from_llil_only(monkeypatch):
    sample, semantics, values, _registered = _load_sample()
    first_llil_dest = object()
    second_llil_dest = object()
    second_mlil_dest = object()
    first = _jump(0x3010, first_llil_dest)
    second = _jump(0x3020, second_llil_dest)
    mapped = Instruction(0x3020, "MLIL_JUMP")
    mapped.ssa_form = types.SimpleNamespace(
        dest=second_mlil_dest,
        function=types.SimpleNamespace(),
    )
    second.mlils = (mapped,)
    llil_ssa = types.SimpleNamespace()
    llil = types.SimpleNamespace(instructions=(first, second), ssa_form=llil_ssa)
    view = View((0x4100, 0x4200, 0x4300))
    observed = []

    def evaluate(view_arg, il, destination, budget, policy):
        observed.append((view_arg, il, destination, budget, policy))
        if destination is first_llil_dest:
            return _complete(values, (0x4100,))
        if destination is second_llil_dest:
            return _complete(values, (0x4200, 0x4300))
        raise AssertionError("branch collection must not evaluate an MLIL mapping")

    monkeypatch.setattr(sample, "evaluate_values", evaluate)

    result = sample.branch_targets(semantics.BranchTargetQuery(view, _main(), llil))

    assert type(result) is semantics.CompleteBatch
    assert [(fact.jump_il.address, fact.targets) for fact in result.facts] == [
        (0x3010, (0x4100,)),
        (0x3020, (0x4200, 0x4300)),
    ]
    assert [item[2] for item in observed] == [
        first_llil_dest,
        second_llil_dest,
    ]
    assert all(type(item[3]) is values.AnalysisBudget for item in observed)


def test_branch_scan_rejects_a_partial_batch_after_inspecting_every_jump(monkeypatch):
    sample, semantics, values, _registered = _load_sample()
    first_dest = object()
    second_dest = object()
    first = _jump(0x5010, first_dest)
    second = _jump(0x5020, second_dest)
    llil = types.SimpleNamespace(instructions=(first, second), ssa_form=object())
    calls = []

    def evaluate(_view, _il, destination, _budget, _policy):
        calls.append(destination)
        return (
            _complete(values, (0x6100,))
            if destination is first_dest
            else semantics.Inconclusive("runtime value remains unknown")
        )

    monkeypatch.setattr(sample, "evaluate_values", evaluate)
    result = sample.branch_targets(
        semantics.BranchTargetQuery(View((0x6100,)), _main(), llil)
    )

    assert type(result) is semantics.Inconclusive
    assert calls == [first_dest, second_dest]
    assert "0x5020" in result.reason


def test_call_scan_preserves_the_full_automatically_collected_target_set(monkeypatch):
    sample, semantics, values, _registered = _load_sample()
    destination = object()
    call = _call(0x7010, destination)
    mlil_ssa = types.SimpleNamespace()
    mlil = types.SimpleNamespace(instructions=(call,), ssa_form=mlil_ssa)

    def evaluate(_view, il, actual_destination, _budget, _policy):
        assert il is mlil_ssa
        assert actual_destination is destination
        return _complete(values, (0x8200, 0x8100))

    monkeypatch.setattr(sample, "evaluate_values", evaluate)
    result = sample.call_targets(
        semantics.CallTargetQuery(View((0x8100, 0x8200)), _main(), mlil)
    )

    assert type(result) is semantics.CompleteBatch
    assert len(result.facts) == 1
    assert result.facts[0].call_il is call
    assert result.facts[0].targets == (0x8100, 0x8200)


def test_call_scan_omits_sites_without_a_complete_executable_target(monkeypatch):
    sample, semantics, values, _registered = _load_sample()
    proven_destination = object()
    unknown_destination = object()
    proven = _call(0x7010, proven_destination)
    unknown = _call(0x7020, unknown_destination)
    mlil = types.SimpleNamespace(
        instructions=(proven, unknown),
        ssa_form=types.SimpleNamespace(),
    )

    def evaluate(_view, _il, destination, _budget, _policy):
        if destination is proven_destination:
            return _complete(values, (0x8200,))
        return semantics.Inconclusive("dynamic call destination")

    monkeypatch.setattr(sample, "evaluate_values", evaluate)
    result = sample.call_targets(
        semantics.CallTargetQuery(View((0x8200,)), _main(), mlil)
    )

    assert type(result) is semantics.CompleteBatch
    assert [(fact.call_il.address, fact.targets) for fact in result.facts] == [
        (0x7010, (0x8200,)),
    ]


def test_static_snapshot_policy_and_global_scan_use_section_data_not_a_slot_list():
    sample, semantics, values, _registered = _load_sample()
    view = View()
    policy = sample._static_data_policy(view)
    address = view.start + 0x10
    load = Instruction(0, "MLIL_LOAD_SSA", size=8)

    known = policy(load, ((address,),))
    outside = policy(load, ((0x3000,),))

    pointer = Instruction(0, "MLIL_CONST_PTR", constant=address)
    direct_load = Instruction(0, "MLIL_LOAD", size=8, src=pointer)
    mlil = types.SimpleNamespace(
        instructions=(Instruction(0x9010, "MLIL_SET_VAR", src=direct_load),)
    )
    globals_result = sample.global_data(
        semantics.GlobalDataQuery(view, _main(), mlil)
    )

    assert type(known) is values.Handled
    assert known.values == (int.from_bytes(view.data[0x10:0x18], "little"),)
    assert type(outside) is semantics.Inconclusive
    assert type(globals_result) is semantics.CompleteBatch
    assert [(fact.slot_addr, fact.data_type) for fact in globals_result.facts] == [
        (address, view.native_types["uint64_t const"]),
    ]
    assert view.parsed == ["uint64_t const"]


def test_global_scan_finds_nested_static_loads_but_never_marks_local_writes_const():
    sample, semantics, _values, _registered = _load_sample()
    view = View()
    written_address = view.start + 0x10
    read_address = view.start + 0x18
    written_load = Instruction(
        0,
        "MLIL_LOAD",
        expr_index=2,
        size=8,
        src=Instruction(0, "MLIL_CONST_PTR", expr_index=1, constant=written_address),
    )
    read_load = Instruction(
        0,
        "MLIL_LOAD",
        expr_index=5,
        size=8,
        src=Instruction(0, "MLIL_CONST_PTR", expr_index=4, constant=read_address),
    )
    nested = Instruction(0, "MLIL_ZX", expr_index=6, src=written_load)
    store = Instruction(
        0x9014,
        "MLIL_STORE",
        expr_index=8,
        size=1,
        dest=Instruction(
            0,
            "MLIL_CONST_PTR",
            expr_index=7,
            constant=written_address,
        ),
        src=Instruction(0, "MLIL_CONST", expr_index=9, constant=0),
    )
    mlil = types.SimpleNamespace(
        instructions=(
            Instruction(0x9010, "MLIL_SET_VAR", expr_index=3, src=nested),
            Instruction(
                0x9012,
                "MLIL_SET_VAR",
                expr_index=10,
                src=Instruction(0, "MLIL_ZX", expr_index=11, src=read_load),
            ),
            store,
        )
    )

    result = sample.global_data(semantics.GlobalDataQuery(view, _main(), mlil))

    assert type(result) is semantics.CompleteBatch
    assert [(fact.slot_addr, fact.data_type) for fact in result.facts] == [
        (read_address, view.native_types["uint64_t const"]),
    ]
    assert view.parsed == ["uint64_t const"]


def _string_view(source, data):
    view = View()
    image = bytearray(view.data)
    offset = source - view.start
    image[offset : offset + len(data)] = data
    view.data = bytes(image)
    view.sections[".data"].end = view.start + len(view.data)
    return view


def _const_pointer(address, index):
    return Instruction(0, "MLIL_CONST_PTR", expr_index=index, constant=address)


def _variable(variable, index):
    return Instruction(0, "MLIL_VAR", expr_index=index, src=variable)


def _loop_blocks(store, increment, branch, preheader, exit_block, before=()):
    loop = Block(1, tuple(before) + (store, increment, branch))
    preheader.outgoing_edges = (Edge(preheader, loop, "UnconditionalBranch"),)
    loop.incoming_edges = (preheader.outgoing_edges[0],)
    back_edge = Edge(loop, loop, "TrueBranch")
    exit_edge = Edge(loop, exit_block, "FalseBranch")
    loop.outgoing_edges = (back_edge, exit_edge)
    loop.incoming_edges += (back_edge,)
    exit_block.incoming_edges = (exit_edge,)
    return loop


def test_string_recovery_executes_a_direct_two_pointer_decoder_loop():
    sample, semantics, _values, _registered = _load_sample()
    source = 0x1040
    destination = 0x2000
    target = 0x5000
    view = _string_view(source, b"A\0")
    output_parameter = object()
    source_parameter = object()
    index_variable = object()
    preheader = Block(
        0,
        (
            Instruction(
                0x5000,
                "MLIL_SET_VAR",
                instr_index=0,
                dest=index_variable,
                src=Instruction(0, "MLIL_CONST", expr_index=1, constant=0),
            ),
            Instruction(0x5004, "MLIL_GOTO", instr_index=1, dest=2),
        ),
    )
    destination_pointer = Instruction(
        0,
        "MLIL_ADD",
        expr_index=2,
        left=_variable(output_parameter, 3),
        right=_variable(index_variable, 4),
    )
    source_pointer = Instruction(
        0,
        "MLIL_ADD",
        expr_index=5,
        left=_variable(source_parameter, 6),
        right=_variable(index_variable, 7),
    )
    store = Instruction(
        0x5008,
        "MLIL_STORE",
        instr_index=2,
        size=1,
        dest=destination_pointer,
        src=Instruction(0, "MLIL_LOAD", expr_index=8, size=1, src=source_pointer),
    )
    increment = Instruction(
        0x500C,
        "MLIL_SET_VAR",
        instr_index=3,
        dest=index_variable,
        src=Instruction(
            0,
            "MLIL_ADD",
            expr_index=9,
            left=_variable(index_variable, 10),
            right=Instruction(0, "MLIL_CONST", expr_index=11, constant=1),
        ),
    )
    branch = Instruction(
        0x5010,
        "MLIL_IF",
        instr_index=4,
        condition=Instruction(
            0,
            "MLIL_CMP_ULT",
            expr_index=12,
            left=_variable(index_variable, 13),
            right=Instruction(0, "MLIL_CONST", expr_index=14, constant=2),
        ),
        true=2,
        false=5,
    )
    exit_block = Block(2, (Instruction(0x5014, "MLIL_RET", instr_index=5),))
    _loop_blocks(store, increment, branch, preheader, exit_block)
    decoder = types.SimpleNamespace(
        parameter_vars=(output_parameter, source_parameter),
        medium_level_il=types.SimpleNamespace(
            instructions=preheader.instructions + (store, increment, branch) + exit_block.instructions
        ),
    )
    view.get_function_at = lambda address: decoder if address == target else None
    call = Instruction(
        0x7010,
        "MLIL_CALL",
        instr_index=0,
        dest=_const_pointer(target, 20),
        params=(_const_pointer(destination, 21), _const_pointer(source, 22)),
    )

    result = sample.string_recovery(
        semantics.StringRecoveryQuery(
            view,
            _main(),
            types.SimpleNamespace(instructions=(call,)),
            frozenset(),
        )
    )

    assert type(result) is semantics.CompleteBatch
    assert [
        (fact.call_addr, fact.source_addr, fact.destination_addr, fact.plaintext)
        for fact in result.facts
    ] == [(0x7010, source, destination, b"A")]


def test_string_recovery_executes_a_current_function_inline_loop():
    sample, semantics, _values, _registered = _load_sample()
    source = 0x1040
    destination = 0x2000
    view = _string_view(source, b"A\0")
    second_source = source + 0x10
    image = bytearray(view.data)
    image[second_source - view.start : second_source - view.start + 2] = b"\0\0"
    view.data = bytes(image)
    index_variable = object()
    state_variable = object()
    preheader = Block(
        0,
        (
            Instruction(
                0x7100,
                "MLIL_SET_VAR",
                instr_index=0,
                dest=index_variable,
                src=Instruction(0, "MLIL_CONST", expr_index=30, constant=0),
            ),
            Instruction(
                0x7104,
                "MLIL_SET_VAR",
                instr_index=1,
                dest=state_variable,
                src=Instruction(0, "MLIL_CONST", expr_index=31, constant=0),
            ),
            Instruction(0x7104, "MLIL_GOTO", instr_index=2, dest=3),
        ),
    )
    destination_pointer = Instruction(
        0,
        "MLIL_ADD",
        expr_index=32,
        left=_const_pointer(destination, 33),
        right=_variable(index_variable, 34),
    )
    first_source_pointer = Instruction(
        0,
        "MLIL_ADD",
        expr_index=35,
        left=_const_pointer(source, 36),
        right=_variable(index_variable, 37),
    )
    second_source_pointer = Instruction(
        0,
        "MLIL_ADD",
        expr_index=38,
        left=_const_pointer(second_source, 39),
        right=_variable(index_variable, 40),
    )
    state_update = Instruction(
        0x7108,
        "MLIL_SET_VAR",
        instr_index=3,
        dest=state_variable,
        src=Instruction(
            0,
            "MLIL_XOR",
            expr_index=41,
            left=Instruction(
                0,
                "MLIL_LOAD",
                expr_index=42,
                size=1,
                src=first_source_pointer,
            ),
            right=Instruction(
                0,
                "MLIL_LOAD",
                expr_index=43,
                size=1,
                src=second_source_pointer,
            ),
        ),
    )
    store = Instruction(
        0x710C,
        "MLIL_STORE",
        instr_index=4,
        size=1,
        dest=destination_pointer,
        src=Instruction(
            0,
            "MLIL_VAR_FIELD",
            expr_index=44,
            size=1,
            src=state_variable,
            offset=0,
        ),
    )
    increment = Instruction(
        0x7110,
        "MLIL_SET_VAR",
        instr_index=5,
        dest=index_variable,
        src=Instruction(
            0,
            "MLIL_ADD",
            expr_index=45,
            left=_variable(index_variable, 46),
            right=Instruction(0, "MLIL_CONST", expr_index=47, constant=1),
        ),
    )
    branch = Instruction(
        0x7114,
        "MLIL_IF",
        instr_index=6,
        condition=Instruction(
            0,
            "MLIL_CMP_NE",
            expr_index=48,
            left=_variable(index_variable, 49),
            right=Instruction(0, "MLIL_CONST", expr_index=50, constant=2),
        ),
        true=3,
        false=7,
    )
    consumer = Instruction(
        0x7118,
        "MLIL_CALL",
        instr_index=7,
        dest=_const_pointer(0x6000, 51),
        params=(_const_pointer(destination, 52),),
    )
    exit_block = Block(2, (consumer, Instruction(0x711C, "MLIL_RET", instr_index=8)))
    _loop_blocks(store, increment, branch, preheader, exit_block, (state_update,))
    mlil = types.SimpleNamespace(
        instructions=preheader.instructions
        + (state_update, store, increment, branch)
        + exit_block.instructions
    )

    result = sample.string_recovery(
        semantics.StringRecoveryQuery(view, _main(), mlil, frozenset())
    )

    assert type(result) is semantics.CompleteBatch
    assert [
        (fact.call_addr, fact.source_addr, fact.destination_addr, fact.plaintext)
        for fact in result.facts
    ] == [(0x7118, source, destination, b"A")]


def test_string_recovery_groups_proven_static_byte_stores_at_their_consumer(monkeypatch):
    sample, semantics, values, _registered = _load_sample()
    source = 0x1040
    destination = 0x2000
    view = _string_view(source, b"A\0")
    first_source = Instruction(
        0,
        "MLIL_LOAD_SSA",
        expr_index=50,
        size=1,
        src=_const_pointer(source, 51),
    )
    second_source = Instruction(
        0,
        "MLIL_LOAD_SSA",
        expr_index=52,
        size=1,
        src=_const_pointer(source + 1, 53),
    )
    first = Instruction(
        0x7200,
        "MLIL_STORE",
        instr_index=0,
        size=1,
        dest=_const_pointer(destination, 54),
        ssa_form=types.SimpleNamespace(src=first_source),
    )
    second = Instruction(
        0x7204,
        "MLIL_STORE",
        instr_index=1,
        size=1,
        dest=_const_pointer(destination + 1, 55),
        ssa_form=types.SimpleNamespace(src=second_source),
    )
    consumer = Instruction(
        0x7208,
        "MLIL_CALL",
        instr_index=2,
        dest=_const_pointer(0x6000, 56),
        params=(_const_pointer(destination, 57),),
    )
    block = Block(0, (first, second, consumer, Instruction(0x720C, "MLIL_RET", instr_index=3)))
    mlil = types.SimpleNamespace(
        instructions=block.instructions,
        ssa_form=types.SimpleNamespace(),
    )

    def evaluate(_view, _ssa, expression, _policy):
        value = 0x41 if expression is first_source else 0
        return _complete(values, (value,))

    monkeypatch.setattr(sample, "_evaluate", evaluate)
    result = sample.string_recovery(
        semantics.StringRecoveryQuery(view, _main(), mlil, frozenset())
    )

    assert type(result) is semantics.CompleteBatch
    assert [
        (fact.call_addr, fact.source_addr, fact.destination_addr, fact.plaintext)
        for fact in result.facts
    ] == [(0x7208, source, destination, b"A")]


def test_string_recovery_replays_a_static_initializer_pattern_from_its_llil_loads():
    sample, semantics, _values, _registered = _load_sample()
    source = 0x1040
    destination = 0x2000
    view = _string_view(source, b"A")
    first = Instruction(
        0x7300,
        "MLIL_STORE",
        instr_index=0,
        size=1,
        dest=_const_pointer(destination, 60),
        src=Instruction(0, "MLIL_CONST", expr_index=61, constant=0x41),
    )
    second = Instruction(
        0x7304,
        "MLIL_STORE",
        instr_index=1,
        size=1,
        dest=_const_pointer(destination + 1, 62),
        src=Instruction(0, "MLIL_CONST", expr_index=63, constant=0),
    )
    consumer = Instruction(
        0x7308,
        "MLIL_CALL",
        instr_index=2,
        dest=_const_pointer(0x6000, 64),
        params=(_const_pointer(destination, 65),),
    )
    mlil_block = Block(0, (first, second, consumer))
    llil_load = Instruction(
        0x7300,
        "LLIL_LOAD",
        expr_index=66,
        size=1,
        src=_const_pointer(source, 67),
    )
    llil_store = Instruction(
        0x7300,
        "LLIL_STORE",
        instr_index=0,
        size=1,
        dest=_const_pointer(destination, 68),
        src=llil_load,
    )
    llil_block = Block(0, (llil_store,))
    query = semantics.StringRecoveryQuery(
        view,
        types.SimpleNamespace(low_level_il=types.SimpleNamespace(instructions=llil_block.instructions)),
        types.SimpleNamespace(instructions=mlil_block.instructions),
        frozenset(),
    )

    result = sample._recover_static_initializer_strings(query)

    assert [
        (fact.call_addr, fact.source_addr, fact.destination_addr, fact.plaintext)
        for fact in result
    ] == [(0x7308, source, destination, b"A")]


def test_branch_stack_spills_are_resolved_only_when_the_slot_has_not_escaped():
    sample, _semantics, values, _registered = _load_sample()
    view = View()
    policy = sample._static_data_policy(view)
    ssa, load = _private_stack_spill()

    spill_values = sample._private_stack_load_values(view, ssa, policy)
    stack_policy = sample._ValuePolicy(policy.byte_order, policy.regions, spill_values)

    assert spill_values == ((load.expr_index, 0x59),)
    resolved = stack_policy.resolve_load(load)
    assert type(resolved) is values.Handled
    assert resolved.values == (0x59,)

    escaped_ssa, _escaped_load = _private_stack_spill(escaped=True)
    assert sample._private_stack_load_values(view, escaped_ssa, policy) == ()

    incomplete_ssa, incomplete_load = _private_stack_spill(
        missing_call_predecessor=True
    )
    assert sample._private_stack_load_values(view, incomplete_ssa, policy) == (
        (incomplete_load.expr_index, 0x59),
    )


def test_branch_stack_spills_follow_a_proven_ssa_stack_pointer_chain_without_vsa():
    sample, _semantics, _values, _registered = _load_sample()
    view = View()
    policy = sample._static_data_policy(view)
    ssa, load = _syntactic_private_stack_spill()

    assert sample._vsa_stack_offset(load.src) is None
    assert sample._stack_offset(load.src, ssa) == -0x240
    assert sample._private_stack_load_values(view, ssa, policy) == ((load.expr_index, 0x59),)


def test_branch_scan_recovers_a_directed_condition_from_core_edge_evidence(monkeypatch):
    sample, semantics, values, _registered = _load_sample()
    flag_token = object()
    comparison = Instruction(0xA000, "LLIL_CMP_NE")
    set_flag = Instruction(0xA000, "LLIL_SET_FLAG", size=0, dest=flag_token, src=comparison)
    condition = Instruction(0xA004, "LLIL_FLAG", size=0, src=flag_token)
    branch = Instruction(0xA004, "LLIL_IF", size=0, condition=condition)
    true_arm = Block(1, (Instruction(0xA008, "LLIL_GOTO", size=0),))
    false_arm = Block(2, (Instruction(0xA00C, "LLIL_GOTO", size=0),))
    parent = Block(0, (set_flag, branch))
    jump_dest = object()
    jump = _jump(0xA020, jump_dest)
    join = Block(3, (jump,))
    true_from_parent = Edge(parent, true_arm, "TrueBranch")
    false_from_parent = Edge(parent, false_arm, "FalseBranch")
    true_to_join = Edge(true_arm, join, "UnconditionalBranch")
    false_to_join = Edge(false_arm, join, "UnconditionalBranch")
    parent.outgoing_edges = (true_from_parent, false_from_parent)
    true_arm.incoming_edges = (true_from_parent,)
    false_arm.incoming_edges = (false_from_parent,)
    true_arm.outgoing_edges = (true_to_join,)
    false_arm.outgoing_edges = (false_to_join,)
    join.incoming_edges = (true_to_join, false_to_join)
    llil = types.SimpleNamespace(
        instructions=(set_flag, branch, jump),
        ssa_form=types.SimpleNamespace(),
    )
    cases = (
        values.ValueCase(0xB000, (values.PathSource((true_to_join,)),)),
        values.ValueCase(0xC000, (values.PathSource((false_to_join,)),)),
    )

    monkeypatch.setattr(
        sample,
        "evaluate_values",
        lambda *_args: _complete(values, (0xB000, 0xC000), cases),
    )
    result = sample.branch_targets(
        semantics.BranchTargetQuery(View((0xB000, 0xC000)), _main(), llil)
    )

    assert type(result) is semantics.CompleteBatch
    fact = result.facts[0]
    assert fact.condition is comparison
    assert fact.true_target == 0xB000
    assert fact.false_target == 0xC000
