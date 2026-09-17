# Independent candidate v1 safety result

Decision: **BLOCK RETENTION until S1 and S2 are corrected and reverified.**

No benchmark answer/results files were accessed. Review was based on the frozen invariants, the production diff, existing source regression tests, and independent synthetic KCL checks.

## S1: Accepted ill-conditioned network can falsely PASS a narrow requirement

Reproduction (all resistors explicitly 0% tolerance, Vref min/typ/max = 0.8 V, feedback bias = 0 A):

| Ref | Ends | Resistance |
| --- | --- | ---: |
| R1 | SOURCE - MID | 10000000000 ohm |
| R2 | MID - FB | 1 ohm |
| R3 | MID - GND | 10000000000 ohm |
| R4 | FB - GND | 10000000000 ohm |

Exact inverse KCL, computed with independent Fraction/Gauss-Jordan arithmetic, gives `2.40000000016 V`. The candidate returns `2.4000001988168904 V` as both guaranteed limits. With acceptance `[2.4000001, 3] V`, its hot Rule-08 returns PASS although the exact voltage is below the allowed minimum.

Cause: `solve_dividers.py` accepts the 1e10 conductance-ratio boundary, and Cholesky's residual/pivot checks do not bound the resulting output's forward error. `linear_feedback_window()` uses point float extrema as guaranteed bounds, and lint compares them directly to the specification without numerical uncertainty.

Scope: this is a deliberately narrow synthetic specification, not a claim of practical regulator failure. It is a concrete violation of the new solver's guaranteed-window/PASS contract. Correct by exact arithmetic or a justified outward error envelope/rejection with consistent acceptance comparison; do not simply relax this test's requirement.

## S2: Planner READY omits dependencies mandatory in the new hot path

A shared-arm four-resistor graph with only a bound U1 source produces both a READY coverage parent and READY evidence child in `build_review_plan()`. The same input correctly returns INSUFFICIENT from lint because R1-R4 are mandatory in its new graph-specific expansion.

Cause: required graph refs are expanded only inside `Lint._hot_divider()`. `electrical_contract.dependency_refs()` and planner `readiness_gaps()` do not include them. READY means evidence prerequisites are present, so it must not promise readiness when the known graph's required sources are missing. This issue does not independently produce hot PASS.

## Verification already completed

- 12 independent synthetic tests passed: shared/bridge exact corners, 256 interior checks, dead-end edge preservation, nonlinear/intermediate loads, omitted R/C sources, DNP, physical-pin/index validation, distinct reference/source, source-control loss, limits, malformed input, and stale identities/documents/netlists.
- Existing `test_electrical_safety.py`: 29 tests passed.
- 513 deterministic numeric stress cases: 493 accepted, 20 rejected. Maximum accepted relative error was `8.277370436667091e-8`, from S1 above.
- Two additional fixed regression tests for S1/S2 both failed as expected, reproducing the blockers through the public workflow.

Commands:

```sh
env TMPDIR=/tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety PYTHONDONTWRITEBYTECODE=1 python3 -B /tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety/check_candidate.py /tmp/codex-work/schematic-optimize.2Xfcdr/candidate
env TMPDIR=/tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety PYTHONDONTWRITEBYTECODE=1 python3 -B /tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety/stress_numeric.py /tmp/codex-work/schematic-optimize.2Xfcdr/candidate
```

Initial inspected SHA256 values: `solve_dividers.py=1f965e48b02ed66ce8d0d35104f9d649c8bb535b0152594edcb4195134013f66`, `lint.py=030d95e3b4d02049f3744b7911871520fe12419da53d495fc868d57d90c69942`, `electrical_contract.py=b3013bc04ddbd6081a94c7fb0ac1a46a5bdc3a251bd88b81b598c75ac4d87ead`. The S1/S2 targeted rerun still failed after an additional positive-corner check changed only the solver hash to `5b2c05fb1916c6c443c84903841ee0de7256bb352283fa9bcea7e34b903dfed3`.

The implementation author has acknowledged both blockers and is revising the candidate. Tests keep their assertions; missing-source fixtures explicitly remove automatically discovered bindings to preserve the same missing-evidence inputs even if production dependency discovery changes.
