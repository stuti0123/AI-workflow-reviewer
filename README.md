# AI-assisted workflow reviewer (Shodh AI take-home)

Checks whether a structured specification, workflow graph and linked Python snippets implement written routing
requirements, with evidence-backed findings (`PASS` / `CHANGES_REQUIRED` / `INCOMPLETE`).

```bash
pip install -r requirements.txt            # pinned: jsonschema, pytest (stdlib otherwise; CPU only, no network)
python scripts/run_all.py                  # ONE command: tests, fixture reproducibility, freeze check, baseline vs final,
                                           # held-out, two ablations -> reports/*.json + reports/summary.md
python -m shodh_review review fixtures/... # CLI (see shodh_review/cli.py); --model fake:<mode> | anthropic
```
* `docs/ENGINEERING_NOTE.md` design, evaluation, two debugging examples, limitations; `docs/AI_ASSISTANCE.md` AI disclosure.
* `schemas/` contracts · `fixtures/` synthetic bundles, `labels/` (kept apart from inputs), `generate.py` (seeded) · `eval/` oracle, harness, freeze · `checkpoints/checkpoint1/` initial working version · `reports/` machine-readable results.
* Fake-client results are labeled as such everywhere; no real-model numbers exist. No secrets in the repo.
