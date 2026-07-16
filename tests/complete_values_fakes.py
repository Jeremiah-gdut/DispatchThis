"""Shared Binary Ninja-shaped fakes for complete-value tests."""

from __future__ import annotations

import sys

from binaryninja import LowLevelILOperation, MediumLevelILOperation

from conftest import load_plugin_module


class Op:
    def __init__(self, name):
        self.name = name


class Var:
    def __init__(self, reg, version):
        self.reg = reg
        self.version = version

    def __eq__(self, other):
        return isinstance(other, Var) and (self.reg, self.version) == (
            other.reg,
            other.version,
        )

    def __hash__(self):
        return hash((self.reg, self.version))

    def __str__(self):
        return f"{self.reg}#{self.version}"


class Expr:
    def __init__(self, op, **attrs):
        self.operation = LowLevelILOperation.__members__.get(
            op, MediumLevelILOperation.__members__.get(op, Op(op))
        )
        self.size = attrs.pop("size", 8)
        for key, value in attrs.items():
            setattr(self, key, value)


class Block:
    def __init__(self):
        self.incoming_edges = []
        self.outgoing_edges = []
        self.instructions = []

    def __iter__(self):
        return iter(self.instructions)


class Edge:
    def __init__(self, source, target, edge_type=None):
        self.source = source
        self.target = target
        self.type = edge_type
        source.outgoing_edges.append(self)
        target.incoming_edges.append(self)


class FakeSSA:
    def __init__(self, definitions, flag_definitions=None):
        self.definitions = definitions
        self.flag_definitions = {} if flag_definitions is None else flag_definitions

    def get_ssa_reg_definition(self, variable):
        return self.definitions.get(variable)

    def get_ssa_flag_definition(self, variable):
        return self.flag_definitions.get(variable)


def const(value, size=8):
    return Expr("LLIL_CONST", constant=value, size=size)


def reg(variable, size=8):
    return Expr("LLIL_REG_SSA", src=variable, size=size)


def set_reg(source, block=None, dest=None, instr_index=None):
    attributes = {"src": source, "il_basic_block": block}
    if dest is not None:
        attributes["dest"] = dest
        attributes["detailed_operands"] = (
            ("dest", dest, "reg_ssa"),
            ("src", source, "expr"),
        )
    if instr_index is not None:
        attributes["instr_index"] = instr_index
    return Expr("LLIL_SET_REG_SSA", **attributes)


def phi(*sources, block, size=8):
    return Expr("LLIL_REG_PHI", src=sources, il_basic_block=block, size=size)


def add(left, right, size=8):
    return Expr("LLIL_ADD", left=left, right=right, size=size)


def clear_value_modules():
    for name in tuple(sys.modules):
        if name.startswith("plugins.DispatchThis.helpers._values_"):
            del sys.modules[name]


def values_module():
    load_plugin_module("plugins.DispatchThis.semantics")
    clear_value_modules()
    return load_plugin_module("plugins.DispatchThis.helpers.values")
