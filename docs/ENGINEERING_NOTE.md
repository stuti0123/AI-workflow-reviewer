# Engineering note: AI-assisted workflow reviewer

All numbers come from synthetic fixtures and a **fake model client**. Real-model performance was **not measured**.
Reproduce everything offline on CPU: `pip install -r requirements.txt && python scripts/run_all.py`.

## 1. Supported scope and contracts (Stage 1)
A *review bundle* (`schemas/bundle.schema.json`) holds: anchored source requirements, optional `changes[]` and top-level `approvals[]`, a structured specification, a versioned workflow graph, linked Python snippets, and the ticket schema. Every artifact carries a version, and every finding cites artifact, version and location.

* **Condition language** (structured): missing, equality, numeric `< <= > >= ==` with inclusive/exclusive boundaries, nested AND/OR/NOT, ordered exceptions, and declared time units (seconds/minutes/hours/days). A time comparison without a unit is *ambiguous* and is not guessed.
* **Source language**: sentence patterns of the form "If … then route to …", "Otherwise …", with exceptions; anything else becomes an `unsupported` requirement that stays in coverage.
* **Python subset**: one function; `if/elif/else`, `return "<route>"`, and/or/not, comparisons, `ticket[...]`/`.get`, constants. Never `exec`'d: the AST is checked against the subset and interpreted. Syntax check, subset inspection and interpretation are reported as separate checks.
* **Effective requirements**: only top-level approval records carry authority; "approved by the CTO" inside prose is reported and ignored. A change applies iff approved, targeted at the requirement, and scoped to the current requirements version; applicable changes apply in ascending `seq`. Contradictions (approved + rejected for the same change, or same-`seq` conflicting approvals) create *alternative candidates*; nothing is chosen silently.

## 2. Semantic checks (Stage 2)
Every artifact is reduced to a function ticket → route. Because each atomic condition is a threshold/equality test, its truth value is constant on each cell between the thresholds that appear anywhere in the bundle. The reviewer builds one representative ticket per threshold, per open interval, above the maximum, per named string value plus "any other string", and for each missing value, then evaluates **every cell** under the effective requirements, specification, workflow traversal and interpreted code. That is *exhaustive within the declared language*, and valid unusual formulations (De Morgan, split nodes, merged rules) pass because the comparison is behavioral, not textual. If the grid exceeds a cap it falls back to a sample and the review cannot be PASS (reported as unreviewed).

Findings carry a concrete counterexample ticket with expected route, actual route, which rule produced each, and the artifact location. Defect types: changed threshold/boundary/unit, wrong precedence, omitted requirement (found by behavior even when the spec has no matching ID), wrong/missing branch, script disagreement, and unrequested behavior (e.g. an unapproved change). Overlapping rules and exceptions are covered because precedence is part of the evaluated function. When approval candidates disagree, a blocker is raised only if the defect holds under **every** candidate; otherwise the result is INCOMPLETE.

## 3. Model integration and failure handling (Stage 3)
`model.py` defines a request/response interface, a reviewer prompt, a deterministic `FakeClient` (clean, timeout, malformed, truncated, invented references, claims PASS over a defect, …) and an `AnthropicClient` (not exercised in the offline suite). Request configuration, prompt hash and response hash are stored as provenance. **What a model may contribute:** advisory observations on prose or ambiguous content that the structured checks cannot judge. **What it cannot do:** establish a blocker, change approvals, or turn an incomplete review into PASS. Model output is validated: JSON must parse and match the schema; rule references must exist; cited evidence must occur in the cited artifact; conflicting claims are rejected. Retries and calls are bounded; text inside requirements or code comments is data and cannot alter reviewer behavior (injection attempts are reported). A required check that fails yields INCOMPLETE, never PASS. Oversized input is reported as unreviewed rather than truncated. The cache key hashes the bundle (artifacts, approvals), config and reviewer version, so a changed approval or artifact cannot reuse a stale passing report.

**Status rules.** `CHANGES_REQUIRED`: an evidenced blocker exists (unreviewed scope is still disclosed). `INCOMPLETE`: no blocker, but missing evidence, ambiguity, unsupported content, oversize or a failed required check. `PASS`: complete review of the effective requirements within the supported scope and no blocker.

## 4. Evaluation (Stage 4)
28 bundles: 7 valid, 14 defective, 7 requiring INCOMPLETE; covering approved/unapproved changes, approval conflicts, missing requirements, boundaries, units, precedence, wrong/missing branches, code disagreement, injected instructions, model failures, a valid unusual formulation, a multi-rule defect, and paired variants (renamed IDs / reordered records / paraphrase). Labels are stored separately from inputs (`fixtures/labels`), and expected routes come from an independently written oracle (`eval/oracle.py`), not from the reviewer. **Held-out:** 7 cases (D08, D10, D12, I03, I05, I06, V04) whose labels, generator config and both implementation trees were hashed in `eval/freeze.json` (commit "Freeze held-out…") before the held-out run; `scripts/run_all.py` verifies the freeze. The other 21 are declared *development evidence*. Fixtures regenerate from seed `20261118`.

