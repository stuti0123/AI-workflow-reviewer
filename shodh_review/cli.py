"""CLI.  python -m shodh_review review bundle.json [--model fake:clean|anthropic] [--model-required] [--cache-dir D]
       python -m shodh_review route bundle.json ticket.json
       python -m shodh_review validate bundle.json"""
from __future__ import annotations

import argparse
import json
import sys

from .model import make_client
from .review import Config, ReportCache, _schema_errors, review, route_ticket


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="shodh_review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review"); r.add_argument("bundle"); r.add_argument("--model", default=None, help="fake:<mode> | anthropic")
    r.add_argument("--model-required", action="store_true"); r.add_argument("--cache-dir"); r.add_argument("--out")
    t = sub.add_parser("route"); t.add_argument("bundle"); t.add_argument("ticket")
    v = sub.add_parser("validate"); v.add_argument("bundle")
    a = ap.parse_args(argv)
    bundle = json.load(open(a.bundle))
    if "bundle" in bundle and "requirements_doc" not in bundle:   # accept fixtures/cases/*.json wrapper
        bundle = bundle["bundle"]
    if a.cmd == "validate":
        errs = _schema_errors(bundle)
        print(json.dumps({"valid": not errs, "errors": errs}, indent=1))
        return 0 if not errs else 2
    if a.cmd == "route":
        print(json.dumps(route_ticket(bundle, json.load(open(a.ticket))), indent=1))
        return 0
    client = None
    if a.model:
        kind, _, mode = a.model.partition(":")
        client = make_client({"client": kind, "mode": mode or "clean"})
    rep = review(bundle, Config(), model=client, model_required=a.model_required, cache=ReportCache(a.cache_dir) if a.cache_dir else None)
    txt = json.dumps(rep, indent=1)
    if a.out:
        open(a.out, "w").write(txt + "\n")
    print(txt if not a.out else f"{rep['status']}: {rep['summary']}")
    return {"PASS": 0, "CHANGES_REQUIRED": 1, "INCOMPLETE": 3}[rep["status"]]


if __name__ == "__main__":
    sys.exit(main())
