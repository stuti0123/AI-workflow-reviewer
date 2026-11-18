"""Restricted Python subset: parsing, static inspection and *interpretation* (never exec()).

Supported subset (everything else is reported UNSUPPORTED and the snippet is left unreviewed):
  def route(t): ...                       exactly one parameter, t is the ticket dict
  statements: if/elif/else, return <expr>, name = <expr>, pass, docstring
  expressions: str/number/bool/None constants, local names, t["f"], t.get("f"),
               comparisons (< <= > >= == != is is-not in not-in with constant tuples/lists), and/or/not,
               unary minus, + - * / over numbers, parenthesised sub-expressions
Python semantics are kept: ordering a None raises TypeError -> reported as route "<error:TypeError>".
"""
from __future__ import annotations

import ast
from fractions import Fraction
from typing import Any

from .lang import FIELDS, Unsupported, frac


class _Err(Exception):
    pass


def parse_snippet(source: str, entry: str | None = None) -> tuple[ast.FunctionDef, str]:
    """Syntax check + locate entry function. Raises SyntaxError / Unsupported."""
    tree = ast.parse(source)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    others = [n for n in tree.body if not isinstance(n, (ast.FunctionDef,)) and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if others:
        raise Unsupported(f"module-level statement {type(others[0]).__name__} at line {others[0].lineno} is outside the subset")
    fn = next((f for f in fns if f.name == entry), fns[0] if fns and entry is None else None)
    if fn is None:
        raise Unsupported(f"entry function {entry!r} not found")
    if len(fn.args.args) != 1 or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs or fn.args.defaults:
        raise Unsupported("entry function must take exactly one positional parameter")
    _check_stmts(fn.body, fn.args.args[0].arg)
    return fn, fn.args.args[0].arg


def _check_stmts(body, p):
    for s in body:
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            continue
        if isinstance(s, ast.Pass):
            continue
        if isinstance(s, ast.Return):
            if s.value is None:
                raise Unsupported(f"bare return at line {s.lineno}")
            _check_expr(s.value, p)
        elif isinstance(s, ast.Assign):
            if len(s.targets) != 1 or not isinstance(s.targets[0], ast.Name):
                raise Unsupported(f"assignment form at line {s.lineno}")
            _check_expr(s.value, p)
        elif isinstance(s, ast.If):
            _check_expr(s.test, p)
            _check_stmts(s.body, p)
            _check_stmts(s.orelse, p)
        else:
            raise Unsupported(f"statement {type(s).__name__} at line {s.lineno} is outside the subset")


def _check_expr(e, p):
    if isinstance(e, ast.Constant):
        if isinstance(e.value, (str, int, float, bool)) or e.value is None:
            return
        raise Unsupported(f"constant type at line {e.lineno}")
    if isinstance(e, ast.Name):
        return
    if isinstance(e, ast.Subscript):
        if _is_field_access(e, p):
            return
        raise Unsupported(f"subscript form at line {e.lineno}")
    if isinstance(e, ast.Call):
        if _is_field_access(e, p):
            return
        raise Unsupported(f"call at line {e.lineno} is outside the subset")
    if isinstance(e, ast.BoolOp):
        for v in e.values:
            _check_expr(v, p)
        return
    if isinstance(e, ast.UnaryOp) and isinstance(e.op, (ast.Not, ast.USub)):
        _check_expr(e.operand, p)
        return
    if isinstance(e, ast.BinOp) and isinstance(e.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        _check_expr(e.left, p)
        _check_expr(e.right, p)
        return
    if isinstance(e, ast.Compare):
        if len(e.ops) != 1:
            raise Unsupported(f"chained comparison at line {e.lineno}")
        op = e.ops[0]
        if isinstance(op, (ast.In, ast.NotIn)):
            c = e.comparators[0]
            if not isinstance(c, (ast.Tuple, ast.List, ast.Set)) or not all(isinstance(x, ast.Constant) for x in c.elts):
                raise Unsupported(f"'in' needs a constant collection at line {e.lineno}")
        elif not isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
            raise Unsupported(f"comparison operator at line {e.lineno}")
        else:
            _check_expr(e.comparators[0], p)
        _check_expr(e.left, p)
        return
    raise Unsupported(f"expression {type(e).__name__} at line {getattr(e, 'lineno', '?')} is outside the subset")


def _is_field_access(e, p) -> bool:
    if isinstance(e, ast.Subscript):
        return (isinstance(e.value, ast.Name) and e.value.id == p and isinstance(e.slice, ast.Constant)
                and e.slice.value in FIELDS)
    if isinstance(e, ast.Call):
        f = e.func
        return (isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Name) and f.value.id == p
                and len(e.args) == 1 and not e.keywords and isinstance(e.args[0], ast.Constant) and e.args[0].value in FIELDS)
    return False


def constants_of(fn: ast.FunctionDef) -> tuple[set[Fraction], dict[str, set]]:
    """Numeric constants (with unit-scaled variants) and string literals, for grid construction."""
    nums: set[Fraction] = set()
    strs: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant):
            if isinstance(n.value, bool) or n.value is None:
                continue
            if isinstance(n.value, (int, float)):
                c = frac(n.value)
                for k in (Fraction(1), Fraction(60), Fraction(3600), Fraction(1, 60), Fraction(1, 3600), Fraction(1, 1440), Fraction(1440)):
                    nums.add(c * k)
                    nums.add(c / k)
            elif isinstance(n.value, str) and n.value not in FIELDS:
                strs.add(n.value)
    return nums, {"category": set(strs), "tier": set(strs)}