| run | status exact | false passes | false blocks | INCOMPLETE found | defect+rule ok | counterexample valid vs oracle | unsupported blockers |
|---|---|---|---|---|---|---|---|
| checkpoint-1, all | 25/28 | 0/14 | 0/7 | 5/7 | 14/16 | 14/16 | 1/15 |
| **final, all** | **28/28** | 0/14 | 0/7 | 7/7 | 16/16 | 16/16 | 0/16 |
| final, held-out | 7/7 | 0/3 | 0/1 | 3/3 | 3/3 | 3/3 | 0/3 |
| final, dev | 21/21 | 0/11 | 0/6 | 4/4 | 13/13 | 13/13 | 0/13 |
| ablation: no boundary points | 24/28 | 3/14 | 0/7 | 7/7 | 12/16 | 12/16 | 0/12 |

Resources (fake client / offline): 0.59 s for all 28 bundles, 30 model calls, 2 retries. Real-model resource use: not measured. Checkpoint-1 differs from final on `D02` (blocked but mis-attributed), `D14` (abstained), `I03`/`I06` (false PASS: no model-failure handling). Because the set is small and written by the same author as the reviewer, a perfect score shows the mechanisms work on these cases, not general accuracy.

**Ablation 1 (boundary points disabled; same inputs and settings).** `D05` (code `>` vs `>=` at the threshold), `D10` and `D13` flip from CHANGES_REQUIRED to **PASS** (3 false passes), and `D14` degrades to INCOMPLETE: sampling "typical" values never lands on a threshold. **Ablation 2 (model-claim validation disabled).** No status or finding-quality metric changes; the only effect is one model claim (`D13`, "PASS" asserted over a defect) no longer being rejected. This is a valid null result here: the verdict was already protected because model output cannot set status.

## 5. Two debugging examples (real failures; regression tests in `tests/test_regressions.py`)
**Debug 1: misattributed blocker (D02, checkpoint-1).** *Actual output* (`reports/debugging/debug1_before.json`): blocker with `rule_ref: null`, type `unrequested_behavior`, counterexample waiting=25 → expected `normal_queue`, actual `escalation`. *Diagnosis:* attribution used only the **expected** rule (the default); it never asked which requirement the actual route came from, so the unapproved 30→25 change was not named. *Fix:* attribute to the artifact rule that fired and compare it to the requirement it claims to implement, naming the unapproved change; also corrected a message that cited the wrong "expected-by" rule. *Regression:* `rule_ref == "R3"`, type `unapproved_change_applied`, and a guard that an unrelated VIP rule is not blamed on R2.

**Debug 2: defect hidden by an unrelated unsupported requirement (D14).** *Actual output* (`debug2_before.json`): status INCOMPLETE; the code defect at waiting=30 was only a "major" finding because the whole review was downgraded when any requirement was unsupported (R5: "customers who seem upset… extra care"). *Diagnosis:* the downgrade was global, but R5 ranks after R3 and cannot pre-empt it, so the blocker was fully evidenced. *Fix:* downgrade only when an unsupported requirement can precede the mismatching rule; keep disclosing R5 as unreviewed. *Regression:* D14 is CHANGES_REQUIRED with R5 still in `unreviewed_scope`, plus a guard where an unsupported requirement ranked *before* R3 correctly stays INCOMPLETE.

No fault injection was used in the debugging examples.

## 6. Limitations
* **Freeze provenance.** The held-out labels were committed with checkpoint-1, but `eval/freeze.json` was committed only after the final implementation commit, so git history cannot prove the held-out cases were untouched during tuning. The freeze guarantees only that labels, cases and code have not changed since it was recorded. The sentence in `eval/freeze.json` saying the cases were never run is the original author's statement and is not independently verified.
* **Debug example 2** before-output comes from an intermediate, uncommitted state; checkpoint-1 independently reproduces the same INCOMPLETE status on D14.
* Single-ticket routing over four declared fields; the language has no arithmetic across fields, regexes, loops or state. Prose outside the patterns is only listed as unreviewed.
* Exhaustiveness is claimed only for the structured language and the Python subset, not for arbitrary prose or Python.
* Real-model behavior is unmeasured; the fake client cannot show model quality. The prompt and interface are provided for a live run.
* Small, author-written benchmark; the held-out split guards against tuning but not against author blind spots.
