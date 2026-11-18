# Material AI assistance disclosure

**Tool:** Anthropic Claude (Claude Code / claude.ai) was used heavily, across more than one session.

**What the AI did:** proposed the architecture and wrote the Python implementation (`shodh_review/`), the checkpoint-1 version, the fixture generator and independent routing oracle, the evaluation and freeze harness, the tests, the evaluation reports, and the drafts of the README, the engineering note and this disclosure. It also ran a post-hoc audit of the repository (history, freeze, dataset distribution).

**How results were verified (automated, not by reading alone):**
- 25 pytest tests, including regression tests for both debugging examples, paired-variant tests and cache-staleness tests.
- Expected labels are stored apart from the inputs, and counterexamples are checked against a separately written oracle (`eval/oracle.py`) that shares no code with the reviewer.
- Fixtures regenerate byte-identically from the seed, and `scripts/run_all.py` checks this.
- Held-out hashes are checked by `eval/freeze.py verify`. See the freeze limitation in the engineering note.
- A controlled ablation (boundary points off) changed 4 case outcomes; the second ablation showed no status benefit and is reported as such.

**Not verified:** behaviour with a real model (never run), and generalization beyond the synthetic, author-written fixtures.

**Candidate verification:** I reviewed the assessment requirements, repository structure, README, engineering note, AI assistance disclosure and evaluation reports. I installed the dependencies and ran the automated test suite on this repository, with all 25 tests passing. I also inspected the freeze verification and its platform-specific path hashing issue. I have not independently verified real-model performance; all reported evaluation results use synthetic fixtures and a fake model client.
