# Independent final safety review

Decision: **KEEP candidate v3 for the bounded static resistor-network extension. No remaining blocking issue was found in the reviewed scope.**

This decision is independent of the implementation author's evaluation or benchmark score. No `evals/circuit_bench/` answers or results were read. The independent oracle uses exact inverse KCL and Gauss-Jordan elimination, separately implemented in `check_candidate.py`; it does not call the production LDL factorization. The source test suite also has no `circuit_bench`, `benchmark`, or `evals/` references (verified with `rg`).

## Verified final implementation

| File | SHA256 |
| --- | --- |
| scripts/solve_dividers.py | ca4a3c6d4c1af05de066b0ab358953799734e6dae2c7c5c4d156f7c2e6affee5 |
| scripts/lint.py | 560c1efc6168779af2477b688b2e353837c6a5400bbc9b0f0509d50dbc3168c5 |
| scripts/electrical_contract.py | 7796804c39bfe40989addd35a7713d555ebec9df1e600d26b35470425c1cbba1 |

The final contract hash differs from the test-run hash `14d52d6733ef6f59315af2bc2999739cc350d959228eccfe98731c33a9a2c1e8` only by its dependency-discovery docstring. That final difference was read and contains no executable change.

## Blockers closed

**S1, floating numerical error near a narrow acceptance boundary:** The fixed four-resistor counterexample now computes exact `2.40000000016 V`; a lower requirement of `2.4000001 V` can no longer falsely PASS. Raw decimal resistance/tolerance values are retained as fractions. Corner extrema and acceptance comparison use exact fractions; reported float limits are rounded outward. Decimal resistances including 0.9 ohm were independently tested, not only integer ohms.

**S2, planner omitting newly mandatory dependencies:** The same missing-R-source input now gives WAITING_EVIDENCE for both the coverage parent and evidence child. Missing-source test payloads deliberately remove automatically generated bindings after the production fixture runs, so improvements to dependency discovery cannot silently turn a negative fixture into a complete positive one. Lint still independently rejects missing R/C evidence.

**S3, malformed source/reference endpoint type crashing planning:** v2 accepted a list/dict endpoint through `validate_evidence()` and then raised an unhandled TypeError in graph extraction. v3 checks nonempty string endpoints before constructing sets. The fixed list-source and dict-reference cases now remain controlled missing-evidence outcomes without crashing the planner.

## Final independently executed verification

| Check | Result |
| --- | --- |
| Independent safety suite | 17/17 passed, exit 0 |
| Full source regression suite | 146/146 passed, exit 0 |
| Deterministic numeric stress | 513 networks: 493 accepted, 20 rejected |
| Exact oracle equality for accepted stress networks | 493/493 exact min/max fraction matches |
| Floating enclosure for accepted stress networks | 493/493 contain the exact result |
| Interior-point cross-checks on shared/bridge networks | 256/256 contained in exact corner envelopes |
| Fixed-resistor mesh extension | 11 distinct edges retained; exact min/max match independent KCL |

The stress suite's largest reported float upper-bound difference from the nearest float representation of the exact value was `1.8189894035458565e-12 V` at about `8240.8 V`. This is verified outward rounding, not a solver forward-error estimate; the associated exact bound is identical to the independent oracle.

The independent cases additionally cover all physical resistors appearing once, dangling resistor edges, hidden Q/D/J/U/M loads, intermediate capacitor dependency binding, DNP removal, missing tolerance, stale netlist/document/identity, pseudonets, malformed/multiple physical resistor pins, distinct reference domains, loss of source control through a clamped reference, dynamic-range/resource limits, and signed bias.

## Resource-policy distinction

v3 deliberately changes the support limit from ten total resistors to **twenty total resistors, twelve nets, and at most ten resistors with nonzero tolerance**. It retains the same maximum binary resistor-corner budget. The fixed electrical expectation is still that eleven independently varying resistors cannot generate a guaranteed window. The corresponding assertion now includes window generation, allowing either graph collection or the window entrypoint to enforce the rejection. No voltage, missing-evidence, or false-PASS expectation was relaxed. The extra fixed-resistor capacity was checked using an independently constructed eleven-edge mesh.

## Supported safety conclusion and limits

The extension supports complete, bounded, positive, linear DC resistor networks with one declared ideal source, one reference, and input bias applied at the feedback node. It preserves explicit INSUFFICIENT behavior for unsupported topology, undeclared loads, missing/stale parameter evidence, loss of source control, and exceeded limits. The legacy reducible-divider/CLI path remains separate. This review does not validate physical source reachability, regulator headroom, startup, loop stability, transient behavior, or whole-board release.

## Reproduce

```sh
env TMPDIR=/tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety PYTHONDONTWRITEBYTECODE=1 python3 -B /tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety/check_candidate.py /tmp/codex-work/schematic-optimize.2Xfcdr/candidate
env TMPDIR=/tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety PYTHONDONTWRITEBYTECODE=1 python3 -B /tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety/stress_numeric.py /tmp/codex-work/schematic-optimize.2Xfcdr/candidate
env TMPDIR=/tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s /tmp/codex-work/schematic-optimize.2Xfcdr/candidate/scripts/tests -q
```

Run all commands with `/bin/sh`, `login:false`, `workdir:/tmp`. Review files and temporary fixtures remain inside the designated `independent-safety` directory. No production/candidate file was modified by this reviewer.
