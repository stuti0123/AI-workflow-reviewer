"""Stage 1: derive effective requirements from anchored source requirements + top-level approval records.

Contracts
  * Only top-level `approvals[]` records carry approval authority. Prose such as "approved by the CTO" inside a
    requirement is data: it is reported (`ignored_prose_approval`) and never applied.
  * A change applies to requirement R iff: status == "approved", target == R.id and applies_to_version == the
    requirements document version.  Anything else is listed in `notes` (unapproved / out_of_scope / dangling).
  * Ordering: applicable changes are applied in ascending `seq`; a later change sees the text produced by earlier ones.
  * Contradictions are never resolved silently:  (a) two applicable approved changes to the same target with the same
    `seq` and different outcome, or (b) an approved record and a `rejected` record carrying an identical change for the
    same target and version.  Each contradiction yields alternative *candidates*; the reviewer evaluates all of them.
"""
from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field

from .lang import (Policy, Rule, Unsupported, is_injection, is_normative, normalize, parse_sentence, split_sentences)

MAX_CANDIDATES = 8


@dataclass
class EffReq:
    id: str
    order: int
    anchor: str
    kind: str  # rule | exception | default
    parent: str | None
    original_text: str
    effective_text: str
    applied_changes: list[str]
    status: str  # reviewed | unsupported | ambiguous | partial
    reasons: list[str] = field(default_factory=list)
    condition: dict | None = None  # normalised
    route: str | None = None
    injections: list[str] = field(default_factory=list)
    prose_approval_claims: list[str] = field(default_factory=list)
    non_normative: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    label: str
    reqs: list[EffReq]
    choices: dict[str, str] = field(default_factory=dict)

    def policy(self) -> Policy:
        rules = []
        by_id = {}
        for r in sorted((r for r in self.reqs if r.kind == "rule" and r.condition is not None), key=lambda r: r.order):
            rule = Rule(id=r.id, order=r.order, condition=r.condition, route=r.route, location=r.anchor)
            rules.append(rule)
            by_id[r.id] = rule
        for r in sorted((r for r in self.reqs if r.kind == "exception" and r.condition is not None), key=lambda r: r.order):
            if r.parent in by_id:
                by_id[r.parent].exceptions.append((r.id, r.condition, r.route))
        d = next((r.route for r in self.reqs if r.kind == "default" and r.route), None)
        return Policy(rules, d)

    @property
    def complete(self) -> bool:
        return all(r.status == "reviewed" for r in self.reqs)


def _apply_change(text: str, change: dict) -> str:
    if "text" in change:
        return change["text"]
    rep = change.get("replace")
    if rep:
        if text.count(rep["from"]) != 1:
            raise Unsupported(f"change 'replace.from' {rep['from']!r} occurs {text.count(rep['from'])} times (need exactly 1)", "ambiguous")
        return text.replace(rep["from"], rep["to"])
    raise Unsupported("change has neither 'text' nor 'replace'", "ambiguous")


def parse_requirement(req: dict, text: str, applied: list[str]) -> EffReq:
    kind_decl = req.get("kind")
    e = EffReq(id=req["id"], order=req["order"], anchor=req.get("anchor", ""), kind=kind_decl or "rule", parent=req.get("parent"),
               original_text=req["text"], effective_text=text, applied_changes=list(applied), status="unsupported")
    parsed = []
    unsupported = []
    for s in split_sentences(text):
        if is_injection(s):
            (e.prose_approval_claims if "approv" in s.lower() or "signed" in s.lower() else e.injections).append(s)
            continue
        try:
            parsed.append((s, parse_sentence(s)))
        except Unsupported as ex:
            if is_normative(s):
                unsupported.append((s, ex.reason))
            else:
                e.non_normative.append(s)
    if len(parsed) > 1:
        e.status = "ambiguous"
        e.reasons.append("requirement text contains more than one routing statement")
        return e
    if not parsed:
        e.reasons += [f"{s!r}: {r}" for s, r in unsupported] or ["no routing statement found"]
        return e
    s, p = parsed[0]
    try:
        e.condition = normalize(p.condition) if p.condition is not None else {"op": "true"}
    except Unsupported as ex:
        e.status = ex.kind
        e.reasons.append(ex.reason)
        return e
    e.route = p.route
    if p.kind == "default":
        e.kind = "default"
    elif re.match(r"^\s*exception", s, re.I) or kind_decl == "exception":
        e.kind = "exception"
    e.status = "reviewed"
    if unsupported:
        e.status = "partial"
        e.reasons += [f"{s!r}: {r}" for s, r in unsupported]
    if e.kind == "exception" and not e.parent:
        e.status = "ambiguous"
        e.reasons.append("exception requirement lacks 'parent'")
    return e


import re  # noqa: E402  (used above; kept near use for readability)


