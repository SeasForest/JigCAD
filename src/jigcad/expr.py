"""Safe arithmetic expression evaluator for spec values.

A spec value that is a string starting with ``=`` is an expression, e.g.
``"=L - 2*margin"`` or ``"=base.xmax - 10"``. Only arithmetic, comparisons,
conditionals, whitelisted math functions, names from the namespace and
attribute access on namespace objects are allowed.
"""
from __future__ import annotations

import ast
import math
import operator
from typing import Any

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMPOPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}
FUNCS = {
    "sqrt": math.sqrt, "sin": lambda d: math.sin(math.radians(d)),
    "cos": lambda d: math.cos(math.radians(d)), "tan": lambda d: math.tan(math.radians(d)),
    "atan2": lambda y, x: math.degrees(math.atan2(y, x)),
    "abs": abs, "min": min, "max": max, "round": round, "floor": math.floor,
    "ceil": math.ceil, "int": int, "float": float, "hypot": math.hypot,
}
CONSTS = {"pi": math.pi, "true": True, "false": False, "True": True, "False": False}


class ExprError(ValueError):
    pass


class _Eval:
    def __init__(self, ns: dict[str, Any]):
        self.ns = ns

    def __call__(self, node):
        m = getattr(self, "_" + type(node).__name__, None)
        if m is None:
            raise ExprError(f"syntax not allowed: {type(node).__name__}")
        return m(node)

    def _Expression(self, n):
        return self(n.body)

    def _Constant(self, n):
        if isinstance(n.value, (int, float, bool, str)):
            return n.value
        raise ExprError("constant type not allowed")

    def _Name(self, n):
        if n.id in self.ns:
            return self.ns[n.id]
        if n.id in CONSTS:
            return CONSTS[n.id]
        raise ExprError(f"unknown name '{n.id}'")

    def _Attribute(self, n):
        obj = self(n.value)
        if n.attr.startswith("_"):
            raise ExprError("private attribute")
        if isinstance(obj, dict):
            if n.attr in obj:
                return obj[n.attr]
            raise ExprError(f"unknown attribute '{n.attr}' (have: {', '.join(sorted(obj))})")
        if hasattr(obj, "expr_attrs") and n.attr in obj.expr_attrs():
            return getattr(obj, n.attr)
        raise ExprError(f"unknown attribute '{n.attr}'")

    def _Subscript(self, n):
        obj = self(n.value)
        idx = self(n.slice)
        try:
            return obj[idx]
        except Exception as e:  # noqa: BLE001
            raise ExprError(f"bad index {idx!r}") from e

    def _List(self, n):
        return [self(e) for e in n.elts]

    _Tuple = _List

    def _BinOp(self, n):
        op = _BINOPS.get(type(n.op))
        if op is None:
            raise ExprError("operator not allowed")
        a, b = self(n.left), self(n.right)
        if isinstance(n.op, ast.Pow) and abs(b) > 100:
            raise ExprError("exponent too large")
        return op(a, b)

    def _UnaryOp(self, n):
        return _UNOPS[type(n.op)](self(n.operand))

    def _BoolOp(self, n):
        vals = [self(v) for v in n.values]
        return all(vals) if isinstance(n.op, ast.And) else any(vals)

    def _Compare(self, n):
        left = self(n.left)
        for op, comp in zip(n.ops, n.comparators):
            right = self(comp)
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True

    def _IfExp(self, n):
        return self(n.body) if self(n.test) else self(n.orelse)

    def _Call(self, n):
        if not isinstance(n.func, ast.Name) or n.func.id not in FUNCS:
            raise ExprError("only math functions may be called: " + ", ".join(sorted(FUNCS)))
        if n.keywords:
            raise ExprError("keyword arguments not allowed")
        return FUNCS[n.func.id](*[self(a) for a in n.args])


def evaluate(src: str, ns: dict[str, Any]) -> Any:
    if len(src) > 500:
        raise ExprError("expression too long")
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error in '{src}'") from e
    return _Eval(ns)(tree)


def is_expr(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("=")


def resolve(v: Any, ns: dict[str, Any]) -> Any:
    """Recursively resolve ``=expr`` strings inside lists/dicts."""
    if is_expr(v):
        return evaluate(v[1:], ns)
    if isinstance(v, list):
        return [resolve(x, ns) for x in v]
    if isinstance(v, dict):
        return {k: resolve(x, ns) for k, x in v.items()}
    return v
