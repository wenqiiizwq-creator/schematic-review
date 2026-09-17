#!/usr/bin/env python3
"""Independent Rule-08 oracle. Freeze never imports production or benchmarks."""
import argparse
import copy
from fractions import Fraction as Q
import hashlib
from itertools import product
import json
import math
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'frozen_cases.json'
SOURCE, FB, GND = 'VOUT_TEST', 'FB_TEST', 'GND'


def fraction(value):
    return Q(str(value))


def R(ref, a, b, ohm, tol='0.01', populated=True):
    return dict(ref=ref, a=a, b=b, ohm=str(ohm), tol=tol, populated=populated,
                value=f'{ohm}R/{fraction(tol) * 100}%')


def database(edges, extras=()):
    nets = {FB: ['U1.1']}
    parts = {'U1': {'value': 'SYNTHETIC-INDEPENDENT-FB', 'nc': False}}
    for edge in edges:
        ref = edge['ref']
        parts[ref] = dict(value=edge['value'], nc=not edge['populated'])
        for pin, net in enumerate((edge['a'], edge['b']), 1):
            nets.setdefault(net, []).append(f'{ref}.{pin}')
    for ref, value, endpoints in extras:
        parts[ref] = dict(value=value, nc=False)
        for pin, net in enumerate(endpoints, 1):
            nets.setdefault(net, []).append(f'{ref}.{pin}')
    return dict(nets=nets, parts=parts,
                pin2net={node: net for net, nodes in nets.items() for node in nodes},
                pinname={'U1.1': 'FB'}, pintype={}, ref2page={}, pseudo_nets=[])


def resistor_corners(edges):
    active = [e for e in edges if e['populated']]
    choices = []
    for e in active:
        nominal, tol = fraction(e['ohm']), fraction(e['tol'])
        choices.append(sorted({nominal * (1 - tol), nominal * (1 + tol)}))
    for values in product(*choices):
        yield dict(zip((e['ref'] for e in active), values))


def simple(r, v, i):
    # KCL at F: (F-O)/Ru + F/Rl + Ib = 0.
    return v * (1 + r['R1'] / r['R2']) + i * r['R1']


def upper_trunk(r, v, i):
    ru = r['R1'] + 1 / (1 / r['R2'] + 1 / r['R3'])
    return v * (1 + ru / r['R4']) + i * ru


def lower_trunk(r, v, i):
    rl = 1 / (1 / r['R2'] + 1 / r['R3']) + r['R4']
    return v * (1 + r['R1'] / rl) + i * r['R1']


def bridge(r, v, i):
    # R1 O-A, R2 A-G, R3 O-F, R4 F-G, R5 A-F.
    # KCL(A): A=(g1*O+g5*F)/S; S=g1+g2+g5.
    # Substitution in KCL(F) gives this scalar elimination.
    g1, g2, g3, g4, g5 = (1 / r[f'R{j}'] for j in range(1, 6))
    s = g1 + g2 + g5
    return (v * (g3 + g4 + g5 - g5 * g5 / s) + i) / (g3 + g1 * g5 / s)


def independent_window(edges, equation, vref, bias):
    values = [equation(r, fraction(v), fraction(i)) for r in resistor_corners(edges)
              for v in (vref['min'], vref['max']) for i in (bias['min'], bias['max'])]
    nominal = {e['ref']: fraction(e['ohm']) for e in edges if e['populated']}
    return dict(min=float(min(values)), max=float(max(values)),
                typ=float(equation(nominal, fraction(vref['typ']), Q(0))))


def case(num, description, edges, verdict, bounds, equation=None,
         vref=(.594, .6, .606), bias=(0, 0), extras=(), ignored=None, basis=''):
    identifier = f'IR{num:02d}'
    db = database(edges, extras)
    vref = dict(zip(('min', 'typ', 'max'), vref))
    bias = dict(zip(('min', 'max'), bias))
    model = dict(source_net=SOURCE, reference_net=GND, bias_current_a=bias,
                 ignored_nodes={'U1.1': 'Synthetic FB input, all current in bias_current_a.'})
    model['ignored_nodes'].update(ignored or {})
    check = dict(id=identifier, rule='Rule-08', kind='divider', net=FB, node='U1.1',
                 vref=vref, expected=dict(zip(('min', 'max'), bounds)), divider_model=model,
                 depends_on=sorted(ref for ref, p in db['parts'].items() if not p['nc']),
                 citation='Independent synthetic circuit: ' + description)
    expected = dict(review_result=verdict)
    if equation:
        expected['calculation'] = independent_window(edges, equation, vref, bias)
    return dict(id=identifier, description=description, db=db, check=check,
                expected=expected, oracle_basis=basis, resistor_definition=edges)