def run_snippet(fn: ast.FunctionDef, pname: str, ticket: dict) -> tuple[str, int | None]:
    """Interpret fn on ticket. Returns (route-or-sentinel, line of the executed return)."""
    env: dict[str, Any] = {}

    def ev(e):
        if isinstance(e, ast.Constant):
            v = e.value
            return frac(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        if isinstance(e, ast.Name):
            if e.id not in env:
                raise _Err("NameError")
            return env[e.id]
        if isinstance(e, ast.Subscript):
            return ticket.get(e.slice.value)
        if isinstance(e, ast.Call):
            return ticket.get(e.args[0].value)
        if isinstance(e, ast.BoolOp):
            if isinstance(e.op, ast.And):
                v = True
                for x in e.values:
                    v = ev(x)
                    if not v:
                        return v
                return v
            v = False
            for x in e.values:
                v = ev(x)
                if v:
                    return v
            return v
        if isinstance(e, ast.UnaryOp):
            v = ev(e.operand)
            if isinstance(e.op, ast.Not):
                return not v
            if v is None or isinstance(v, (str, bool)):
                raise _Err("TypeError")
            return -v
        if isinstance(e, ast.BinOp):
            a, b = ev(e.left), ev(e.right)
            if any(x is None or isinstance(x, (str, bool)) for x in (a, b)):
                raise _Err("TypeError")
            if isinstance(e.op, ast.Add):
                return a + b
            if isinstance(e.op, ast.Sub):
                return a - b
            if isinstance(e.op, ast.Mult):
                return a * b
            if b == 0:
                raise _Err("ZeroDivisionError")
            return Fraction(a) / Fraction(b)
        if isinstance(e, ast.Compare):
            a = ev(e.left)
            op = e.ops[0]
            if isinstance(op, (ast.In, ast.NotIn)):
                coll = [ev(x) for x in e.comparators[0].elts]
                r = a in coll
                return r if isinstance(op, ast.In) else not r
            b = ev(e.comparators[0])
            if isinstance(op, ast.Is):
                return a is b or (a is None and b is None) or (isinstance(a, bool) and a is b)
            if isinstance(op, ast.IsNot):
                return not (a is b or (a is None and b is None))
            if isinstance(op, ast.Eq):
                return a == b
            if isinstance(op, ast.NotEq):
                return a != b
            if a is None or b is None or isinstance(a, (str, bool)) != isinstance(b, (str, bool)) or isinstance(a, str) and not isinstance(b, str):
                raise _Err("TypeError")  # Python 3: ordering None/str/number mixes raises
            return a < b if isinstance(op, ast.Lt) else a <= b if isinstance(op, ast.LtE) else a > b if isinstance(op, ast.Gt) else a >= b
        raise _Err("Unsupported")

    def run(body):
        for s in body:
            if isinstance(s, ast.Return):
                v = ev(s.value)
                return ("ret", v, s.lineno)
            if isinstance(s, ast.Assign):
                env[s.targets[0].id] = ev(s.value)
            elif isinstance(s, ast.If):
                r = run(s.body if ev(s.test) else s.orelse)
                if r:
                    return r
        return None

    try:
        r = run(fn.body)
    except _Err as ex:
        return f"<error:{ex}>", None
    if r is None:
        return "<none>", None
    v = r[1]
    return (v if isinstance(v, str) else f"<non_route:{v!r}>"), r[2]
