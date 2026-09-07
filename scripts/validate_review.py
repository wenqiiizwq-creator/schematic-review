#!/usr/bin/env python3
"""Validate a completed review ledger; never infer electrical correctness from it.
Exit 0: valid ledger, 2: invalid; --require-release also returns 2 for NO_GO.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from validate_remediation import validate_remediation, READINESS

RESULTS = {"PASS", "FAIL", "INSUFFICIENT", "NA"}
SEVERITIES = {"P0", "P1", "P2", "P3"}
APPLICABILITY = {"APPLICABLE", "NOT_APPLICABLE", "UNDETERMINED"}
SCOPE = {"input_consistency", "requirements", "chains", "states", "datasheets", "history"}


def text(value):
    return isinstance(value, str) and bool(value.strip())


def evidence(value):
    return isinstance(value, list) and bool(value) and all(
        isinstance(x, dict) and text(x.get("source")) and text(x.get("locator"))
        for x in value)


def ids(value):
    return isinstance(value, list) and bool(value) and all(text(x) for x in value)


def fingerprint(data):
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_review(plan, report, db=None, lint_runs=None, require_actionable=False):
    errors, blockers = [], []
    def require(ok, message):
        if not ok:
            errors.append(message)
    def index(items, label):
        out = {}
        if not isinstance(items, list):
            errors.append(f"{label} must be an array")
            return out
        for item in items:
            if not isinstance(item, dict) or not text(item.get("id")):
                errors.append(f"{label}: invalid object/id")
                continue
            key = item["id"]
            require(key not in out, f"{label}: duplicate id {key}")
            out[key] = item
        return out

    if not isinstance(plan, dict) or not isinstance(report, dict):
        return {"valid": False, "errors": ["plan/results must be objects"],
                "release": "NO_GO", "blockers": ["invalid input"]}
    require(report.get("schema_version") == 2, "results.schema_version must be 2")
    remediation_version = report.get("remediation_version")
    actionable = require_actionable or "remediation_version" in report
    if actionable:
        require(type(remediation_version) is int and remediation_version == 1,
                "remediation_version must be 1 for actionable instructions")
    require(report.get("plan_digest") == fingerprint(plan), "plan_digest mismatch")
    if db is not None:
        if db.get('integrity', {}).get('self_check_passed') is False or db.get('export_errors'):
            blockers.append('input netlist failed integrity/export checks')
        require(report.get("db_digest") == fingerprint(db), "db_digest mismatch")
    expected = index(plan.get("checks"), "plan.checks")
    checks = index(report.get("checks"), "results.checks")
    findings = index(report.get("findings"), "findings")
    require(bool(expected), "empty review plan")
    require(set(checks) == set(expected),
            f"check coverage mismatch: missing={sorted(set(expected)-set(checks))}, "
            f"unexpected={sorted(set(checks)-set(expected))}")
    accepted = False
    for key, item in checks.items():
        result, app = item.get("review_result"), item.get("applicability")
        require(isinstance(result, str) and result in RESULTS, f"{key}: invalid result")
        require(isinstance(app, str) and app in APPLICABILITY, f"{key}: invalid applicability")
        require(text(item.get("rationale")), f"{key}: missing rationale")
        require(evidence(item.get("evidence")), f"{key}: missing locatable evidence")
        require(isinstance(item.get("blocking"), bool), f"{key}: blocking must be boolean")
        if key in expected and app != expected[key].get("applicability"):
            require(evidence(item.get("applicability_evidence")),
                    f"{key}: applicability changed without evidence")
        if result == "NA":
            require(app == "NOT_APPLICABLE", f"{key}: NA requires NOT_APPLICABLE")
        elif app == "NOT_APPLICABLE":
            require(False, f"{key}: NOT_APPLICABLE requires NA")
        elif app == "UNDETERMINED":
            require(result == "INSUFFICIENT", f"{key}: unresolved applicability")
        confidence = item.get("evidence_confidence")
        if result in ("PASS", "FAIL"):
            require(confidence in ("A", "B"), f"{key}: PASS/FAIL needs A/B evidence")
        if result == "INSUFFICIENT":
            require(confidence == "C", f"{key}: unresolved conclusion must remain C")
            require(ids(item.get("missing_inputs")), f"{key}: missing_inputs required")
            require(item.get("potential_severity") in tuple(SEVERITIES),
                    f"{key}: potential_severity required")
        if result == "FAIL":
            require(item.get("severity") in tuple(SEVERITIES), f"{key}: invalid severity")
            fid = item.get("finding_id")
            require(text(fid) and fid in findings, f"{key}: FAIL needs finding_id")
            if text(fid) and fid in findings:
                require(findings[fid].get("severity") == item.get("severity"),
                        f"{key}: finding severity mismatch")
                require(key in findings[fid].get("check_ids", []),
                        f"{key}: finding link is not reciprocal")
        else:
            require(item.get("severity") is None, f"{key}: severity is only for FAIL")
        state = item.get("disposition", "OPEN")
        require(state in ("OPEN", "FIXED_VERIFIED", "ACCEPTED", "RETRACTED"),
                f"{key}: invalid disposition")
        if state in ("FIXED_VERIFIED", "RETRACTED"):
            require(result in ("PASS", "NA"), f"{key}: closed correction needs current PASS/NA")
            require(evidence(item.get("closure_evidence")), f"{key}: missing closure evidence")
        approval_ok = False
        if state == "ACCEPTED":
            approval = item.get("acceptance", {})
            approval_ok = isinstance(approval, dict) and all(text(approval.get(k)) for k in
                ("by", "date", "scope", "reason", "record"))
            require(approval_ok, f"{key}: acceptance needs by/date/scope/reason/record")
            require(result in ("FAIL", "INSUFFICIENT"), f"{key}: acceptance cannot manufacture PASS")
            accepted = True
        critical = item.get("severity") in ("P0", "P1") or (
            result == "INSUFFICIENT" and item.get("potential_severity") in ("P0", "P1"))
        if result in ("FAIL", "INSUFFICIENT"):
            if item.get("severity") == "P0":
                blockers.append(f"{key}: P0 requires verified repair")
            elif (critical or item.get("blocking")) and not approval_ok:
                blockers.append(f"{key}: unresolved blocking {result}")
        h = item.get("handoff", {"required": False})
        require(isinstance(h, dict), f"{key}: invalid handoff")
        if isinstance(h, dict):
            require(isinstance(h.get("required"), bool), f"{key}: handoff.required must be boolean")
        if isinstance(h, dict) and expected.get(key, {}).get('handoff', {}).get('required') and not h.get('required'):
            require(evidence(item.get('handoff_evidence')), f'{key}: required planned handoff removed without evidence')
        if isinstance(h, dict) and h.get("required"):
            require(h.get("state") in ("OPEN", "ACCEPTED", "VERIFIED"), f"{key}: invalid handoff state")
            require(ids(h.get("receivers")) and text(h.get("constraint")) and text(h.get("verification")),
                    f"{key}: handoff lacks receiver/constraint/verification")
            if h.get("state") not in ("ACCEPTED", "VERIFIED"):
                blockers.append(f"{key}: required handoff not accepted")
            else:
                require(evidence(h.get("evidence")), f"{key}: handoff acceptance/verification needs record")

    for fid, item in findings.items():
        if actionable:
            errors.extend(validate_remediation(fid, item.get("remediation"), set(findings)))
        elif "remediation" in item:
            require(False, f"{fid}: remediation requires top-level remediation_version")
        require(item.get("severity") in tuple(SEVERITIES), f"{fid}: invalid severity")
        require(item.get("kind") in ("DEFECT", "IMPROVEMENT"), f"{fid}: invalid finding kind")
        require(ids(item.get("check_ids")), f"{fid}: check_ids required")
        linked = item.get("check_ids") if ids(item.get("check_ids")) else []
        require(all(x in checks for x in linked), f"{fid}: unknown check link")
        for field in ("title", "observed", "criterion", "impact", "scenario", "root_cause",
                      "recommendation", "verification", "severity_reason"):
            require(text(item.get(field)), f"{fid}: missing {field}")
        location = item.get("location", {})
        require(isinstance(location, dict) and all(ids(location.get(k)) for k in ("pages", "refs", "nets")),
                f"{fid}: location needs pages/refs/nets (use explicit unresolved locator for missing data)")
        if item.get("kind") == "DEFECT":
            require(any(checks[x].get("review_result") == "FAIL" and checks[x].get("finding_id") == fid
                        for x in linked if x in checks), f"{fid}: orphan defect")
        else:
            require(item.get("severity") == "P3", f"{fid}: optional improvement must be P3")
            require(all(checks[x].get("review_result") == "PASS" for x in linked if x in checks),
                    f"{fid}: improvement requires compliant underlying check")

    scope = report.get("scope_checks", {})
    require(isinstance(scope, dict) and set(scope) == SCOPE, "scope_checks must cover six audit dimensions")
    if isinstance(scope, dict):
        for dimension, key in scope.items():
            require(text(key) and key in checks, f"scope {dimension}: unknown check id")
            if text(key) and key in checks and checks[key].get("review_result") != "PASS":
                # A history audit may be NA for a documented first review.
                if not (dimension == "history" and checks[key].get("review_result") == "NA"):
                    blockers.append(f"scope {dimension}: coverage audit not PASS")

    coverage = report.get("coverage", {})
    require(isinstance(coverage, dict), "coverage must be an object")
    wanted = {"requirements": {str(x["object"]["requirement_id"]) for x in expected.values()
              if isinstance(x.get("object"), dict) and x["object"].get("requirement_id")}}
    if db is not None:
        wanted.update({
            "components": set(db.get("parts", {})),
            "pins": set(db.get("pin2net", {})) | set(db.get("declared_pinname", {})),
            "nets": set(db.get("nets", {})) - set(db.get("pseudo_nets", [])),
            "pages": {str(x) for x in db.get("ref2page", {}).values()},
        })
    if isinstance(coverage, dict):
        for dimension, objects in wanted.items():
            entries = coverage.get(dimension, {})
            require(isinstance(entries, dict), f"coverage.{dimension} must be object")
            if not isinstance(entries, dict):
                continue
            require(set(entries) == objects, f"coverage.{dimension}: missing={sorted(objects-set(entries))}, "
                    f"unexpected={sorted(set(entries)-objects)}")
            for obj, linked in entries.items():
                require(ids(linked) and all(x in checks for x in linked),
                        f"coverage.{dimension}.{obj}: missing/unknown check references")

    if lint_runs is not None:
        reviews = report.get('lint_reviews', [])
        require(isinstance(reviews, list), 'lint_reviews must be an array')
        review_map = {}
        for review in reviews if isinstance(reviews, list) else []:
            if not isinstance(review, dict) or not text(review.get('run_digest')):
                require(False, 'lint_reviews: invalid run_digest')
                continue
            digest = review['run_digest']
            require(digest not in review_map, 'duplicate lint run review')
            review_map[digest] = review.get('items')
        require(set(review_map) == {fingerprint(x) for x in lint_runs},
                'lint_reviews must match supplied cold/hot runs')
        for run in lint_runs:
            digest = fingerprint(run)
            findings_list = run.get('findings', []) if isinstance(run, dict) else run
            if not isinstance(findings_list, list):
                require(False, 'invalid lint findings list')
                continue
            dispositions = review_map.get(digest)
            require(isinstance(dispositions, dict), f'{digest}: missing lint item dispositions')
            if not isinstance(dispositions, dict):
                continue
            require(set(dispositions) == {str(i) for i in range(len(findings_list))},
                    f'{digest}: lint candidate coverage mismatch')
            for number, linked in dispositions.items():
                require(ids(linked) and all(x in checks for x in linked),
                        f'{digest}[{number}]: missing/unknown result check')

    counts = dict(Counter(x.get("review_result") for x in checks.values()
                          if isinstance(x.get("review_result"), str)))
    severity_counts = {s: sum(x.get("severity") == s and x.get("kind") == "DEFECT"
                          for x in findings.values()) for s in sorted(SEVERITIES)}
    computed = {"checks": len(checks), "results": counts,
                "confirmed_defects": sum(severity_counts.values()), "by_severity": severity_counts,
                "improvements": sum(x.get("kind") == "IMPROVEMENT" for x in findings.values())}
    if "summary" in report:
        require(report["summary"] == computed, "summary does not match unique findings/check results")
    release = "NO_GO" if errors or blockers else ("CONDITIONAL_GO" if accepted else "GO")
    if "release" in report:
        require(report["release"] == release, f"claimed release differs from computed {release}")
    repair_counts = {state: sum(isinstance(x.get("remediation"), dict) and
                    x["remediation"].get("readiness") == state for x in findings.values())
                    for state in READINESS}
    return {"valid": not errors, "errors": errors, "blockers": blockers,
            "release": "NO_GO" if errors else release, "summary": computed,
            "remediation_validation": {"enforced": actionable, "by_readiness": repair_counts}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("results")
    parser.add_argument("--db")
    parser.add_argument("--json")
    parser.add_argument("--lint", action="append", help="cold/hot lint JSON; repeat for each run")
    parser.add_argument("--require-release", action="store_true")
    parser.add_argument("--require-actionable", action="store_true",
                        help="require detailed repair instructions for every finding")
    args = parser.parse_args()
    try:
        read = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))
        result = validate_review(read(args.plan), read(args.results), read(args.db) if args.db else None,
                                 [read(x) for x in args.lint] if args.lint else None,
                                 require_actionable=args.require_actionable)
    except (ValueError, OSError, TypeError) as exc:
        result = {"valid": False, "release": "NO_GO", "errors": [str(exc)]}
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["valid"] or (args.require_release and result["release"] == "NO_GO"):
        sys.exit(2)


if __name__ == "__main__":
    main()
