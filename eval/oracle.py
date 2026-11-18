"""Independently written routing oracle used ONLY to validate labels/counterexamples in the evaluation harness.
It shares no code with shodh_review (no DSL, no parser). Parameters come from hand-written labels."""


def oracle_route(t: dict, p: dict) -> str:
    w = t["waiting_minutes"]
    for key in p["order"]:
        if key == "missing" and t["category"] is None:
            return "manual_review"
        if key == "urgent" and t["urgent"]:
            return p.get("urgent_route", "priority_queue")
        if key == "gold" and t["tier"] == "gold" and w >= p["gold_min"]:
            return "priority_queue"
        if key == "vip_always" and t["tier"] == "vip":
            return "priority_queue"
        if key == "wait":
            th, op = p["wait"]
            hit = (w >= th) if op == ">=" else (w > th)
            if hit:
                return "priority_queue" if (p.get("vip_exc") and t["tier"] == "vip") else "escalation"
            if p.get("no_false_branch"):
                return "<missing_branch>"
    return "normal_queue"
