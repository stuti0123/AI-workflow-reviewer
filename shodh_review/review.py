"""Deterministic reviewer (Stages 1-2). Compares effective requirements with specification, workflow and linked code
by *exhaustive evaluation over representative tickets* of the declared language (see lang.build_grid)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from pathlib import Path

import jsonschema

from . import pycode
from .effective import Candidate, derive_effective, original_candidate
from .lang import (FIELDS, ROUTES, Policy, Rule, Unsupported, atoms, build_grid, cond_text, eval_cond, is_injection,
                   literals_of, normalize, num_out, route_policy, ticket_out)

REVIEWER_VERSION = "1.0.0"
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "bundle.schema.json").read_text())
SEV_RANK = {"blocker": 0, "major": 1, "minor": 2, "info": 3}


@dataclass
class Config:
    include_boundaries: bool = True      # ablation switch: threshold points in the exhaustive grid
    max_grid: int = 200_000
    max_requirements: int = 200
    max_snippet_chars: int = 20_000
    max_nodes: int = 500
    max_prompt_chars: int = 40_000
    max_model_attempts: int = 2          # total calls per review = 1 + retries; hard bound
    validate_model_claims: bool = True   # ablation switch


# ----------------------------------------------------------------------------- helpers
def finding(sev, type_, message, *, rule_ref=None, artifact=None, version=None, location=None, evidence=None,
            correction=None, counterexample=None, confidence="exhaustive", source="deterministic", **extra):
    f = {"severity": sev, "type": type_, "rule_ref": rule_ref, "artifact": artifact, "version": version, "location": location,
         "message": message, "evidence": evidence or [], "correction": correction, "counterexample": counterexample,
         "confidence": confidence, "source": source}
    f.update(extra)
    return f


def _cx(ticket, exp, exp_fired, act, act_where, artifact):
    return {"ticket": ticket_out(ticket), "expected_route": exp, "expected_by": exp_fired, "actual_route": act,
            "actual_by": act_where, "artifact": artifact}


def _simplicity(t):
    nondefault = sum(1 for k, v in t.items() if v not in (None, False, 0, Fraction(0)) and v != "__other__")
    return (nondefault, t["waiting_minutes"])


def _spec_policy(spec: dict):
    """Returns (Policy | None, issues[list of (rule_id, reason, kind)])."""
    rules, issues = [], []
    for i, r in enumerate(spec["rules"]):
        try:
            cond = normalize(r["condition"])
            ex = [(x.get("id", f"{r['id']}.x{j}"), normalize(x["condition"]), x["route"]) for j, x in enumerate(r.get("exceptions", []))]
        except Unsupported as e:
            issues.append((r["id"], e.reason, e.kind))
            continue
        rules.append(Rule(id=r["id"], order=r["order"], condition=cond, route=r["route"], exceptions=ex, ref=r.get("ref"),
                          location=r.get("location") or f"specification.rules[{i}]"))
    return (Policy(rules, spec.get("default_route")) if not issues else None), issues


def _walk_workflow(wf_nodes: dict, entry: str, t: dict):
    node_id, path = entry, []
    for _ in range(len(wf_nodes) + 2):
        n = wf_nodes.get(node_id)
        if n is None:
            return f"<dangling:{node_id}>", path
        path.append(node_id)
        if n["type"] == "terminal":
            return n.get("route") or "<no_route>", path
        branch = "on_true" if eval_cond(n["_cond"], t) else "on_false"
        nxt = n.get(branch)
        if not nxt:
            return f"<missing_branch:{branch}@{node_id}>", path
        node_id = nxt
    return "<cycle>", path


def _workflow_structure(wf: dict):
    """Static structural findings + normalised node map (nodes with unsupported conditions are reported)."""
    out, nodes, bad = [], {}, []
    ids = [n["id"] for n in wf["nodes"]]
    for i in {x for x in ids if ids.count(x) > 1}:
        out.append(finding("major", "duplicate_node_id", f"node id {i} is defined more than once", artifact="workflow",
                           version=wf["version"], location=wf.get("location")))
    for idx, n in enumerate(wf["nodes"]):
        n = dict(n)
        n["_loc"] = n.get("location") or f"workflow.nodes[{idx}]"
        if n["type"] == "decision":
            try:
                n["_cond"] = normalize(n["condition"]) if "condition" in n else None
                if n["_cond"] is None:
                    raise Unsupported("decision node has no condition")
            except Unsupported as e:
                bad.append((n["id"], e.reason, e.kind))
        nodes[n["id"]] = n
    if wf["entry"] not in nodes:
        out.append(finding("blocker", "invalid_workflow", f"entry node {wf['entry']} does not exist", artifact="workflow",
                           version=wf["version"], location=wf.get("location")))
    reach, stack = set(), [wf["entry"]]
    while stack:
        x = stack.pop()
        if x in reach or x not in nodes:
            continue
        reach.add(x)
        stack += [nodes[x].get("on_true"), nodes[x].get("on_false")] if nodes[x]["type"] == "decision" else []
        stack = [s for s in stack if s]
    for nid, n in nodes.items():
        if nid not in reach:
            out.append(finding("minor", "unreachable_node", f"node {nid} is unreachable from entry", artifact="workflow",
                               version=wf["version"], location=n["_loc"], evidence=[{"artifact": "workflow", "location": n["_loc"], "quote": json.dumps({k: v for k, v in n.items() if not k.startswith('_')}, sort_keys=True)}]))
        if n["type"] == "decision":
            for b in ("on_true", "on_false"):
                if not n.get(b):
                    out.append(finding("info", "missing_branch_declared", f"decision node {nid} has no {b} branch", artifact="workflow",
                                       version=wf["version"], location=n["_loc"]))
                elif n[b] not in nodes:
                    out.append(finding("major", "dangling_branch", f"node {nid}.{b} points to unknown node {n[b]}", artifact="workflow",
                                       version=wf["version"], location=n["_loc"]))
    return out, nodes, bad


def _quote_spec_rule(spec, rid):
    for r in spec["rules"]:
        if r["id"] == rid:
            return json.dumps(r, sort_keys=True)
    return None


def _relax(c: dict) -> dict:
    """Replace numeric comparisons by 'true' (used to ask: does ticket satisfy R apart from its thresholds?)."""
    op = c["op"]
    if op == "cmp":
        return {"op": "true"}
    if op in ("and", "or"):
        return {"op": op, "args": [_relax(a) for a in c["args"]]}
    if op == "not":
        return {"op": "not", "arg": _relax(c["arg"])}
    return c


def _has_cmp(c: dict) -> bool:
    return any(a[1] in ("<", "<=", ">", ">=", "==") for a in atoms(c))


def _equiv(c1: dict, c2: dict, grid) -> bool:
    return all(eval_cond(c1, t) == eval_cond(c2, t) for t in grid)


def _diagnose_spec(cand: Candidate, spec_pol: Policy, grid):
    """Rule-level explanation of spec/requirement differences. Returns {req_id: (type, message, spec_rule_or_None)}."""
    diag = {}
    eff = [r for r in cand.reqs if r.kind == "rule" and r.condition is not None]
    srules = sorted(spec_pol.rules, key=lambda r: r.order)
    matched = {}
    for r in eff:
        # semantic match: equivalent condition & route & no exceptions difference
        sem = [s for s in srules if s.route == r.route and _equiv(s.condition, r.condition, grid)]
        ref = [s for s in srules if s.ref == r.id]
        if sem:
            matched[r.id] = sem[0]
            continue
        same_route = [s for s in srules if s.route == r.route and {a[0] for a in atoms(s.condition)} & {a[0] for a in atoms(r.condition)}]
        cand_rules = ref or same_route
        if not cand_rules:
            diag[r.id] = ("omitted_requirement", f"no specification rule implements requirement {r.id} ({cond_text(r.condition)} -> {r.route})", None)
            continue
        s = cand_rules[0]
        matched[r.id] = s
        if s.route != r.route:
            diag[r.id] = ("wrong_destination", f"spec rule {s.id} routes to {s.route}, requirement {r.id} requires {r.route}", s)
            continue
        ra, sa = [a for a in atoms(r.condition) if a[1] in ("<", "<=", ">", ">=", "==")], [a for a in atoms(s.condition) if a[1] in ("<", "<=", ">", ">=", "==")]
        if len(ra) == 1 and len(sa) == 1 and ra[0][0] == sa[0][0]:
            (f, o1, v1), (_, o2, v2) = ra[0], sa[0]
            if o1 != o2 and v1 == v2:
                diag[r.id] = ("boundary_mismatch", f"spec rule {s.id} uses '{o2}' where requirement {r.id} uses '{o1}' at {num_out(v1)} minutes", s)
            elif v1 != v2 and v1 and (v2 / v1 in (60, 1440, Fraction(1, 60), Fraction(1, 1440))):
                diag[r.id] = ("unit_mismatch", f"spec rule {s.id} threshold {num_out(v2)} min differs from requirement {r.id} {num_out(v1)} min by a unit factor", s)
            elif v1 != v2:
                diag[r.id] = ("threshold_mismatch", f"spec rule {s.id} uses threshold {num_out(v2)} min, effective requirement {r.id} uses {num_out(v1)} min", s)
            else:
                diag[r.id] = ("condition_mismatch", f"spec rule {s.id} condition differs from requirement {r.id}", s)
        else:
            diag[r.id] = ("condition_mismatch", f"spec rule {s.id} condition ({cond_text(s.condition)}) differs from requirement {r.id} ({cond_text(r.condition)})", s)
    # precedence: matched pair inverted and overlapping with different routes
    for (ra, rb) in combinations(eff, 2):
        if ra.id in matched and rb.id in matched and ra.order < rb.order:
            sa, sb = matched[ra.id], matched[rb.id]
            if sa.order > sb.order and ra.route != rb.route and any(eval_cond(ra.condition, t) and eval_cond(rb.condition, t) for t in grid):
                diag.setdefault(ra.id, ("precedence_error", f"spec places {sb.id} (for {rb.id}) before {sa.id} (for {ra.id}); requirements give {ra.id} precedence", sa))
    return diag, matched


def _alt_explanation(name, actual_fn, alt_cands, grid, base_label):
    """Does the artifact agree everywhere with a *different* reading (pre-approval text / unapproved suggestion)?"""
    for kind, cand in alt_cands:
        pol = cand.policy()
        if cand.complete and all(actual_fn(t)[0] == route_policy(pol, t)[0] for t in grid):
            return kind
    return None


def _schema_errors(bundle):
    v = jsonschema.Draft202012Validator(SCHEMA)
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in sorted(v.iter_errors(bundle), key=lambda e: list(map(str, e.absolute_path)))][:20]


# ----------------------------------------------------------------------------- main
def _review_core(bundle: dict, cfg: Config) -> dict:
    t0 = time.perf_counter()
    checks, findings, unreviewed = [], [], []
    report = {"reviewer_version": REVIEWER_VERSION, "bundle_id": bundle.get("bundle_id") if isinstance(bundle, dict) else None}

    errs = _schema_errors(bundle)
    checks.append({"name": "schema_validation", "kind": "static", "status": "failed" if errs else "passed", "required": True, "details": errs})
    if errs:
        findings.append(finding("major", "invalid_input", "bundle does not satisfy the input contract: " + "; ".join(errs[:3]),
                                confidence="static"))
        unreviewed.append("entire bundle (invalid input)")
        return _finish(report, "INCOMPLETE", findings, checks, unreviewed, {}, t0, bundle, "Input contract violated; nothing could be reviewed.")

    doc, spec, wf, snippets = bundle["requirements_doc"], bundle["specification"], bundle["workflow"], bundle["snippets"]
    # size limits: refuse, never truncate silently
    over = []
    if len(doc["requirements"]) > cfg.max_requirements:
        over.append(f"{len(doc['requirements'])} requirements > limit {cfg.max_requirements}")
    if len(wf["nodes"]) > cfg.max_nodes:
        over.append(f"{len(wf['nodes'])} workflow nodes > limit {cfg.max_nodes}")
    for s in snippets:
        if len(s["source"]) > cfg.max_snippet_chars:
            over.append(f"snippet {s['id']} has {len(s['source'])} chars > limit {cfg.max_snippet_chars}")
    checks.append({"name": "input_size_limits", "kind": "static", "status": "failed" if over else "passed", "required": True, "details": over})
    if over:
        findings.append(finding("major", "input_too_large", "input exceeds configured limits and was NOT truncated: " + "; ".join(over), confidence="static"))
        unreviewed.append("entire bundle (oversized input)")
        return _finish(report, "INCOMPLETE", findings, checks, unreviewed, {}, t0, bundle, "Input exceeds limits; nothing was silently dropped, nothing was reviewed.")

    # ---- Stage 1: effective requirements
    cands, notes = derive_effective(doc, bundle["approvals"])
    for n in notes:
        findings.append(finding(n["severity"], n["type"], n["message"], rule_ref=n.get("target"), artifact="approvals",
                                evidence=[{"artifact": "approvals", "location": f"approvals[{n.get('approval')}]", "quote": n["message"]}] if n.get("approval") else [],
                                confidence="static"))
    conflict = len(cands) > 1
    checks.append({"name": "approval_resolution", "kind": "static", "status": "ambiguous" if conflict else "passed", "required": True,
                   "details": [f"{len(cands)} candidate effective-requirement set(s)"] + [c.label for c in cands]})
    if conflict:
        unreviewed.append("effective requirement text is ambiguous because approval records contradict each other")

    # requirement parse coverage (same for all candidates except where a change touches text)
    cov_reqs = []
    for r in cands[0].reqs:
        bad_in = [c.label for c in cands if next(x for x in c.reqs if x.id == r.id).status != "reviewed"]
        status = r.status if not bad_in else next(next(x for x in c.reqs if x.id == r.id).status for c in cands if next(x for x in c.reqs if x.id == r.id).status != "reviewed")
        cov_reqs.append({"id": r.id, "anchor": r.anchor, "kind": r.kind, "status": "reviewed" if status == "reviewed" else status,
                         "effective_text": r.effective_text, "changes_applied": r.applied_changes, "reasons": r.reasons,
                         "non_normative_sentences": r.non_normative})
        if status != "reviewed":
            unreviewed.append(f"requirement {r.id}: {status} ({'; '.join(r.reasons) or 'see coverage'})")
            findings.append(finding("major", "unsupported_content" if status != "ambiguous" else "ambiguous_content",
                                    f"requirement {r.id} is outside the supported source language and was not checked: {'; '.join(r.reasons)}",
                                    rule_ref=r.id, artifact="requirements", version=doc["version"], location=r.anchor,
                                    evidence=[{"artifact": "requirements", "location": r.anchor, "quote": r.effective_text}],
                                    correction="Rewrite the requirement using a supported pattern or add an approved structured form.", confidence="static"))
    policy_complete = all(c.complete for c in cands)

    # ---- artifacts
    spec_pol, spec_issues = _spec_policy(spec)
    for rid, reason, kind in spec_issues:
        unreviewed.append(f"specification rule {rid}: {reason}")
        findings.append(finding("major", "ambiguous_unit" if "unit" in reason else "unsupported_content",
                                f"specification rule {rid} could not be interpreted: {reason}", rule_ref=next((r.get('ref') for r in spec['rules'] if r['id'] == rid), None),
                                artifact="specification", version=spec["version"], location=f"specification.rules[{rid}]",
                                evidence=[{"artifact": "specification", "location": f"specification.rules[{rid}]", "quote": _quote_spec_rule(spec, rid)}],
                                correction="Declare the unit explicitly (seconds/minutes/hours/days) or use supported operators.", confidence="static"))
    wf_static, wf_nodes, wf_bad = _workflow_structure(wf)
    findings += wf_static
    for nid, reason, kind in wf_bad:
        unreviewed.append(f"workflow node {nid}: {reason}")
        findings.append(finding("major", "ambiguous_unit" if "unit" in reason else "unsupported_content", f"workflow node {nid}: {reason}",
                                artifact="workflow", version=wf["version"], location=wf_nodes[nid]["_loc"], confidence="static"))
    wf_ok = not wf_bad and wf["entry"] in wf_nodes

    code = []  # [{snippet, fn, pname, status, reason}]
    for s in snippets:
        entry = {"snippet": s, "fn": None, "pname": None, "status": "ok", "reason": None}
        try:
            import ast as _ast
            _ast.parse(s["source"])
            checks.append({"name": f"code_syntax:{s['id']}", "kind": "syntax", "status": "passed", "required": True, "details": []})
        except SyntaxError as e:
            entry.update(status="syntax_error", reason=f"line {e.lineno}: {e.msg}")
            checks.append({"name": f"code_syntax:{s['id']}", "kind": "syntax", "status": "failed", "required": True, "details": [entry["reason"]]})
            findings.append(finding("major", "code_syntax_error", f"snippet {s['id']} does not parse: {entry['reason']}", artifact=f"code:{s['id']}",
                                    version=s["version"], location=s["location"], confidence="static"))
            code.append(entry)
            unreviewed.append(f"snippet {s['id']}: syntax error")
            continue
        try:
            fn, p = pycode.parse_snippet(s["source"], s.get("entry"))
            entry.update(fn=fn, pname=p)
            checks.append({"name": f"code_inspection:{s['id']}", "kind": "inspection", "status": "passed", "required": True, "details": ["within supported subset"]})
        except Unsupported as e:
            entry.update(status="unsupported", reason=e.reason)
            checks.append({"name": f"code_inspection:{s['id']}", "kind": "inspection", "status": "unsupported", "required": True, "details": [e.reason]})
            findings.append(finding("major", "unsupported_code", f"snippet {s['id']} is outside the supported Python subset and was not behaviourally checked: {e.reason}",
                                    artifact=f"code:{s['id']}", version=s["version"], location=s["location"],
                                    evidence=[{"artifact": f"code:{s['id']}", "location": s["location"], "quote": s["source"][:300]}], confidence="static"))
            unreviewed.append(f"snippet {s['id']}: {e.reason}")
        if any(is_injection(line.split('#', 1)[1]) for line in s["source"].splitlines() if '#' in line):
            findings.append(finding("info", "ignored_injected_instruction", f"snippet {s['id']} comments contain instruction-like text; treated as data",
                                    artifact=f"code:{s['id']}", version=s["version"], location=s["location"], confidence="static"))
        code.append(entry)

    # version consistency
    for name, art in (("specification", spec), ("workflow", wf)):
        if art["version"] != doc["version"]:
            findings.append(finding("major", "version_mismatch", f"{name} version {art['version']} differs from requirements version {doc['version']}",
                                    artifact=name, version=art["version"], confidence="static"))

    # ---- grid
    thr, lits = set(), {}
    allc = cands + [original_candidate(doc)]
    for c in allc:
        for r in c.reqs:
            if r.condition:
                thr |= {a[2] for a in atoms(r.condition) if a[1] in ("<", "<=", ">", ">=", "==")}
                literals_of(r.condition, lits)
    for r in (spec_pol.rules if spec_pol else []):
        for cc in [r.condition] + [x[1] for x in r.exceptions]:
            thr |= {a[2] for a in atoms(cc) if a[1] in ("<", "<=", ">", ">=", "==")}
            literals_of(cc, lits)
    for n in wf_nodes.values():
        if n.get("_cond"):
            thr |= {a[2] for a in atoms(n["_cond"]) if a[1] in ("<", "<=", ">", ">=", "==")}
            literals_of(n["_cond"], lits)
    for e in code:
        if e["fn"] is not None:
            nums, strs = pycode.constants_of(e["fn"])
            thr |= nums
            for k in strs:
                lits.setdefault(k, set()).update(strs[k])
    grid, gmeta = build_grid(thr, lits, cfg.include_boundaries, cfg.max_grid)
    if gmeta.get("over_cap"):
        findings.append(finding("major", "input_too_large", f"exhaustive grid would need {gmeta['grid_size']} tickets (> {cfg.max_grid}); not truncated, not sampled", confidence="static"))
        unreviewed.append("behavioural equivalence (grid over cap)")
        return _finish(report, "INCOMPLETE", findings, checks, unreviewed, {}, t0, bundle, "Behavioural domain too large for exhaustive review.")
    method = (f"Exhaustive over {len(grid)} representative tickets: every combination of category/tier literal classes, urgent, and "
              f"waiting_minutes in {{each threshold, each open interval between thresholds}}. All predicates of the declared language are "
              f"constant inside each class, so agreement on the grid = agreement on the whole domain (within the language).")

    # ---- Stage 2: behavioural comparison per candidate
    per_cand = []  # list of dict(artifact -> list[finding])
    for ci, cand in enumerate(cands):
        exp_pol = cand.policy()
        alts = []
        if any(r.applied_changes for r in cand.reqs):
            alts.append(("approved_change_not_implemented", original_candidate(doc)))
        dm = json.loads(json.dumps(bundle["approvals"]))
        for a in dm:
            if a["status"] in ("suggested", "pending") and a["applies_to_version"] == doc["version"]:
                a["status"] = "approved"
        if any(a["status"] == "approved" for a in dm) and dm != bundle["approvals"]:
            alts.append(("unapproved_change_applied", derive_effective(doc, dm)[0][0]))
        fnd: dict[str, list] = {}
        unrev = [r for r in cand.reqs if r.status != "reviewed"]
        by_id = {r.id: r for r in cand.reqs}

        def trusted(ef: str) -> bool:
            """May a counterexample whose expected route comes from `ef` be called a blocker? Only if no requirement
            we could not interpret is able to pre-empt that outcome (earlier precedence, or an exception to it)."""
            if not unrev:
                return True
            if ef == "default" or ef not in by_id:
                return False
            e = by_id[ef]
            base = by_id.get(e.parent, e) if e.kind == "exception" else e
            return all(u.order > base.order and u.parent != base.id for u in unrev)

        def compare(artifact, version, actual_fn, where_loc, ev_fn, diag=None):
            dis = {}
            for t in grid:
                er, ef = route_policy(exp_pol, t)
                ar, aw = actual_fn(t)
                if er != ar:
                    key = ef
                    if ef == "default":  # a threshold drifting into the default region: attribute to the rule whose outcome fires
                        hits = [r for r in cand.reqs if r.kind == "rule" and r.condition is not None and r.route == ar
                                and _has_cmp(r.condition) and eval_cond(_relax(r.condition), t)]
                        if len(hits) == 1:
                            key = hits[0].id
                    dis.setdefault(key, []).append((t, er, ef, ar, aw))
            out = []
            alt = _alt_explanation(artifact, lambda t: actual_fn(t), alts, grid, cand.label) if dis else None
            for key, lst in sorted(dis.items()):
                t, er, ef, ar, aw = min(lst, key=lambda x: _simplicity(x[0]))
                req = next((r for r in cand.reqs if r.id == key), None)
                typ, msg, corr = "behavior_mismatch", None, None
                if diag and key in diag:
                    typ, msg, _s = diag[key]
                if ar.startswith("<missing_branch"):
                    typ = "missing_branch"
                if typ == "behavior_mismatch" and artifact.startswith("code"):
                    typ = "script_disagreement"
                if key == "default" and not (diag and key in diag):
                    typ = "unrequested_behavior" if not ar.startswith("<") else typ
                if alt and typ in ("behavior_mismatch", "script_disagreement", "unrequested_behavior", "threshold_mismatch", "boundary_mismatch", "unit_mismatch", "condition_mismatch"):
                    typ = alt
                exp_txt = f"requirement {ef}" if ef != "default" else "the default rule, i.e. no requirement matches"
                base = f"{artifact}: for this ticket the effective requirements ({exp_txt}) give '{er}' but the artifact gives '{ar}'"
                msg = (msg + ". " if msg else "") + base
                if alt == "approved_change_not_implemented":
                    msg += "; the artifact matches the requirement text *before* the approved change"
                if alt == "unapproved_change_applied":
                    msg += "; the artifact matches an *unapproved* suggested change"
                if req is not None and req.condition is not None:
                    corr = f"Make {artifact} route tickets matching ({cond_text(req.condition)}) to '{req.route}' at the precedence given by requirement {key} (order {req.order})."
                else:
                    corr = f"Remove or constrain the behaviour that sends this ticket to '{ar}'; the effective requirements route it to '{er}'."
                loc, quote = ev_fn(aw)
                ev = [{"artifact": artifact, "location": loc, "quote": quote}]
                if req is not None:
                    ev.append({"artifact": "requirements", "location": req.anchor, "quote": req.effective_text})
                if ar == "<cycle>":
                    typ = "workflow_cycle"
                if not trusted(ef):
                    msg += " [not a blocker: a requirement that could not be interpreted may pre-empt this outcome]"
                out.append(finding("blocker" if trusted(ef) else "major", typ, msg, rule_ref=None if key == "default" else key,
                                   artifact=artifact, version=version, location=loc, evidence=ev, correction=corr,
                                   counterexample=_cx(t, er, ef, ar, aw if isinstance(aw, (str, int)) else str(aw), artifact),
                                   n_disagreeing_tickets=len(lst), candidate=cand.label))
            return out

        # specification
        if spec_pol is not None:
            diag, _m = _diagnose_spec(cand, spec_pol, grid)

            def spec_fn(t):
                r, f = route_policy(spec_pol, t)
                return r, f

            def spec_ev(w):
                if w == "default":
                    return "specification.default_route", f"default_route={spec.get('default_route')!r}"
                loc = next((r.location for r in spec_pol.rules if r.id == w or any(x[0] == w for x in r.exceptions)), "specification")
                return loc, _quote_spec_rule(spec, w) or json.dumps(next((r for r in spec["rules"] if any(x.get("id") == w for x in r.get("exceptions", []))), {}), sort_keys=True)
            fnd["specification"] = compare("specification", spec["version"], spec_fn, None, spec_ev, diag)
            # omitted requirement that is behaviourally invisible
            for rid, (typ, msg, _s) in diag.items():
                if typ == "omitted_requirement" and not any(f["rule_ref"] == rid for f in fnd["specification"]):
                    req = next(r for r in cand.reqs if r.id == rid)
                    fnd["specification"].append(finding("major", "omitted_requirement", msg + " (no ticket in the domain is affected: rule is shadowed/redundant)",
                                                        rule_ref=rid, artifact="specification", version=spec["version"], location="specification.rules",
                                                        evidence=[{"artifact": "requirements", "location": req.anchor, "quote": req.effective_text}],
                                                        correction=f"Add a rule for {rid} for traceability.", candidate=cand.label))
        # workflow
        if wf_ok:
            def wf_fn(t):
                r, path = _walk_workflow(wf_nodes, wf["entry"], t)
                return r, path[-1] if path else None

            def wf_ev(w):
                n = wf_nodes.get(w) if w else None
                loc = n["_loc"] if n else (wf.get("location") or "workflow")
                return loc, json.dumps({k: v for k, v in (n or {}).items() if not k.startswith("_")}, sort_keys=True)

            def wf_fn2(t):
                r, path = _walk_workflow(wf_nodes, wf["entry"], t)
                # evidence: last decision node on the path (where the wrong branch is taken) else terminal
                dec = [p for p in path if wf_nodes.get(p, {}).get("type") == "decision"]
                return r, (dec[-1] if dec and (r.startswith("<") or True) else (path[-1] if path else None))
            fnd["workflow"] = compare("workflow", wf["version"], wf_fn2, None, wf_ev)
        # code
        for e in code:
            if e["fn"] is None:
                continue
            s = e["snippet"]
            lines = s["source"].splitlines()

            def code_fn(t, e=e):
                r, ln = pycode.run_snippet(e["fn"], e["pname"], t)
                return r, ln

            def code_ev(ln, s=s, lines=lines):
                if ln is None:
                    return s["location"], lines[0] if lines else ""
                return f"{s['location']}:L{ln}", lines[ln - 1].strip() if ln - 1 < len(lines) else ""
            fnd[f"code:{s['id']}"] = compare(f"code:{s['id']}", s["version"], code_fn, None, code_ev)
        per_cand.append(fnd)

    # ---- merge candidates
    arts = set().union(*[set(f) for f in per_cand]) if per_cand else set()
    established = {a for a in arts if all(any(x["severity"] == "blocker" for x in f.get(a, [])) for f in per_cand)}
    for a in sorted(arts):
        base = per_cand[0].get(a, [])
        for x in base:
            if x["severity"] == "blocker" and a not in established:
                x["severity"] = "major"
                x["message"] += " [depends on which contradictory approval record is authoritative; not established]"
        findings += base
    if conflict:
        findings.append(finding("major", "approval_conflict_unresolved",
                                "contradictory approval records mean the effective requirement is ambiguous; all alternatives were evaluated and none was chosen silently",
                                artifact="approvals", evidence=[{"artifact": "approvals", "location": "approvals", "quote": c.label} for c in cands], confidence="static"))

    # ---- coverage / checks
    art_cov = {"specification": {"version": spec["version"], "status": "reviewed" if spec_pol is not None else "not_reviewed",
                                 "reason": None if spec_pol is not None else "; ".join(f"{i[0]}: {i[1]}" for i in spec_issues)},
               "workflow": {"version": wf["version"], "status": "reviewed" if wf_ok else "not_reviewed", "reason": None if wf_ok else "unsupported node conditions or invalid entry"}}
    for e in code:
        art_cov[f"code:{e['snippet']['id']}"] = {"version": e["snippet"]["version"], "status": "reviewed" if e["fn"] is not None else "not_reviewed", "reason": e["reason"]}
    for name, c in art_cov.items():
        checks.append({"name": f"behavioural_equivalence:{name}", "kind": "exhaustive" if c["status"] == "reviewed" else "skipped",
                       "status": "passed" if c["status"] == "reviewed" and not any(f["artifact"] == name and f["severity"] in ("blocker",) for f in findings) else
                       ("failed" if c["status"] == "reviewed" else "not_run"), "required": True,
                       "details": [method] if c["status"] == "reviewed" else [c["reason"] or "not reviewable"]})
    if wf_ok:
        pass
    for r in cov_reqs:
        r["checked_against"] = [n for n, c in art_cov.items() if c["status"] == "reviewed"] if r["status"] == "reviewed" else []

    blockers = [f for f in findings if f["severity"] == "blocker"]
    nr = [n for n, c in art_cov.items() if c["status"] != "reviewed"]
    for n in nr:
        unreviewed.append(f"artifact {n} could not be reviewed")
    unreviewed = list(dict.fromkeys(unreviewed))
    if blockers:
        status = "CHANGES_REQUIRED"
        summary = f"{len(blockers)} evidenced blocker(s). " + ("Unreviewed scope is listed in coverage." if unreviewed else "")
    elif unreviewed or any(c["status"] in ("failed", "ambiguous", "unsupported") and c["required"] for c in checks):
        status = "INCOMPLETE"
        summary = "No blocker established, but review is incomplete: " + "; ".join(unreviewed[:4])
    else:
        status = "PASS"
        summary = f"Effective requirements are implemented by specification, workflow and linked code on all {len(grid)} equivalence classes of the declared domain."
    return _finish(report, status, findings, checks, unreviewed, {"requirements": cov_reqs, "artifacts": art_cov, "grid": gmeta}, t0, bundle, summary)


def _finish(report, status, findings, checks, unreviewed, coverage, t0, bundle, summary):
    findings = sorted(findings, key=lambda f: (SEV_RANK[f["severity"]], f.get("artifact") or "", f.get("rule_ref") or ""))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F-{i:03d}"
    report.update({"status": status, "summary": summary, "findings": findings, "coverage": {**coverage, "unreviewed_scope": unreviewed},
                   "checks": checks, "usage": {"runtime_s": round(time.perf_counter() - t0, 4), "model_calls": 0, "model_retries": 0}})
    return report


# ----------------------------------------------------------------------------- Stage 3 wrapper: model, cache
def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), default=str)


def input_fingerprint(bundle, cfg, model, model_required) -> str:
    import hashlib
    m = None if model is None else {"name": model.name, "model_id": getattr(model, "model_id", None), "required": model_required}
    return hashlib.sha256(_canon({"bundle": bundle, "cfg": cfg.__dict__, "model": m, "reviewer": REVIEWER_VERSION}).encode()).hexdigest()


class ReportCache:
    """Keyed by sha256 of (whole bundle incl. approvals + config + reviewer version + model config). Any changed
    artifact/approval changes the key, so a stale passing report can never be returned. Reports whose required
    model check failed are not cached (transient)."""

    def __init__(self, directory: str | None = None):
        self.dir = Path(directory) if directory else None
        self.mem: dict[str, dict] = {}
        self.hits = self.misses = 0
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str):
        r = self.mem.get(key)
        if r is None and self.dir and (self.dir / f"{key}.json").exists():
            r = json.loads((self.dir / f"{key}.json").read_text())
        if r is not None and r.get("inputs", {}).get("fingerprint") == key:
            self.hits += 1
            return json.loads(json.dumps(r))
        self.misses += 1
        return None

    def put(self, key: str, report: dict):
        self.mem[key] = report
        if self.dir:
            (self.dir / f"{key}.json").write_text(json.dumps(report))


def review(bundle: dict, cfg: Config | None = None, model=None, model_required: bool = False, cache: ReportCache | None = None) -> dict:
    from .model import run_model_review
    cfg = cfg or Config()
    key = input_fingerprint(bundle, cfg, model, model_required)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            hit["cache"] = {"hit": True, "fingerprint": key}
            return hit
    rep = _review_core(bundle, cfg)
    rep["inputs"] = {"fingerprint": key}
    rep["cache"] = {"hit": False, "fingerprint": key}
    rep["model"] = {"configured": False, "note": "no model configured: model judgment was not part of this review"}
    if model is not None and rep["coverage"].get("requirements") is not None and rep["checks"][0]["status"] == "passed":
        mo = run_model_review(bundle, rep, model, cfg)
        _merge_model(rep, mo, model_required)
    if cache is not None and not (rep["model"].get("configured") and rep["model"].get("status") != "ok" and model_required):
        cache.put(key, rep)
    return rep


def _merge_model(rep: dict, mo: dict, required: bool):
    calls = len(mo["attempts"])
    rep["usage"].update({"model_calls": calls, "model_retries": max(0, calls - 1),
                         "model_tokens": {"input": sum((a.get("usage") or {}).get("input_tokens") or 0 for a in mo["attempts"]),
                                          "output": sum((a.get("usage") or {}).get("output_tokens") or 0 for a in mo["attempts"])}})
    det_blockers = {(f["artifact"], f["rule_ref"]) for f in rep["findings"] if f["severity"] == "blocker"}
    det_blocker_arts = {a for a, _ in det_blockers}
    reviewed = {n for n, c in rep["coverage"]["artifacts"].items() if c["status"] == "reviewed"}
    conflicts = []
    for f in mo["accepted_findings"]:
        art, ref = f["artifact"], f.get("rule_ref")
        corroborated = any(a == art and (ref is None or r == ref or r is None) for a, r in det_blockers)
        sev = f["severity"]
        note = ""
        if sev in ("blocker", "major") and not corroborated:
            if art in reviewed and art not in ("requirements", "approvals"):
                # deterministic exhaustive check found this artifact equivalent for the cited rule
                conflicts.append({"type": "model_claim_contradicted_by_exhaustive_check", "artifact": art, "rule_ref": ref, "claim": f["claim"]})
                sev, note = "info", " [contradicted by the exhaustive deterministic check; kept as info only]"
            else:
                sev, note = "minor", " [unverified model claim: advisory only, cannot establish a blocker]"
        rep["findings"].append(finding(sev, "model_observation", f["claim"] + note, rule_ref=ref, artifact=art, location=f["location"],
                                       evidence=[{"artifact": art, "location": f["location"], "quote": f["evidence_quote"]}],
                                       correction=f.get("correction"), confidence="model", source="model", corroborated_by_deterministic=corroborated))
    if mo["status"] == "ok" and mo["verdict_opinion"] == "PASS" and (rep["status"] != "PASS"):
        conflicts.append({"type": "model_verdict_conflicts_with_deterministic_status", "model_verdict": "PASS", "deterministic_status": rep["status"]})
    mo["conflicts"] = conflicts
    mo["accepted_findings"] = len(mo["accepted_findings"])
    rep["model"] = mo
    ok = mo["status"] == "ok"
    rep["checks"].append({"name": "model_review", "kind": "model", "status": "passed" if ok else "failed", "required": required,
                          "details": [mo.get("error") or "ok", f"calls={calls}"]})
    if not ok and required:
        rep["coverage"]["unreviewed_scope"].append(f"model review required but failed ({mo['error']}); no model-based review of ambiguous/out-of-language content")
        if rep["status"] == "PASS":
            rep["status"] = "INCOMPLETE"
            rep["summary"] = f"Deterministic checks passed, but the required model review failed ({mo['error']}); not reported as PASS."
        else:
            rep["summary"] += f" Required model review failed ({mo['error']}); disclosed as unreviewed."
    for i, f in enumerate(sorted(rep["findings"], key=lambda f: (SEV_RANK[f["severity"]], f.get("artifact") or "", f.get("rule_ref") or "")), 1):
        f["id"] = f"F-{i:03d}"
    rep["findings"].sort(key=lambda f: f["id"])


def route_ticket(bundle: dict, raw_ticket: dict) -> dict:
    """Debug aid / documented ticket-input contract: route one ticket through every artifact.
    Ticket fields: category (str|null), tier (str|null), urgent (bool), waiting_minutes (number >= 0)."""
    from .lang import ticket_in
    t = ticket_in(raw_ticket)
    cands, _ = derive_effective(bundle["requirements_doc"], bundle["approvals"])
    out = {"ticket": ticket_out(t), "effective_requirements": []}
    for c in cands:
        r, fired = route_policy(c.policy(), t)
        out["effective_requirements"].append({"candidate": c.label, "route": r, "fired": fired})
    sp, _ = _spec_policy(bundle["specification"])
    out["specification"] = dict(zip(("route", "fired"), route_policy(sp, t))) if sp else "unreviewable"
    _, nodes, bad = _workflow_structure(bundle["workflow"])
    if not bad and bundle["workflow"]["entry"] in nodes:
        r, path = _walk_workflow(nodes, bundle["workflow"]["entry"], t)
        out["workflow"] = {"route": r, "path": path}
    else:
        out["workflow"] = "unreviewable"
    for s in bundle["snippets"]:
        try:
            fn, p = pycode.parse_snippet(s["source"], s.get("entry"))
            r, ln = pycode.run_snippet(fn, p, t)
            out[f"code:{s['id']}"] = {"route": r, "line": ln}
        except (Unsupported, SyntaxError) as e:
            out[f"code:{s['id']}"] = f"unreviewable: {e}"
    return out