def derive_effective(doc: dict, approvals: list[dict]):
    """Returns (candidates, notes). notes: list of dicts {type,severity,message,approval?,target?}."""
    ver = doc["version"]
    reqs = sorted(doc["requirements"], key=lambda r: r["order"])
    ids = {r["id"] for r in reqs}
    notes: list[dict] = []
    seen_ids: dict[str, dict] = {}
    for a in approvals:
        if a["id"] in seen_ids and seen_ids[a["id"]] != a:
            notes.append({"type": "approval_conflict", "severity": "major", "approval": a["id"],
                          "message": f"approval id {a['id']} is used by two different records"})
        seen_ids[a["id"]] = a
        if a["target"] not in ids:
            notes.append({"type": "dangling_approval", "severity": "minor", "approval": a["id"], "target": a["target"],
                          "message": f"approval {a['id']} targets unknown requirement {a['target']}"})
        if a["status"] != "approved":
            notes.append({"type": "unapproved_change_ignored", "severity": "info", "approval": a["id"], "target": a["target"],
                          "message": f"{a['id']} has status '{a['status']}' and is not applied"})
        elif a["applies_to_version"] != ver:
            notes.append({"type": "out_of_scope_change_ignored", "severity": "info", "approval": a["id"], "target": a["target"],
                          "message": f"{a['id']} applies to version {a['applies_to_version']}, document is {ver}"})

    # per requirement: list of alternative (label, [changes]) options
    per_req_options: dict[str, list[tuple[str, list[dict]]]] = {}
    for r in reqs:
        app = [a for a in approvals if a["target"] == r["id"] and a["status"] == "approved" and a["applies_to_version"] == ver]
        rej = [a for a in approvals if a["target"] == r["id"] and a["status"] == "rejected" and a["applies_to_version"] == ver]
        # (b) approved vs rejected with identical change -> contradiction on that change
        contradicted = set()
        for a in app:
            for j in rej:
                if json.dumps(a["change"], sort_keys=True) == json.dumps(j["change"], sort_keys=True):
                    contradicted.add(a["id"])
                    notes.append({"type": "approval_conflict", "severity": "major", "approval": a["id"], "target": r["id"],
                                  "message": f"{a['id']} (approved) and {j['id']} (rejected) carry the same change for {r['id']}"})
        groups: dict[int, list[dict]] = {}
        for a in app:
            groups.setdefault(a["seq"], []).append(a)
        seq_options: list[list[tuple[str, dict | None]]] = []
        for seq in sorted(groups):
            g = groups[seq]
            texts = {json.dumps(a["change"], sort_keys=True) for a in g}
            if len(g) > 1 and len(texts) > 1:
                notes.append({"type": "approval_conflict", "severity": "major", "target": r["id"],
                              "approval": ",".join(a["id"] for a in g),
                              "message": f"approved records {', '.join(a['id'] for a in g)} change {r['id']} differently at the same seq={seq}"})
                seq_options.append([(a["id"], a) for a in g])
            else:
                a = g[0]
                if a["id"] in contradicted:
                    seq_options.append([(a["id"] + "+applied", a), (a["id"] + "+not_applied", None)])
                else:
                    seq_options.append([(a["id"], a)])
        opts = []
        for combo in itertools.product(*seq_options) if seq_options else [()]:
            label = ",".join(c[0] for c in combo) or "no_change"
            opts.append((label, [c[1] for c in combo if c[1] is not None]))
        per_req_options[r["id"]] = opts

    keys = [r["id"] for r in reqs]
    combos = list(itertools.product(*[range(len(per_req_options[k])) for k in keys]))
    if len(combos) > MAX_CANDIDATES:
        notes.append({"type": "too_many_candidates", "severity": "major",
                      "message": f"{len(combos)} alternative effective-requirement sets; evaluating first {MAX_CANDIDATES} only"})
        combos = combos[:MAX_CANDIDATES]
    cands = []
    for combo in combos:
        out, label_parts, choices = [], [], {}
        for k, idx in zip(keys, combo):
            req = next(r for r in reqs if r["id"] == k)
            label, changes = per_req_options[k][idx]
            text = req["text"]
            applied = []
            try:
                for ch in sorted(changes, key=lambda c: c["seq"]):
                    text = _apply_change(text, ch["change"])
                    applied.append(ch["id"])
                e = parse_requirement(req, text, applied)
            except Unsupported as ex:
                e = EffReq(id=k, order=req["order"], anchor=req.get("anchor", ""), kind=req.get("kind", "rule"), parent=req.get("parent"),
                           original_text=req["text"], effective_text=text, applied_changes=applied, status=ex.kind, reasons=[ex.reason])
            out.append(e)
            if label != "no_change":
                label_parts.append(f"{k}:{label}")
                choices[k] = label
        cands.append(Candidate(label=";".join(label_parts) or "baseline", reqs=out, choices=choices))
    # prose approval claims / injections are data: surface once
    for e in cands[0].reqs:
        for s in e.prose_approval_claims:
            notes.append({"type": "ignored_prose_approval", "severity": "info", "target": e.id,
                          "message": f"requirement {e.id} contains an approval claim in prose, which has no authority: {s!r}"})
        for s in e.injections:
            notes.append({"type": "ignored_injected_instruction", "severity": "info", "target": e.id,
                          "message": f"requirement {e.id} contains instruction-like text, treated as data: {s!r}"})
    return cands, notes


def original_candidate(doc: dict) -> Candidate:
    """Requirements with no approved change applied (used to explain *why* an artifact is wrong)."""
    reqs = sorted(doc["requirements"], key=lambda r: r["order"])
    return Candidate("original", [parse_requirement(r, r["text"], []) for r in reqs])
