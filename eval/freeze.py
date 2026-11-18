"""Freeze evaluation inputs BEFORE the final run: hashes of held-out cases + labels, generation config, and the
implementation source trees. eval/verify_freeze.py (called by scripts/run_all.py) fails if anything changed."""
import hashlib, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tree_hash(paths):
    h = hashlib.sha256()
    for base in paths:
        for f in sorted(Path(base).rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                h.update(str(f.relative_to(ROOT)).encode()); h.update(f.read_bytes())
    return h.hexdigest()


def snapshot():
    held = {}
    for lab in sorted((ROOT / "fixtures" / "labels").glob("*.json")):
        l = json.loads(lab.read_text())
        if l["split"] == "heldout":
            held[lab.stem] = {"label_sha256": hashlib.sha256(lab.read_bytes()).hexdigest(),
                              "case_sha256": hashlib.sha256((ROOT / "fixtures" / "cases" / lab.name).read_bytes()).hexdigest()}
    return {"heldout": held, "generation_config": json.loads((ROOT / "fixtures" / "config.json").read_text()),
            "final_implementation_sha256": tree_hash([ROOT / "shodh_review", ROOT / "schemas"]),
            "checkpoint_implementation_sha256": tree_hash([ROOT / "checkpoints" / "checkpoint1"]),
            "oracle_sha256": hashlib.sha256((ROOT / "eval" / "oracle.py").read_bytes()).hexdigest()}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        want = json.loads((ROOT / "eval" / "freeze.json").read_text())
        got = snapshot()
        got.pop("frozen_at_commit", None); want.pop("frozen_at_commit", None); want.pop("declared_development_cases", None); want.pop("note", None)
        bad = [k for k in got if got[k] != want.get(k)]
        print("FREEZE OK" if not bad else f"FREEZE VIOLATED: {bad}")
        sys.exit(1 if bad else 0)
    snap = snapshot()
    snap["frozen_at_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    snap["note"] = ("Held-out cases were generated with their labels before any reviewer run on them. They were never executed through the reviewer, "
                    "inspected for output, or used for tuning before this freeze.")
    (ROOT / "eval" / "freeze.json").write_text(json.dumps(snap, indent=1) + "\n")
    print("frozen", len(snap["heldout"]), "held-out cases")
