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


class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target
        source.outgoing_edges.append(self)
        target.incoming_edges.append(self)


class FakeSSA:
    def __init__(self, definitions):
        self.definitions = definitions

    def get_ssa_reg_definition(self, variable):
        return self.definitions.get(variable)


def const(value, size=8):
    return Expr("LLIL_CONST", constant=value, size=size)


def reg(variable, size=8):
    return Expr("LLIL_REG_SSA", src=variable, size=size)


def set_reg(source, block=None):
    return Expr("LLIL_SET_REG_SSA", src=source, il_basic_block=block)


def phi(*sources, block):
    return Expr("LLIL_REG_PHI", src=sources, il_basic_block=block)


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