def generate():
    s = [R('R1', SOURCE, FB, 27000, '.02'), R('R2', FB, GND, 9000)]
    equation_basis = 'KCL: O=F*(1+Ru/Rl)+Ib*Ru. Exact rational independent endpoint enumeration; typ has zero bias.'
    bridge_basis = ('Eliminate A from KCL(A) and KCL(F): O=[F*(g3+g4+g5-g5^2/S)+Ib]/'
                    '(g3+g1*g5/S), S=g1+g2+g5. Nominal gain=29/13, bias coefficient=140000/13 ohms.')
    c = [case(1, 'Simple divider, asymmetric signed bias', s, 'PASS', (2.2, 2.6), simple,
              bias=(-2e-6, 3e-6), basis=equation_basis),
         case(2, 'Nominal correct, guaranteed window fails', s, 'FAIL', (2.39, 2.41), simple, basis=equation_basis)]
    u = [R('R1', FB, 'X', 6000, '.03'), R('R2', 'X', SOURCE, 12000),
         R('R3', 'X', SOURCE, 12000), R('R4', FB, GND, 8000, '.02')]
    c.append(case(3, 'Shared upper series trunk', u, 'PASS', (1.65, 1.86), upper_trunk,
                  vref=(.693, .7, .707), basis='Ru=R1+(R2||R3), Rl=R4. ' + equation_basis))
    l = [R('R1', SOURCE, FB, 20000), R('R2', FB, 'X', 15000),
         R('R3', FB, 'X', 15000), R('R4', 'X', GND, 2500)]
    c.append(case(4, 'Shared lower return trunk and input bias', l, 'PASS', (2.54, 2.87), lower_trunk,
                  vref=(.891, .9, .909), bias=(-2e-6, 4e-6),
                  basis='Rl=(R2||R3)+R4, Ru=R1. ' + equation_basis))
    b = [R('R1', SOURCE, 'A', 10000), R('R2', 'A', GND, 30000),
         R('R3', SOURCE, FB, 20000), R('R4', FB, GND, 10000), R('R5', 'A', FB, 10000)]
    vr = (.6435, .65, .6565)
    c.append(case(5, 'Unbalanced mesh with signed input bias', b, 'PASS', (1.37, 1.56),
                  bridge, vref=vr, bias=(-2e-6, 5e-6), basis=bridge_basis))
    c.append(case(6, 'Mesh nominal passes but tolerance window fails', b, 'FAIL', (1.44, 1.46),
                  bridge, vref=vr, basis=bridge_basis))
    c.append(case(7, 'Large negative input current reverses resistor sensitivity', b, 'FAIL', (1.3, 1.6),
                  bridge, vref=vr, bias=(-100e-6, -60e-6), basis=bridge_basis))
    grid, n = [], 1
    for row in range(3):
        left, right = f'A{row}', FB if row == 1 else f'B{row}'
        for a, d in ((SOURCE, left), (left, right), (right, GND)):
            grid.append(R(f'R{n}', a, d, 1000, '0'))
            n += 1
    for row in range(2):
        grid.append(R(f'R{n}', f'A{row}', f'A{row+1}', 7000, '0'))
        n += 1
        grid.append(R(f'R{n}', FB if row == 1 else f'B{row}',
                      FB if row+1 == 1 else f'B{row+1}', 11000, '0'))
        n += 1
    c.append(case(8, 'Six internal nodes, three row resistor grid', grid, 'PASS', (2.669, 2.731),
                  lambda r, v, i: 3*v, vref=(.89, .9, .91),
                  basis='Equal three-resistor horizontal strings: A=2O/3, B=O/3. Vertical edges carry zero current. F=B1 => O=3F. Exact resistors, zero bias.'))
    c.append(case(9, 'DNP 1 ohm shunt must not load feedback', s+[R('R99', FB, GND, 1, populated=False)],
                  'PASS', (2.2, 2.6), simple, basis='DNP is open circuit. ' + equation_basis))
    dnp = copy.deepcopy(s)
    dnp[0]['populated'] = False
    c.append(case(10, 'Only source arm is DNP', dnp, 'INSUFFICIENT', (2.2, 2.6),
                  basis='No conducting O-F path; output cannot control positive reference.'))
    mt = copy.deepcopy(s)
    mt[0].update(value='27000R', tol=None)
    c.append(case(11, 'Missing upper resistor tolerance', mt, 'INSUFFICIENT', (2.2, 2.6),
                  basis='Nominal resistance alone gives no guaranteed window.'))
    mb = copy.deepcopy(b)
    mb[4].update(value='10000R', tol=None)
    c.append(case(12, 'Missing loaded bridge resistor tolerance', mb, 'INSUFFICIENT', (1.37, 1.56),
                  basis='Nonzero R5 current and absent tolerance leave mesh gain unbounded.'))
    c.append(case(13, 'Third power rail', s+[R('R3', FB, 'VDD_AUX_5V', 100000)],
                  'INSUFFICIENT', (2.2, 2.6), basis='KCL requires third rail voltage/state not supplied in model.'))
    c.append(case(14, 'Separate reference domain', s+[R('R3', FB, 'AGND', 100000)],
                  'INSUFFICIENT', (2.2, 2.6), basis='AGND not proven equipotential with GND.'))
    c.append(case(15, 'MOSFET cannot be waived with ignore prose', s+[R('R3', FB, 'DRAIN', 15000)],
                  'INSUFFICIENT', (2.2, 2.6), extras=[('Q1', 'SYNTHETIC-MOS', ['DRAIN', GND, 'CTRL'])],
                  ignored={'Q1.1': 'Ignore with no transistor state model.'},
                  basis='Transistor state/I-V absent; prose is not a physical model.'))
    c.append(case(16, 'Nonlinear diode clamp', s, 'INSUFFICIENT', (2.2, 2.6),
                  extras=[('D1', 'SYNTHETIC-LED', [FB, GND])],
                  ignored={'D1.1': 'Assume no current without I-V guarantee.'},
                  basis='Diode I-V/state absent; unknown current changes required output.'))
    unk = copy.deepcopy(s)
    unk[0]['value'] = 'UNKNOWN'
    c.append(case(17, 'Unknown critical resistance', unk, 'INSUFFICIENT', (2.2, 2.6),
                  basis='Gain cannot be established without upper resistance.'))
    c.append(case(18, 'Vref has nominal only', s, 'INSUFFICIENT', (2.2, 2.6),
                  basis='Nominal Vref does not bound manufacturing and operating variation.'))
    c[-1]['check']['vref'] = .6
    c.append(case(19, 'Feedback shorted to ground by zero ohms', s+[R('R3', FB, GND, 0, '0')],
                  'INSUFFICIENT', (2.2, 2.6), basis='F=0 contradicts F>=.594; no finite divider solution.'))
    c.append(case(20, 'Open ended passive stub has zero DC current', s+[R('R3', FB, 'STUB', 33000)],
                  'PASS', (2.2, 2.6), simple, basis='KCL(STUB): Vstub=F. ' + equation_basis))
    c.append(case(21, 'Missing bias-current bounds', s, 'INSUFFICIENT', (2.2, 2.6),
                  basis='Unknown FB input current leaves output unbounded.'))
    del c[-1]['check']['divider_model']['bias_current_a']
    c.append(case(22, 'FB pin not explicitly bound to load model', s, 'INSUFFICIENT', (2.2, 2.6),
                  basis='IC pin must be tied to the stated input-current model.'))
    c[-1]['check']['divider_model']['ignored_nodes'] = {}
    return c


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze():
    if MANIFEST.exists():
        raise SystemExit('Refusing to overwrite frozen oracle.')
    cases = generate()
    for c in cases:
        if 'calculation' in c['expected']:
            w, limit = c['expected']['calculation'], c['check']['expected']
            verdict = 'PASS' if w['min'] >= limit['min'] and w['max'] <= limit['max'] else 'FAIL'
            assert verdict == c['expected']['review_result'], c['id']
    payload = dict(schema_version=1, runner_sha256=digest(__file__), cases=cases,
                   oracle='Independent scalar KCL elimination and exact rational endpoints',
                   implementation_reads_before_freeze=[
                       'before/scripts/solve_dividers.py', 'before/scripts/lint.py',
                       'before/scripts/electrical_contract.py',
                       'before/scripts/tests/electrical_fixtures.py',
                       'before/scripts/tests/test_electrical_safety.py',
                       'before/scripts/tests/test_solve_dividers.py'],
                   prohibited_sources_read=[])
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)+'\n')
    print(json.dumps(dict(case_count=len(cases), runner_sha256=digest(__file__),
                         manifest_sha256=digest(MANIFEST), manifest_path=str(MANIFEST),
                         cases=[dict(id=c['id'], expected=c['expected']) for c in cases]), indent=2))


