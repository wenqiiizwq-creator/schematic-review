#!/usr/bin/env python3
"""Revision dependencies and re-verification routing, never electrical signoff.

Exact identifiers only. Partial dependency scopes fall back to a full review;
even NO_RECORDED_CHANGE is not permission to transfer an earlier verdict.
"""
from copy import deepcopy
import hashlib
import json
import os

from electrical_contract import db_fingerprint, dependency_refs, validate_evidence
from diff_netlists import diff_databases
from i2c_topology import validate_db_shape

VERSION = 1
COVERAGE_ID = 'ER7.revision-impact-coverage.GLOBAL'
FIELDS = ('refs', 'nodes', 'nets', 'states', 'check_ids')
SPEC_FIELDS = ('id', 'check', 'rule', 'object', 'criterion', 'applicability',
               'stage', 'executor', 'domain', 'evidence_check_id', 'parent_check_id', 'handoff')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value):
    return (isinstance(value, list) and all(text(x) for x in value)
            and len(value) == len(set(value)))


def check_spec(check):
    return {key: deepcopy(check[key]) for key in SPEC_FIELDS if key in check}


def is_revision_check(check):
    return check.get('check') in ('revision-impact-coverage', 'revision-removed-check')


def index_checks(plan):
    checks = plan.get('checks')
    if not isinstance(checks, list):
        raise ValueError('revision plan.checks must be an array')
    result = {}
    for check in checks:
        if (not isinstance(check, dict) or not text(check.get('id'))
                or check['id'] in result or not isinstance(check.get('object'), dict)
                or not text(check.get('criterion'))):
            raise ValueError('revision checks require unique IDs, objects and criteria')
        result[check['id']] = check
    return result


def validate_declarations(intent):
    declarations = (intent or {}).get('review_dependencies', {})
    if not isinstance(declarations, dict):
        return ['review_dependencies must be an object keyed by exact check ID']
    errors = []
    sources = (intent or {}).get('review_sources', [])
    if not isinstance(sources, list):
        errors.append('review_sources must be an array')
    else:
        seen = set()
        for source in sources:
            if (not isinstance(source, dict) or not all(text(source.get(k)) for k in ('id', 'path', 'citation'))
                    or not os.path.isabs(source['path']) or not strings(source.get('refs', []))):
                errors.append('review_sources requires id, absolute local path, citation and unique refs')
                continue
            if source['id'] in seen:
                errors.append('duplicate review_sources.id: ' + source['id'])
            seen.add(source['id'])
    allowed = set(FIELDS) | {'complete', 'citation', 'db_digest', 'check_digest'}
    for key, item in declarations.items():
        if not text(key) or not isinstance(item, dict):
            errors.append('review_dependencies requires named objects')
            continue
        if set(item) - allowed:
            errors.append(key + ': unknown dependency declaration fields')
        if type(item.get('complete')) is not bool or not text(item.get('citation')):
            errors.append(key + ': dependency complete boolean and citation required')
        for field in FIELDS:
            if field in item and not strings(item[field]):
                errors.append(key + ': dependency ' + field + ' must be unique strings')
        if item.get('complete') is True and not all(
                text(item.get(k)) and len(item[k]) == 64 and
                all(c in '0123456789abcdef' for c in item[k])
                for k in ('db_digest', 'check_digest')):
            errors.append(key + ': complete dependency scope requires db/check digests')
    return errors


