# Material AI assistance disclosure

**Tools:** Anthropic Claude (via Claude Code / claude.ai) was used heavily for this assessment.

**What the AI did:** drafted the architecture, the Python implementation (`shodh_review/`), the fixture generator and independent oracle, the evaluation/freeze harness, the unit and regression tests, and the first drafts of the engineering note and this disclosure. Work was done across more than one AI session; the final session finished the freeze, evaluation runs and documentation.

**What I (the candidate) must confirm before submitting:** I have read the code and the note, can explain the design (threshold-cell exhaustive evaluation, candidate-based approval handling, validated model output), and have re-run `python scripts/run_all.py` myself. _(Edit this paragraph to describe what you personally reviewed or changed.)_

**How results were verified (not just generated):**
* 25 automated tests, including both debugging regressions, paired-variant and cache-staleness tests.
* Expected labels were fixed by construction and by a separately written routing oracle (`eval/oracle.py`), not by the reviewer; counterexamples are checked against that oracle.
* Held-out labels/config/implementation hashes were frozen before the held-out run, and `scripts/run_all.py` fails if they change.
* A controlled ablation (boundary points off) was run to confirm a mechanism matters; a second ablation showed no benefit and is reported as such.
* Fixture regeneration from the seed is checked to be byte-identical.

**Not verified:** behavior with a real model (never run); generalization beyond the synthetic, author-written fixtures.