def verify(source, out):
    frozen = json.loads(MANIFEST.read_text())
    if digest(__file__) != frozen['runner_sha256']:
        raise SystemExit('Runner changed since freeze.')
    out = out.resolve()
    if not out.is_relative_to(ROOT):
        raise SystemExit('All output must remain in independent-regression.')
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(source.resolve()/'scripts'))
    sys.path.insert(0, str(ROOT.parent/'before'/'scripts'/'tests'))
    from electrical_fixtures import bind_evidence
    from lint import Lint, validate_evidence
    rows = []
    for item in frozen['cases']:
        c = copy.deepcopy(item)
        evidence = dict(schema_version=1, checks=[c['check']])
        target = out/c['id']
        target.mkdir(parents=True, exist_ok=True)
        row = dict(id=c['id'], description=c['description'], expected=c['expected'])
        try:
            audit = bind_evidence(c['db'], evidence, target)
            errors = validate_evidence(evidence)
            if errors:
                raise ValueError('Invalid fixture: '+repr(errors))
            lint = Lint(c['db'], evidence=evidence, datasheet_audit=audit)
            lint.run()
            if len(lint.results) != 1:
                raise AssertionError(f'Expected one result, got {len(lint.results)}')
            actual, problems = lint.results[0], []
            row['actual'] = actual
            if actual.get('review_result') != c['expected']['review_result']:
                problems.append('Decision mismatch')
            for key, value in c['expected'].get('calculation', {}).items():
                got = actual.get('calculation', {}).get(key)
                if not isinstance(got, (float, int)) or not math.isclose(got, value, rel_tol=2e-9, abs_tol=1e-9):
                    problems.append(f'{key}: expected {value!r}, got {got!r}')
            row.update(passed=not problems, problems=problems)
            for name, data in [('evidence', evidence), ('audit', audit), ('db', c['db'])]:
                (target/(name+'.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
        except Exception:
            row.update(passed=False, problems=[traceback.format_exc()])
        rows.append(row)
    result = dict(source=str(source.resolve()), case_count=len(rows),
                  passed=sum(r['passed'] for r in rows), failed=sum(not r['passed'] for r in rows),
                  runner_sha256=digest(__file__), manifest_sha256=digest(MANIFEST), results=rows)
    (out/'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)+'\n')
    print(json.dumps({k: result[k] for k in ('source', 'case_count', 'passed', 'failed', 'manifest_sha256')}, indent=2))
    for row in rows:
        if not row['passed']:
            print(json.dumps(row, ensure_ascii=False))
    print('report='+str(out/'report.json'))
    return 0 if not result['failed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.freeze:
        if args.source or args.out:
            parser.error('Freeze does not execute a source tree.')
        freeze()
        return 0
    if not args.source or not args.out:
        parser.error('Need --source and --out.')
    return verify(args.source, args.out)


if __name__ == '__main__':
    raise SystemExit(main())