def input_snapshot(db, intent=None, evidence=None, audit=None):
    """Freeze actual inputs and freshly hash available local source documents.

    No cached stat-based hash here: same-size file replacements must be visible.
    Unreadable documents are recorded, never silently removed from the manifest.
    """
    for label, value in (('intent', intent), ('evidence', evidence), ('datasheet_audit', audit)):
        if value is not None and not isinstance(value, dict):
            raise ValueError(label + ' must be an object')
    errors = validate_declarations(intent)
    if evidence:
        errors.extend(validate_evidence(evidence))
    if errors:
        raise ValueError('; '.join(errors))
    documents = {}
    for material in (audit or {}).get('materials', []):
        document = material.get('document') or {}
        path = document.get('path')
        if not text(path):
            continue
        path = os.path.abspath(path)
        item = documents.setdefault(path, {'path': path, 'refs': set(), 'global': False})
        item['refs'].update(material.get('refdes') or [])
        item['global'] |= not bool(material.get('refdes'))
    for source in (intent or {}).get('review_sources', []):
        path = os.path.abspath(source['path'])
        item = documents.setdefault(path, {'path': path, 'refs': set(), 'global': False})
        item['refs'].update(source.get('refs', []))
        item['global'] |= not bool(source.get('refs'))
    for path, item in documents.items():
        item['refs'] = sorted(item['refs'])
        try:
            hashed = hashlib.sha256()
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1048576), b''):
                    hashed.update(chunk)
            item.update(sha256=hashed.hexdigest(), status='READABLE')
        except OSError:
            item.update(sha256=None, status='UNREADABLE')
    snapshot = {'schema_version': VERSION, 'db_digest': digest(db),
                'intent': deepcopy(intent or {}), 'evidence': deepcopy(evidence or {}),
                'datasheet_audit': deepcopy(audit),
                'documents': [documents[k] for k in sorted(documents)]}
    snapshot['digest'] = digest(snapshot)
    return snapshot


def _add_targets(target, obj):
    for plural, singulars in (
            ('refs', ('ref',)), ('nodes', ('node',)), ('nets', ('net',)), ('states', ('state',))):
        for key in singulars:
            if text(obj.get(key)):
                target[plural].add(obj[key])
        for key in (plural, 'return_' + plural):
            if strings(obj.get(key)):
                target[plural].update(obj[key])


def dependency_catalog(plan, db, snapshot):
    checks = index_checks(plan)
    declarations = snapshot['intent'].get('review_dependencies', {})
    evidence_checks = {x['id']: x for x in snapshot['evidence'].get('checks', [])}
    result = {}
    for key, check in checks.items():
        targets = {field: set() for field in FIELDS}
        _add_targets(targets, check['object'])
        targets['refs'].update(check.get('required_material_refs') or [])
        if check.get('parent_check_id'):
            targets['check_ids'].add(check['parent_check_id'])
        source = evidence_checks.get(check.get('evidence_check_id'))
        if source:
            _add_targets(targets, source)
            targets['refs'].update(dependency_refs(db, source))
            model = source.get('divider_model') or {}
            targets['nets'].update(model[k] for k in ('source_net', 'reference_net') if text(model.get(k)))
        declaration = declarations.get(key)
        global_scope = (check.get('check', '').startswith(('coverage-', 'feature-'))
                        or bool(check['object'].get('requirement_id')) or is_revision_check(check))
        gaps = [] if global_scope else ['dependency-scope-not-reviewed']
        if declaration:
            for field in FIELDS:
                targets[field].update(declaration.get(field, []))
            bound = (declaration.get('db_digest') == snapshot['db_digest'] and
                     declaration.get('check_digest') == digest(check_spec(check)))
            if declaration['complete'] and bound:
                gaps = []
            elif declaration['complete']:
                gaps = ['stale-dependency-scope-declaration']
        if not global_scope and not any(targets[k] for k in ('refs', 'nodes', 'nets', 'check_ids')):
            gaps.append('no-dependency-anchors')
        for node in targets['nodes']:
            targets['refs'].add(node.rsplit('.', 1)[0])
            if text(db.get('pin2net', {}).get(node)):
                targets['nets'].add(db['pin2net'][node])
            elif node not in db.get('declared_pinname', {}):
                gaps.append('unknown-node:' + node)
        for ref in targets['refs']:
            if ref not in db.get('parts', {}):
                gaps.append('unknown-ref:' + ref)
        for net in targets['nets']:
            if net not in db.get('nets', {}):
                gaps.append('unknown-net:' + net)
        for other in targets['check_ids']:
            if other not in checks or other == key:
                gaps.append('unknown-or-self-check:' + other)
        result[key] = {field: sorted(values) for field, values in targets.items()}
        result[key].update(
            check_digest=digest(check_spec(check)),
            scope='GLOBAL' if global_scope else ('PARTIAL' if gaps else 'DECLARED'),
            gaps=sorted(set(gaps)), citation=declaration.get('citation') if declaration else None,
            evidence_ids=[source['id']] if source else [],
            sources=[d['path'] for d in snapshot['documents']
                     if global_scope or set(d['refs']) & targets['refs']])
    return result


