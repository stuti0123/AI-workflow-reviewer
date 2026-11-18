"""Frozen-set evaluation harness. Labels live in fixtures/labels and are never passed to the reviewer.

  python eval/run_eval.py --impl-path . --split all --out reports/final.json
  python eval/run_eval.py --impl-path checkpoints/checkpoint1 --split all --out reports/baseline.json
Reviewer inputs: bundle + (optional) run_config.model. Nothing else.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from oracle import oracle_route  # noqa: E402


def load_cases(split):
    out = []
    for f in sorted((ROOT / "fixtures" / "cases").glob("*.json")):
        lab = json.loads((ROOT / "fixtures" / "labels" / f.name).read_text())
        if split != "all" and lab["split"] != split:
            continue
        out.append((f.stem, json.loads(f.read_text()), lab))
    return out


def grounded(ev, bundle):
    art, loc, q = ev.get("artifact"), ev.get("location"), ev.get("quote")
    if not (art and loc and q):
        return False
    if art == "requirements":
        return loc in {r["anchor"] for r in bundle["requirements_doc"]["requirements"]}
    if art == "approvals":
        return True
    if art == "specification":
        return q in json.dumps(bundle["specification"], sort_keys=True) or q.startswith("default_route=")
    if art == "workflow":
        return q in json.dumps(bundle["workflow"], sort_keys=True)
    if art.startswith("code:"):
        return any(q in s["source"] for s in bundle["snippets"] if s["id"] == art[5:])
    return False


def cx_valid(f, label):
    cx = f.get("counterexample")
    if not cx:
        return False
    t = cx["ticket"]
    t = {"category": t.get("category"), "tier": t.get("tier"), "urgent": bool(t.get("urgent")), "waiting_minutes": t["waiting_minutes"]}
    if oracle_route(t, label["oracle_expected"]) != cx["expected_route"]:
        return False
    pa = label["oracle_actual"].get(f["artifact"])
    if pa is None:
        return None  # cannot verify actual side independently
    got = oracle_route(t, pa)
    return got == cx["actual_route"] or (got.startswith("<") and cx["actual_route"].startswith(got[:-1]))


def run(impl_path, split, ablation=None):
    sys.path.insert(0, str(Path(impl_path).resolve()))
    for m in [m for m in sys.modules if m.startswith("shodh_review")]:
        del sys.modules[m]
    pkg = importlib.import_module("shodh_review")
    impl_file = Path(pkg.__file__).resolve().parent
    sig = inspect.signature(pkg.review)
    supports_model = "model" in sig.parameters
    cfg = pkg.Config()
    for k, v in (ablation or {}).items():
        setattr(cfg, k, v)
    mk = None
    if supports_model:
        mk = importlib.import_module("shodh_review.model").make_client
    results, t_all = [], time.perf_counter()
    for cid, case, lab in load_cases(split):
        bundle = case["bundle"]
        rc = case.get("run_config", {}).get("model")
        kwargs = {}
        if supports_model:
            m = rc or {"client": "fake", "mode": "clean", "required": True}
            kwargs["model"] = mk(m)
            kwargs["model_required"] = m.get("required", True)
        t0 = time.perf_counter()
        try:
            rep = pkg.review(json.loads(json.dumps(bundle)), cfg, **kwargs)
            err = None
        except Exception as e:  # a crash is a result, not an excuse
            rep, err = {"status": "CRASH", "findings": [], "usage": {}}, f"{type(e).__name__}: {e}"
        dt = time.perf_counter() - t0
        blockers = [f for f in rep["findings"] if f["severity"] == "blocker" and f.get("source", "deterministic") == "deterministic"]
        det = []
        for d in lab["defects"]:
            cand = [f for f in blockers if f["artifact"] == d["artifact"] and (d["rule_ref"] is None or f["rule_ref"] == d["rule_ref"])]
            art_only = [f for f in blockers if f["artifact"] == d["artifact"]]
            f = cand[0] if cand else None
            det.append({"defect": d, "found_blocker_on_artifact": bool(art_only), "matched_with_rule_ref": bool(cand),
                        "evidence_grounded": bool(f and f["evidence"] and all(grounded(e, bundle) for e in f["evidence"])),
                        "counterexample_valid": cx_valid(f, lab) if f else False})
        matched = {id(f) for d in lab["defects"] for f in blockers if f["artifact"] == d["artifact"] and (d["rule_ref"] is None or f["rule_ref"] == d["rule_ref"])}
        unsupported = []
        for f in blockers:
            why = []
            if id(f) not in matched:
                why.append("not_a_labeled_defect")
            if not f["evidence"] or not all(grounded(e, bundle) for e in f["evidence"]):
                why.append("ungrounded_evidence")
            if f.get("counterexample") and cx_valid(f, lab) is False:
                why.append("counterexample_disagrees_with_oracle")
            if why:
                unsupported.append({"finding": f["id"], "artifact": f["artifact"], "rule_ref": f["rule_ref"], "why": why})
        results.append({"case": cid, "split": lab["split"], "expected": lab["expected_status"], "predicted": rep["status"], "error": err,
                        "defects": det, "unsupported_findings": unsupported, "n_findings": len(rep["findings"]),
                        "n_blockers": len(blockers), "runtime_s": round(dt, 5), "usage": rep.get("usage", {}),
                        "model_rejected_claims": rep.get("model", {}).get("rejected_findings", []) if isinstance(rep.get("model"), dict) else [],
                        "finding_types": sorted({f["type"] for f in rep["findings"] if f["severity"] in ("blocker", "major")}),
                        "paired_with": lab.get("paired_with")})
    return summarize(results, impl_file, time.perf_counter() - t_all, supports_model, split, ablation)


def summarize(results, impl_file, total_s, supports_model, split, ablation):
    cls = lambda s: {"PASS": "valid", "CHANGES_REQUIRED": "defective", "INCOMPLETE": "incomplete"}[s]
    by = {"valid": [], "defective": [], "incomplete": []}
    for r in results:
        by[cls(r["expected"])].append(r)
    conf = {c: {} for c in by}
    for c, rs in by.items():
        for r in rs:
            conf[c][r["predicted"]] = conf[c].get(r["predicted"], 0) + 1
    ds = [d for r in results for d in r["defects"]]
    n_def = len(ds)
    pairs = []
    byid = {r["case"]: r for r in results}
    for r in results:
        if r["paired_with"] and r["paired_with"] in byid:
            o = byid[r["paired_with"]]
            pairs.append({"a": o["case"], "b": r["case"], "a_status": o["predicted"], "b_status": r["predicted"],
                          "same_outcome": o["predicted"] == r["predicted"], "same_finding_types": o["finding_types"] == r["finding_types"],
                          "expected_same": o["expected"] == r["expected"]})
    return {
        "implementation": str(impl_file), "split": split, "ablation": ablation or None, "supports_model_integration": supports_model,
        "n_cases": len(results),
        "decision_correctness": {
            "false_passes": {"count": conf["defective"].get("PASS", 0), "denominator": len(by["defective"]), "label": "PASS predicted / defective cases"},
            "false_blocks": {"count": conf["valid"].get("CHANGES_REQUIRED", 0), "denominator": len(by["valid"]), "label": "CHANGES_REQUIRED predicted / valid cases"},
            "valid_cases_passed": {"count": conf["valid"].get("PASS", 0), "denominator": len(by["valid"])},
            "defective_cases_blocked": {"count": conf["defective"].get("CHANGES_REQUIRED", 0), "denominator": len(by["defective"])},
            "incomplete_cases_marked_incomplete": {"count": conf["incomplete"].get("INCOMPLETE", 0), "denominator": len(by["incomplete"])},
            "abstentions_on_valid": conf["valid"].get("INCOMPLETE", 0), "abstentions_on_defective": conf["defective"].get("INCOMPLETE", 0),
            "incomplete_cases_false_pass": conf["incomplete"].get("PASS", 0), "incomplete_cases_false_block": conf["incomplete"].get("CHANGES_REQUIRED", 0),
            "status_exact_match": {"count": sum(r["expected"] == r["predicted"] for r in results), "denominator": len(results)},
            "confusion_by_case_type": conf,
        },
        "finding_quality": {
            "labeled_defects": n_def,
            "defect_artifact_found": {"count": sum(d["found_blocker_on_artifact"] for d in ds), "denominator": n_def},
            "defect_artifact_and_rule_correct": {"count": sum(d["matched_with_rule_ref"] for d in ds), "denominator": n_def},
            "evidence_grounded": {"count": sum(d["evidence_grounded"] for d in ds), "denominator": n_def},
            "counterexample_valid_vs_independent_oracle": {"count": sum(d["counterexample_valid"] is True for d in ds), "denominator": n_def},
            "unsupported_blocker_findings": {"count": sum(len(r["unsupported_findings"]) for r in results),
                                             "denominator": sum(r["n_blockers"] for r in results), "label": "blocker findings that are unlabeled, ungrounded or contradicted by the oracle / all blocker findings"},
            "model_claims_rejected": sum(len(r["model_rejected_claims"]) for r in results),
        },
        "paired_variants": pairs,
        "resource_use": {"fake_client_or_offline": {"total_runtime_s": round(total_s, 4), "mean_case_runtime_s": round(sum(r["runtime_s"] for r in results) / max(1, len(results)), 5),
                                                    "model_calls": sum(r["usage"].get("model_calls", 0) for r in results), "model_retries": sum(r["usage"].get("model_retries", 0) for r in results)},
                         "real_model": "not measured - no model access was used for any number in this report"},
        "cases": results,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl-path", default=str(ROOT))
    ap.add_argument("--split", default="all", choices=["dev", "heldout", "all"])
    ap.add_argument("--ablation", default=None, help="JSON dict of Config overrides")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    rep = run(a.impl_path, a.split, json.loads(a.ablation) if a.ablation else None)
    txt = json.dumps(rep, indent=1, default=str)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt + "\n")
    d = rep["decision_correctness"]; q = rep["finding_quality"]
    print(f"[{rep['split']}] cases={rep['n_cases']} exact={d['status_exact_match']['count']}/{d['status_exact_match']['denominator']} "
          f"false_pass={d['false_passes']['count']}/{d['false_passes']['denominator']} false_block={d['false_blocks']['count']}/{d['false_blocks']['denominator']} "
          f"incomplete_ok={d['incomplete_cases_marked_incomplete']['count']}/{d['incomplete_cases_marked_incomplete']['denominator']} | "
          f"defect_rule_ok={q['defect_artifact_and_rule_correct']['count']}/{q['labeled_defects']} cx_ok={q['counterexample_valid_vs_independent_oracle']['count']}/{q['labeled_defects']} "
          f"unsupported={q['unsupported_blocker_findings']['count']}/{q['unsupported_blocker_findings']['denominator']}")
