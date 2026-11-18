"""Declared structured language, ticket domain, policy evaluation and requirement-prose parser.

Supported structured condition language (JSON):
  {"op":"true"}
  {"op":"missing","field":F}                       field is null/absent
  {"op":"eq","field":F,"value":V}                  equality (bool/str); null never equals a value
  {"op":"cmp","field":F,"cmp":"<"|"<="|">"|">="|"==","value":N,"unit":U}   numeric (time fields need unit)
  {"op":"and"|"or","args":[...]}                   nested, n-ary
  {"op":"not","arg":C}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product
from typing import Any

# ----------------------------------------------------------------------------- domain
ROUTES = ["manual_review", "priority_queue", "escalation", "normal_queue"]
FIELDS: dict[str, dict[str, Any]] = {
    "category": {"type": "str", "nullable": True},
    "tier": {"type": "str", "nullable": True},
    "urgent": {"type": "bool"},
    "waiting_minutes": {"type": "num", "unit": "minutes", "min": 0},
}
UNIT_FACTORS = {"seconds": Fraction(1, 60), "minutes": Fraction(1), "hours": Fraction(60), "days": Fraction(1440)}
OTHER = "__other__"  # representative of "any string not named anywhere in the bundle"
CMP_OPS = {"<", "<=", ">", ">=", "=="}


class Unsupported(Exception):
    """Content outside the declared language. Never swallowed: surfaces in coverage."""

    def __init__(self, reason: str, kind: str = "unsupported"):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind  # "unsupported" | "ambiguous"


def frac(v: Any) -> Fraction:
    if isinstance(v, bool):
        raise Unsupported("boolean used as number")
    if isinstance(v, Fraction):
        return v
    return Fraction(str(v))


def num_out(v: Fraction | int | float | None):
    if isinstance(v, Fraction):
        return int(v) if v.denominator == 1 else float(v)
    return v


# ----------------------------------------------------------------------------- conditions
def normalize(cond: dict) -> dict:
    """Validate + canonicalise (units -> minutes). Raises Unsupported on anything outside the language."""
    if not isinstance(cond, dict) or "op" not in cond:
        raise Unsupported(f"condition is not an object with 'op': {cond!r}")
    op = cond["op"]
    if op == "true":
        return {"op": "true"}
    if op in ("missing", "eq", "cmp"):
        f = cond.get("field")
        if f not in FIELDS:
            raise Unsupported(f"unknown field {f!r}")
        spec = FIELDS[f]
        if op == "missing":
            if not spec.get("nullable"):
                raise Unsupported(f"field {f!r} is not nullable; 'missing' is undefined")
            return {"op": "missing", "field": f}
        if op == "eq":
            if spec["type"] == "num":
                raise Unsupported("use cmp '==' for numeric fields")
            v = cond.get("value")
            if spec["type"] == "bool" and not isinstance(v, bool):
                raise Unsupported(f"field {f!r} compared with non-boolean {v!r}")
            if spec["type"] == "str" and not isinstance(v, str):
                raise Unsupported(f"field {f!r} compared with non-string {v!r}")
            return {"op": "eq", "field": f, "value": v}
        # cmp
        if spec["type"] != "num":
            raise Unsupported(f"numeric comparison on non-numeric field {f!r}")
        c = cond.get("cmp")
        if c not in CMP_OPS:
            raise Unsupported(f"unknown comparison {c!r}")
        if "value" not in cond or isinstance(cond["value"], (bool, str)) or cond["value"] is None:
            raise Unsupported("numeric comparison needs a numeric value")
        unit = cond.get("unit")
        if unit is None:
            raise Unsupported(f"time comparison on {f!r} has no declared unit", kind="ambiguous")
        if unit not in UNIT_FACTORS:
            raise Unsupported(f"unknown unit {unit!r}", kind="ambiguous")
        return {"op": "cmp", "field": f, "cmp": c, "value": frac(cond["value"]) * UNIT_FACTORS[unit]}
    if op in ("and", "or"):
        args = cond.get("args")
        if not isinstance(args, list) or not args:
            raise Unsupported(f"'{op}' needs a non-empty args list")
        return {"op": op, "args": [normalize(a) for a in args]}
    if op == "not":
        return {"op": "not", "arg": normalize(cond.get("arg"))}
    raise Unsupported(f"unknown op {op!r}")


def eval_cond(c: dict, t: dict) -> bool:
    """c must be normalised; t is a ticket with Fraction numerics."""
    op = c["op"]
    if op == "true":
        return True
    if op == "missing":
        return t.get(c["field"]) is None
    if op == "eq":
        return t.get(c["field"]) == c["value"]
    if op == "cmp":
        v = t.get(c["field"])
        if v is None:
            return False
        x = c["value"]
        o = c["cmp"]
        return v < x if o == "<" else v <= x if o == "<=" else v > x if o == ">" else v >= x if o == ">=" else v == x
    if op == "and":
        return all(eval_cond(a, t) for a in c["args"])
    if op == "or":
        return any(eval_cond(a, t) for a in c["args"])
    if op == "not":
        return not eval_cond(c["arg"], t)
    raise AssertionError(op)


def atoms(c: dict) -> list[tuple]:
    op = c["op"]
    if op == "true":
        return []
    if op == "missing":
        return [(c["field"], "missing", None)]
    if op == "eq":
        return [(c["field"], "eq", c["value"])]
    if op == "cmp":
        return [(c["field"], c["cmp"], c["value"])]
    if op in ("and", "or"):
        return [a for x in c["args"] for a in atoms(x)]
    return atoms(c["arg"])


def cond_text(c: dict) -> str:
    op = c["op"]
    if op == "true":
        return "true"
    if op == "missing":
        return f"{c['field']} is missing"
    if op == "eq":
        return f"{c['field']} == {c['value']!r}"
    if op == "cmp":
        return f"{c['field']} {c['cmp']} {num_out(c['value'])} (minutes)"
    if op == "not":
        return f"not ({cond_text(c['arg'])})"
    return "(" + f" {op} ".join(cond_text(a) for a in c["args"]) + ")"


# ----------------------------------------------------------------------------- policies
@dataclass
class Rule:
    id: str
    order: int
    condition: dict  # normalised
    route: str
    exceptions: list[tuple[str, dict, str]] = field(default_factory=list)  # (id, cond, route) ordered
    ref: str | None = None
    location: str | None = None


@dataclass
class Policy:
    rules: list[Rule]
    default_route: str | None


def route_policy(p: Policy, t: dict) -> tuple[str, str]:
    for r in sorted(p.rules, key=lambda r: r.order):
        if eval_cond(r.condition, t):
            for xid, xc, xr in r.exceptions:
                if eval_cond(xc, t):
                    return xr, xid
            return r.route, r.id
    return (p.default_route or "<no_route>"), "default"


# ----------------------------------------------------------------------------- tickets / grid
def ticket_out(t: dict) -> dict:
    return {k: num_out(v) for k, v in t.items()}


def ticket_in(raw: dict) -> dict:
    t = {}
    for f, spec in FIELDS.items():
        v = raw.get(f)
        if spec["type"] == "num":
            if v is None:
                raise ValueError(f"ticket field {f} is required")
            t[f] = frac(v)
        else:
            t[f] = v
    return t


def build_grid(thresholds: set[Fraction], literals: dict[str, set], include_boundaries: bool = True,
               cap: int = 200_000) -> tuple[list[dict], dict]:
    """Representative tickets such that every predicate of the declared language (comparisons with the given
    constants, equality with the given literals, null checks) is constant inside each equivalence class.
    Exhaustive for that language: one point per threshold, one per open interval between thresholds."""
    ts = sorted({Fraction(0)} | {x for x in thresholds if x >= 0})
    pts = set()
    if include_boundaries:
        pts |= set(ts)
    pts |= {(a + b) / 2 for a, b in zip(ts, ts[1:])}
    pts.add(ts[-1] + 1)
    wait_vals = sorted(pts)
    cats = {}
    for f, spec in FIELDS.items():
        if spec["type"] == "str":
            cats[f] = [None] + sorted(literals.get(f, set())) + [OTHER]
    combos = len(cats["category"]) * len(cats["tier"]) * 2 * len(wait_vals)
    meta = {"grid_size": combos, "waiting_points": [num_out(v) for v in wait_vals]}
    if combos > cap:
        meta["over_cap"] = True
        return [], meta
    grid = [
        {"category": c, "tier": ti, "urgent": u, "waiting_minutes": w}
        for c, ti, u, w in product(cats["category"], cats["tier"], [False, True], wait_vals)
    ]
    return grid, meta


def literals_of(c: dict, out: dict[str, set]) -> None:
    for f, k, v in atoms(c):
        if k == "eq" and isinstance(v, str):
            out.setdefault(f, set()).add(v)


# ----------------------------------------------------------------------------- prose parser
_ROUTE_ALT = "|".join(sorted((r.replace("_", "[ _]") for r in ROUTES), key=len, reverse=True))
_ROUTE_VERB = r"(?:is |are |be |gets? |goes? |go )?(?:routed|route|sent|send|escalated|assigned|placed|directed|forwarded|go|goes)"
_ROUTE_RE = re.compile(
    rf"^(?P<cond>.*?)[,;]?\s*(?:then\s+)?(?:(?:it|they|the ticket|the tickets|such tickets|these tickets)\s+)?"
    rf"{_ROUTE_VERB}\s+(?:it\s+|them\s+)?(?:to|into)\s+(?:(?:the|a|an|any)\s+)?(?P<route>{_ROUTE_ALT})(?:\s+(?:queue|team))?\s*$",
    re.I,
)
_DEFAULT_RE = re.compile(
    rf"^(?:all\s+(?:remaining|other)|every\s+other|any\s+other|all\s+other|remaining)\s+tickets?\s*(?:are\s+|is\s+|will be\s+|should be\s+)?"
    rf"{_ROUTE_VERB}\s+(?:to|into)\s+(?:(?:the|a|an|any)\s+)?(?P<route>{_ROUTE_ALT})\s*$",
    re.I,
)
_NUM = r"(?P<n>\d+(?:\.\d+)?)\s*(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)"
_UNIT_MAP = {"sec": "seconds", "second": "seconds", "min": "minutes", "minute": "minutes", "hr": "hours", "hour": "hours", "day": "days"}
_WAIT_SUBJ = r"(?:(?:the|a|an|any)\s+)?(?:ticket(?:'s)?\s+)?(?:waiting\s+time|wait(?:ing)?\s+time|time\s+waiting|wait)"
_ATOMS: list[tuple[re.Pattern, Any]] = []


def _unit(u: str) -> str:
    u = u.lower().rstrip("s") if u.lower() not in ("secs", "mins", "hrs") else u.lower()[:-1]
    return _UNIT_MAP.get(u, u + "s" if not u.endswith("s") else u)


def _cmp_atom(op: str):
    def mk(m):
        return {"op": "cmp", "field": "waiting_minutes", "cmp": op, "value": float(m["n"]) if "." in m["n"] else int(m["n"]), "unit": _unit(m["u"])}
    return mk


_PHRASES = [  # order matters (more specific first)
    (r"(?:(?:the|a|an|any)\s+)?(?:ticket\s+|request\s+)?(?:category\s+is\s+(?:missing|absent|empty|not\s+(?:set|provided))|has\s+no\s+category|lacks\s+a\s+category|"
     r"is\s+missing\s+a\s+category|(?:has|have)\s+a\s+missing\s+category|(?:with(?:out)?|without)\s+a\s+category|no\s+category\s+is\s+(?:given|provided|set))",
     lambda m: {"op": "missing", "field": "category"}),
    (r"(?:(?:the|a|an|any)\s+)?(?:ticket|request)\s+(?:is\s+)?(?:not\s+urgent|non-urgent)", lambda m: {"op": "not", "arg": {"op": "eq", "field": "urgent", "value": True}}),
    (r"(?:(?:the|a|an|any)\s+)?(?:ticket|request)\s+(?:is\s+)?(?:marked\s+as\s+urgent|marked\s+urgent|urgent|flagged\s+urgent|flagged\s+as\s+urgent)",
     lambda m: {"op": "eq", "field": "urgent", "value": True}),
    (rf"{_WAIT_SUBJ}\s+(?:is|has\s+been|reaches)\s+(?:at\s+least|no\s+less\s+than|>=)\s+{_NUM}", _cmp_atom(">=")),
    (rf"(?:(?:the|a|an|any)\s+)?ticket\s+has\s+(?:been\s+waiting|waited)\s+(?:for\s+)?(?:at\s+least|no\s+less\s+than)\s+{_NUM}", _cmp_atom(">=")),
    (rf"{_WAIT_SUBJ}\s+(?:is|has\s+been|reaches)\s+(?:more\s+than|over|greater\s+than|exceeds|>)\s+{_NUM}", _cmp_atom(">")),
    (rf"(?:(?:the|a|an|any)\s+)?ticket\s+has\s+(?:been\s+waiting|waited)\s+(?:for\s+)?(?:more\s+than|over)\s+{_NUM}", _cmp_atom(">")),
    (rf"{_WAIT_SUBJ}\s+(?:is|has\s+been)\s+(?:less\s+than|under|below|<)\s+{_NUM}", _cmp_atom("<")),
    (rf"{_WAIT_SUBJ}\s+(?:is|has\s+been)\s+(?:at\s+most|no\s+more\s+than|<=)\s+{_NUM}", _cmp_atom("<=")),
    (rf"{_WAIT_SUBJ}\s+(?:is|equals|is\s+exactly)\s+(?:exactly\s+)?{_NUM}", _cmp_atom("==")),
    (r"(?:(?:the|a|an|any)\s+)?(?:customer\s+)?tier\s+is\s+not\s+(?P<v>[a-z0-9_\-]+)", lambda m: {"op": "not", "arg": {"op": "eq", "field": "tier", "value": m["v"].lower()}}),
    (r"(?:(?:the|a|an|any)\s+)?(?:customer\s+)?tier\s+is\s+(?P<v>[a-z0-9_\-]+)", lambda m: {"op": "eq", "field": "tier", "value": m["v"].lower()}),
    (r"(?:(?:the|a|an|any)\s+)?customer\s+is\s+(?:a\s+)?(?P<v>[a-z0-9_\-]+)\s+(?:customer|tier)", lambda m: {"op": "eq", "field": "tier", "value": m["v"].lower()}),
    (r"(?:(?:the|a|an|any)\s+)?category\s+is\s+(?P<v>[a-z0-9_\-]+)", lambda m: {"op": "eq", "field": "category", "value": m["v"].lower()}),
]
_ATOM_RES = [(re.compile(rf"^(?:{p})$", re.I), f) for p, f in _PHRASES]


def _parse_atom(text: str) -> dict:
    s = text.strip().strip(",")
    s = re.sub(r"^(?:if|when|whenever)\s+", "", s, flags=re.I)
    for rx, fn in _ATOM_RES:
        m = rx.match(s)
        if m:
            return fn(m)
    raise Unsupported(f"unrecognised condition phrase: {s!r}")


def _tokenize(s: str) -> list[str]:
    s = s.replace("(", " ( ").replace(")", " ) ")
    parts = re.split(r"\s+(and|or)\s+|(\(|\))", s)
    toks, buf = [], []
    # re.split with two groups yields None entries
    for p in parts:
        if p is None or p.strip() == "":
            continue
        p = p.strip()
        if p.lower() in ("and", "or") or p in "()":
            if buf:
                toks.append(" ".join(buf))
                buf = []
            toks.append(p.lower())
        else:
            buf.append(p)
    if buf:
        toks.append(" ".join(buf))
    return toks


def parse_condition_text(s: str) -> dict:
    toks = _tokenize(s)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def expr():
        nonlocal pos
        terms = [term()]
        while peek() == "or":
            pos += 1
            terms.append(term())
        return terms[0] if len(terms) == 1 else {"op": "or", "args": terms}

    def term():
        nonlocal pos
        fs = [factor()]
        while peek() == "and":
            pos += 1
            fs.append(factor())
        return fs[0] if len(fs) == 1 else {"op": "and", "args": fs}

    def factor():
        nonlocal pos
        t = peek()
        if t is None:
            raise Unsupported("dangling connective in condition")
        if t == "(":
            pos += 1
            e = expr()
            if peek() != ")":
                raise Unsupported("unbalanced parentheses")
            pos += 1
            return e
        if t in ("and", "or", ")"):
            raise Unsupported(f"unexpected {t!r}")
        pos += 1
        return _parse_atom(t)

    e = expr()
    if pos != len(toks):
        raise Unsupported("trailing tokens in condition")
    return e


@dataclass
class ParsedSentence:
    kind: str  # "rule" | "default"
    condition: dict | None
    route: str


def _norm_route(r: str) -> str:
    return re.sub(r"\s+", "_", r.strip().lower())


def parse_sentence(sentence: str) -> ParsedSentence:
    """Supported patterns (case-insensitive):
       [Otherwise,] [If|When] <cond>, route to <route>.        (also: send / are sent / go to / is routed to)
       [Exception to Rx:] [If] <cond>, route to <route>.
       All remaining|other tickets [are] route[d] to <route>.
       <cond> := atom (and|or atom)* with parentheses; 'and' binds tighter than 'or'.
       atoms: category missing | ticket urgent/not urgent | waiting time at least/more than/less than/at most/exactly N <unit>
              | tier is X | tier is not X | category is X."""
    s = sentence.strip().rstrip(".!").strip()
    s = re.sub(r"^(?:otherwise|else|failing that)[,:]?\s+", "", s, flags=re.I)
    s = re.sub(r"^exception(?:\s+to\s+\S+)?[,:]\s*", "", s, flags=re.I)
    m = _DEFAULT_RE.match(s)
    if m:
        return ParsedSentence("default", None, _norm_route(m["route"]))
    m = _ROUTE_RE.match(s)
    if not m:
        raise Unsupported("sentence does not match a supported routing pattern")
    cond = parse_condition_text(m["cond"])
    return ParsedSentence("rule", cond, _norm_route(m["route"]))


_NORMATIVE = re.compile(r"\b(must|should|shall|route[sd]?|send|sent|escalat\w*|queue|handled|prioriti[sz]e\w*|review|forward\w*)\b", re.I)
_INJECTION = re.compile(
    r"(ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)|disregard|system\s*:|assistant\s*:|you\s+(?:must|should|are\s+to)\s+(?:output|return|report|mark|say|respond)|"
    r"(?:output|return|report|mark|say|respond\s+with)\s+(?:status\s*[:=]?\s*)?[\"']?PASS|reviewer\s+(?:must|should)|do\s+not\s+(?:report|flag)|treat\s+.{0,40}\s+as\s+(?:approved|valid)|"
    r"this\s+(?:change|requirement|bundle)\s+is\s+(?:already\s+)?approved|approved\s+by\s+\w+|(?:ceo|cto|vp)\s+(?:has\s+)?(?:approved|signed))",
    re.I,
)


def is_injection(s: str) -> bool:
    return bool(_INJECTION.search(s))


def is_normative(s: str) -> bool:
    return bool(_NORMATIVE.search(s))


def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