def _event(kind, locator, before, after, refs=(), nodes=(), nets=(), global_scope=False):
    item = {'kind': kind, 'locator': locator, 'old': before, 'new': after,
            'refs': sorted(set(refs)), 'nodes': sorted(set(nodes)), 'nets': sorted(set(nets)),
            'global': global_scope}
    item['id'] = 'CHANGE-' + digest(item)
    return item


def _coordinates(old_db, new_db, refs=(), nodes=(), nets=()):
    refs, nodes, nets = set(refs), set(nodes), set(nets)
    for db in (old_db, new_db):
        for node, net in db.get('pin2net', {}).items():
            if node in nodes or node.rsplit('.', 1)[0] in refs or net in nets:
                nodes.add(node)
                nets.add(net)
        for net in tuple(nets):
            nodes.update(db.get('nets', {}).get(net, []))
    refs.update(n.rsplit('.', 1)[0] for n in nodes)
    return {'refs': refs, 'nodes': nodes, 'nets': nets}


def collect_changes(old_db, db, old_inputs, inputs):
    changes = []
    mapped = ('parts', 'pin2net', 'pinname', 'pintype', 'declared_pinname', 'nets', 'ref2page')
    for field in mapped:
        before, after = old_db.get(field, {}), db.get(field, {})
        for key in sorted(set(before) | set(after)):
            bv, av = before.get(key), after.get(key)
            if digest(bv) == digest(av) and (key in before) == (key in after):
                continue
            coordinates = _coordinates(old_db, db,
                refs=[key] if field in ('parts', 'ref2page') else [],
                nodes=[key] if field in ('pin2net', 'pinname', 'pintype', 'declared_pinname') else [],
                nets=[key] if field == 'nets' else [])
            changes.append(_event('db.' + field, key, bv, av, **coordinates))
    for field in sorted((set(old_db) | set(db)) - set(mapped)):
        if digest(old_db.get(field)) != digest(db.get(field)):
            changes.append(_event('db.metadata', field, old_db.get(field), db.get(field), global_scope=True))
    if old_inputs is None:
        return changes
    # Intent can encode arbitrary upstream/downstream requirements. Without a
    # domain-specific dependency proof, changed conditions require full review.
    for field in sorted(set(old_inputs['intent']) | set(inputs['intent'])):
        if field in ('review_mode', 'review_dependencies'):
            continue
        before, after = old_inputs['intent'].get(field), inputs['intent'].get(field)
        if digest(before) != digest(after):
            changes.append(_event('intent', field, before, after, global_scope=True))
    for field in ('evidence', 'datasheet_audit'):
        if digest(old_inputs[field]) != digest(inputs[field]):
            changes.append(_event(field, field, old_inputs[field], inputs[field], global_scope=True))
    before = {x['path']: x for x in old_inputs['documents']}
    after = {x['path']: x for x in inputs['documents']}
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            refs = set((before.get(path) or {}).get('refs', [])) | set((after.get(path) or {}).get('refs', []))
            changes.append(_event('document', path, before.get(path), after.get(path), refs=refs,
                                  global_scope=not refs or any((side.get(path) or {}).get('global')
                                                             for side in (before, after))))
    return changes


def _review_check(key, kind, obj, criterion):
    return {'id': key, 'check': kind, 'rule': 'Rule-17', 'object': obj,
            'criterion': criterion, 'applicability': 'APPLICABLE', 'stage': 'ER7',
            'executor': 'Expert Review', 'readiness': 'WAITING_EVIDENCE',
            'required_inputs': ['current revision re-verification evidence'],
            'trigger': ['revision-impact'], 'review_result': None,
            'evidence_confidence': None, 'handoff': {'required': False}}


def _baseline(old_db, old_plan):
    gaps, old_inputs, old_dependencies = [], None, {}
    if old_db is None:
        gaps.append('missing-old-db')
    if old_plan is None:
        gaps.append('missing-old-plan')
    if old_db is not None and old_plan is not None:
        index_checks(old_plan)
        if old_plan.get('db_sha256') != db_fingerprint(old_db):
            raise ValueError('old plan is not bound to the supplied old db')
        if (type(old_plan.get('dependency_version')) is not int or old_plan['dependency_version'] != VERSION
                or 'review_inputs' not in old_plan):
            gaps.append('legacy-old-plan-has-no-input-snapshot')
        else:
            old_inputs = old_plan['review_inputs']
            _verify_frozen_inputs(old_inputs, old_db)
            old_dependencies = dependency_catalog(old_plan, old_db, old_inputs)
            if old_dependencies != old_plan.get('check_dependencies'):
                raise ValueError('old plan dependency catalog is modified or incomplete')
            gaps += ['old-unmatched-dependency-declaration:' + key for key in sorted(
                set(old_inputs['intent'].get('review_dependencies', {})) - set(old_dependencies))]
            gaps += ['unreadable-old-document:' + x['path'] for x in old_inputs['documents']
                     if x.get('status') != 'READABLE']
    return gaps, old_inputs, old_dependencies


