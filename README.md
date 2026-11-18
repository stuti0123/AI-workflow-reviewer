# AI-assisted workflow reviewer (Shodh AI take-home)

Checks whether a structured specification, workflow graph and linked Python snippets implement written routing
requirements, and reports `PASS` / `CHANGES_REQUIRED` / `INCOMPLETE` with evidence-backed findings.
CPU only, offline, no API keys. Python 3.10+ (developed on 3.13).

## Windows PowerShell setup and evaluation
```powershell
cd shodh-review
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process Bypass
python -m pip install -r requirements.txt
python scripts/run_all.py             # THE one command: tests, fixture reproducibility, freeze check,
                                      # checkpoint vs final, held-out, two ablations
Get-Content reports\summary.md        # human summary; machine-readable results are reports\*.json
```
Note: `run_all.py` regenerates `reports/*.json` (runtimes vary slightly between runs).

Other commands:
```powershell
python -m pytest -q tests
python eval/freeze.py verify
# CLI: python -m shodh_review review <bundle.json>  (a case file in fixtures\cases holds the bundle under its "bundle" key)
```

## Layout
- `shodh_review/` reviewer: `lang.py` (condition language), `effective.py` (approvals), `pycode.py` (Python subset), `review.py`, `model.py` (client interface, fake client), `cli.py`
- `schemas/` bundle contracts · `fixtures/` cases, `labels/` (separate from inputs), `generate.py` (seeded)
- `eval/` oracle, harness, freeze · `checkpoints/checkpoint1/` initial working version · `reports/` results, `reports/debugging/` failing outputs
- `tests/` unit and regression tests · `docs/ENGINEERING_NOTE.md`, `docs/AI_ASSISTANCE.md`

All results use a fake model client; real-model performance was not measured. See the limitations in the engineering note.
