"""Regression tests for the two debugging examples in docs/ENGINEERING_NOTE.md (inputs from fixtures/)."""
import json
from pathlib import Path

import generate as g
from shodh_review import review

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "cases"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())["bundle"]


# ---- Debug 1 (real): threshold drift attributed to no rule, no cause explained  (D02)
def test_debug1_threshold_drift_is_attributed_to_the_rule_and_explained():
    r = review(load("D02_unapproved_change_used"))
    f = [x for x in r["findings"] if x["severity"] == "blocker"]
    assert len(f) == 1
    assert f[0]["rule_ref"] == "R3"                         # before the fix: None
    assert f[0]["type"] == "unapproved_change_applied"      # before the fix: unrequested_behavior
    assert "(requirement R3) give 'normal_queue'" not in f[0]["message"]   # message bug: cited R3 as the expected-by rule
    assert f[0]["counterexample"]["ticket"]["waiting_minutes"] == 25


def test_debug1_attribution_does_not_blame_unrelated_rules():
    # vip-always rule (D08-like, built here) must NOT be blamed on R2 (urgent -> priority_queue) just because routes match
    s = g.std_spec(vip=False)
    s["rules"].insert(2, {"id": "S-V", "order": 35, "condition": g.tier("vip"), "route": "priority_queue"})
    r = review(g.bundle("t", reqs=g.std_reqs(vip=True), spec=s, wf=g.std_wf(vip=True), code=g.std_code(vip=True)))
    f = [x for x in r["findings"] if x["severity"] == "blocker" and x["artifact"] == "specification"]
    assert f and all(x["rule_ref"] != "R2" for x in f)


# ---- Debug 2 (real): blocker downgraded to INCOMPLETE because of an unrelated, later, unsupported requirement (D14)
def test_debug2_defect_before_unsupported_requirement_is_still_a_blocker():
    r = review(load("D14_defect_before_unsupported_requirement"))
    assert r["status"] == "CHANGES_REQUIRED"                # before the fix: INCOMPLETE
    f = [x for x in r["findings"] if x["severity"] == "blocker"]
    assert f and f[0]["rule_ref"] == "R3" and f[0]["counterexample"]["ticket"]["waiting_minutes"] == 30
    assert any("R5" in u for u in r["coverage"]["unreviewed_scope"])   # unreviewed scope still disclosed


def test_debug2_guard_unsupported_requirement_that_can_preempt_stays_incomplete():
    reqs = g.std_reqs()
    reqs.insert(1, {"id": "R0", "anchor": "policy.md#R0", "order": 15, "text": "Customers who seem upset should generally be handled with extra care."})
    r = review(g.bundle("t", reqs=reqs, code=g.std_code(30, ">")))     # unsupported R0 ranks BEFORE R3
    assert r["status"] == "INCOMPLETE"
    assert not [x for x in r["findings"] if x["severity"] == "blocker"]