def _verify_frozen_inputs(inputs, db):
    if not isinstance(inputs, dict) or type(inputs.get('schema_version')) is not int or inputs['schema_version'] != VERSION:
        raise ValueError('invalid revision input snapshot version')
    if inputs.get('db_digest') != digest(db):
        raise ValueError('revision input snapshot db digest mismatch')
    if inputs.get('digest') != digest({k: v for k, v in inputs.items() if k != 'digest'}):
        raise ValueError('revision input snapshot digest mismatch')
    for field in ('intent', 'evidence'):
        if not isinstance(inputs.get(field), dict):
            raise ValueError('revision snapshot.' + field + ' must be an object')
    if not isinstance(inputs.get('documents'), list):
        raise ValueError('revision snapshot.documents must be an array')
    errors = validate_declarations(inputs.get('intent'))
    if errors:
        raise ValueError('; '.join(errors))


def build_impact(plan, db, old_db=None, old_plan=None):
    gaps, old_inputs, old_dependencies = _baseline(old_db, old_plan)
    inputs, dependencies = plan['review_inputs'], plan['check_dependencies']
    old_checks = index_checks(old_plan) if old_plan is not None else {}
    checks = index_checks(plan)
    changes = collect_changes(old_db, db, old_inputs, inputs) if old_db is not None else []
    gaps += ['unreadable-current-document:' + x['path'] for x in inputs['documents'] if x['status'] != 'READABLE']
    gaps += ['unmatched-dependency-declaration:' + key for key in plan.get('dependency_unmatched', [])]
    reasons, matched = {}, {}
    for key, check in checks.items():
        dep = dependencies[key]
        previous = old_dependencies.get(key, {})
        why, change_ids = [], []
        if key not in old_checks:
            why.append('new-check')
        elif digest(check_spec(old_checks[key])) != digest(check_spec(check)):
            why.append('check-definition-changed')
        # A renewed declaration's citation/digest is not a changed electrical
        # dependency. The definition and actual dependency sets are compared.
        if previous and any(dep.get(k) != previous.get(k) for k in (*FIELDS, 'sources', 'evidence_ids', 'scope', 'gaps')):
            why.append('dependency-scope-changed')
        for change in changes:
            if (change['global'] or dep['scope'] == 'GLOBAL' or previous.get('scope') == 'GLOBAL'
                    or any(set(change[field]) & (set(dep[field]) | set(previous.get(field, [])))
                           for field in ('refs', 'nodes', 'nets'))):
                change_ids.append(change['id'])
        if change_ids:
            why.append('changed-dependency')
        reasons[key], matched[key] = why, change_ids
    partial = sorted(key for key, dep in dependencies.items() if dep['gaps'])
    partial += ['old:' + key for key, dep in old_dependencies.items() if dep['gaps']]
    definition_changes = any(why for key, why in reasons.items() if not is_revision_check(checks[key]))
    full = bool(gaps or ((changes or definition_changes) and
                        (partial or any(c['global'] for c in changes))))
    # Propagate explicit check-to-check dependencies to a fixed point, including
    # cycles. Only exact IDs participate; no inferred unbounded circuit traversal.
    changed = True
    while changed:
        changed = False
        for key, dep in dependencies.items():
            upstream = set(dep['check_ids']) | set(old_dependencies.get(key, {}).get('check_ids', []))
            for other in sorted(upstream):
                reason = 'dependent-check:' + other
                if reasons.get(other) and reason not in reasons[key]:
                    reasons[key].append(reason)
                    changed = True
    entries = []
    for key in sorted(checks):
        why = reasons[key]
        if full:
            why.append('full-review-fallback')
        if key == COVERAGE_ID:
            why.append('revision-scope-audit')
        entries.append({'check_id': key, 'required': bool(why),
                        'status': 'REVERIFY' if why else 'NO_RECORDED_CHANGE',
                        'reasons': sorted(set(why)), 'change_ids': sorted(set(matched[key])),
                        'check_digest': dependencies[key]['check_digest']})
    impact = {'schema_version': VERSION,
              'baseline': {'db_digest': digest(old_db) if old_db is not None else None,
                           'plan_digest': digest(old_plan) if old_plan is not None else None},
              'current': {'db_digest': digest(db), 'inputs_digest': inputs['digest'],
                          'checks_digest': digest([check_spec(checks[k]) for k in sorted(checks)])},
              'strategy': 'FULL_REVIEW' if full else 'EXACT_DEPENDENCIES',
              'scope': 'Routing only. NO_RECORDED_CHANGE is not automatic reuse or electrical approval.',
              'blocking_gaps': sorted(set(gaps)), 'partial_dependencies': sorted(set(partial)),
              'changes': changes, 'entries': entries,
              'structural_diff': diff_databases(old_db, db) if old_db is not None else None}
    impact['digest'] = digest(impact)
    return impact


def attach_metadata(plan, db, intent=None, evidence=None, audit=None, old_db=None, old_plan=None):
    """Called after same-revision cold/hot/manual merging; never reads results."""
    if old_plan is not None and old_db is None:
        raise ValueError('old_plan requires old_db')
    if old_db is not None and not isinstance(old_db, dict):
        raise ValueError('old_db must be an object')
    if old_db is not None:
        errors = validate_db_shape(old_db)
        for field in ('declared_pinname', 'ref2page'):
            if field in old_db and not isinstance(old_db[field], dict):
                errors.append('old_db.' + field + ' must be an object')
        if errors:
            raise ValueError('old_db: ' + '; '.join(errors))
    if old_plan is not None and not isinstance(old_plan, dict):
        raise ValueError('old_plan must be an object')
    if (old_db is not None or old_plan is not None) and plan['review_mode'] != 'revision':
        raise ValueError('revision baseline cannot be used in first-review mode')
    current_all = index_checks(plan)
    carry = {c['object'].get('prior_check_id') for c in current_all.values()
             if c.get('check') == 'revision-removed-check'}
    if plan['review_mode'] != 'revision' and any(is_revision_check(c) for c in current_all.values()):
        raise ValueError('revision checks cannot be merged into first-review mode')
    plan['checks'] = [c for c in plan['checks'] if not is_revision_check(c)]
    if plan['review_mode'] == 'revision':
        current = index_checks(plan)
        if COVERAGE_ID in current:
            raise ValueError('reserved revision coverage ID')
        if old_plan is not None:
            old_checks = index_checks(old_plan)
            known_history = {c['object'].get('prior_check_id') for c in old_checks.values()
                             if c.get('check') == 'revision-removed-check'}
            if carry - set(old_checks) - known_history:
                raise ValueError('carried revision disposition has no matching old baseline check')
            for key, check in sorted(old_checks.items()):
                if (key in current and key not in carry) or check.get('check') == 'revision-impact-coverage':
                    continue
                if check.get('check') == 'revision-removed-check':
                    # Preserve the historical disposition record on later
                    # revisions too, but never carry its annotated verdict.
                    retained = deepcopy(check)
                    retained['review_result'] = None
                    retained['evidence_confidence'] = None
                    plan['checks'].append(retained)
                    continue
                removed_id = 'ER7.revision-removed.' + digest(key)
                if removed_id in current:
                    raise ValueError('reserved removed-check ID')
                removed = _review_check(removed_id, 'revision-removed-check',
                    {'feature': key, 'prior_check_id': key},
                    '核对旧项在本版冷/热阶段的消失、恢复或替代及连带影响，不能以清单增减证明修复。')
                removed['prior_check'] = check_spec(check)
                plan['checks'].append(removed)
        plan['checks'].append(_review_check(COVERAGE_ID, 'revision-impact-coverage',
            {'feature': 'revision-impact'},
            '核对新旧基线、变化、依赖缺口及扩大复验范围；全部必需项使用本轮证据，不迁移旧结论。'))
    plan['checks'].sort(key=lambda c: c['id'])
    plan['dependency_version'] = VERSION
    plan['review_inputs'] = input_snapshot(db, intent, evidence, audit)
    plan['check_dependencies'] = dependency_catalog(plan, db, plan['review_inputs'])
    plan['dependency_unmatched'] = sorted(set((intent or {}).get('review_dependencies', {})) -
                                          set(plan['check_dependencies']))
    if plan['review_mode'] == 'revision':
        plan['revision_impact_version'] = VERSION
        plan['revision_impact'] = build_impact(plan, db, old_db, old_plan)
    return plan


def validate_metadata(plan, db, old_db=None, old_plan=None, require_revision=False):
    """Recompute from current db, frozen context, live documents and old baseline."""
    errors = []
    enabled = any(k in plan for k in ('dependency_version', 'review_inputs', 'check_dependencies'))
    checks = plan.get('checks')
    revision = (require_revision or any(k in plan for k in ('revision_impact_version', 'revision_impact'))
                or any(is_revision_check(c) for c in (checks if isinstance(checks, list) else []) if isinstance(c, dict))
                or (enabled and plan.get('review_mode') == 'revision'))
    if not enabled and not revision:
        return errors, None
    try:
        if db is None:
            raise ValueError('dependency/revision validation requires --db')
        if type(plan.get('dependency_version')) is not int or plan['dependency_version'] != VERSION:
            raise ValueError('dependency_version must be 1')
        inputs = plan['review_inputs']
        _verify_frozen_inputs(inputs, db)
        if revision and plan.get('review_mode') != 'revision':
            raise ValueError('revision impact requires revision review_mode')
        expected = attach_metadata(deepcopy(plan), db, inputs['intent'], inputs['evidence'],
                                   inputs['datasheet_audit'], old_db, old_plan)
        for key in ('review_inputs', 'check_dependencies', 'dependency_unmatched'):
            if plan.get(key) != expected[key]:
                errors.append(key + ': stale or modified dependency/input snapshot')
        if revision:
            if type(plan.get('revision_impact_version')) is not int or plan['revision_impact_version'] != VERSION:
                errors.append('revision_impact_version must be 1')
            if plan.get('revision_impact') != expected.get('revision_impact'):
                errors.append('revision impact stale/modified; supply exact --old-db and --old-plan')
            actual_checks, expected_checks = index_checks(plan), index_checks(expected)
            if {k for k, c in actual_checks.items() if is_revision_check(c)} != {
                    k for k, c in expected_checks.items() if is_revision_check(c)}:
                errors.append('revision disposition check set is incomplete or unexpected')
            for key, check in expected_checks.items():
                if is_revision_check(check) and actual_checks.get(key) != check:
                    errors.append(key + ': missing/modified revision disposition check')
        return errors, expected.get('revision_impact')
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        return errors + ['invalid revision metadata: ' + str(exc)], None


def validate_reverification(plan, report, impact):
    errors = []
    if type(report.get('revision_impact_version')) is not int or report['revision_impact_version'] != VERSION:
        errors.append('results.revision_impact_version must be 1')
    if report.get('revision_digest') != impact['digest']:
        errors.append('results.revision_digest mismatch')
    checks = report.get('checks')
    if not isinstance(checks, list):
        return errors + ['revision results.checks must be an array']
    rows = {c['id']: c for c in checks if isinstance(c, dict) and text(c.get('id'))}
    for entry in impact['entries']:
        if not entry['required']:
            continue
        key = entry['check_id']
        row = rows.get(key, {})
        record = row.get('reverification')
        if not isinstance(record, dict):
            errors.append(key + ': current re-verification record required; no old verdict transfer')
            continue
        wanted = {'revision_digest': impact['digest'], 'inputs_digest': plan['review_inputs']['digest'],
                  'check_digest': entry['check_digest']}
        if any(record.get(k) != v for k, v in wanted.items()):
            errors.append(key + ': stale re-verification binding')
        sources = record.get('evidence')
        if not text(record.get('method')) or not (isinstance(sources, list) and sources and all(
                isinstance(x, dict) and text(x.get('source')) and text(x.get('locator')) for x in sources)):
            errors.append(key + ': re-verification needs method and locatable current evidence')
    if impact['blocking_gaps'] and rows.get(COVERAGE_ID, {}).get('review_result') != 'INSUFFICIENT':
        errors.append(COVERAGE_ID + ': unresolved baseline/document gaps must remain INSUFFICIENT')
    return errors
