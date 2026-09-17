#!/usr/bin/env python3
"""Check frozen scalar circuit answers by reapplying predicted outputs in ngspice."""
import json
from pathlib import Path
import re
import subprocess
import independent_rule08 as independent

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'oracle-ngspice'
OUT.mkdir(exist_ok=True)
data = json.loads(independent.MANIFEST.read_text())
assert data['runner_sha256'] == independent.digest(independent.__file__)
equations = {'IR01': independent.simple, 'IR02': independent.simple,
             'IR03': independent.upper_trunk, 'IR04': independent.lower_trunk,
             'IR05': independent.bridge, 'IR06': independent.bridge,
             'IR07': independent.bridge, 'IR08': lambda r, v, i: 3*v,
             'IR09': independent.simple, 'IR20': independent.simple}


def net(name):
    return {independent.GND: '0', independent.SOURCE: 'out',
            independent.FB: 'fb'}.get(name, name.lower())


rows = []
for case in data['cases']:
    if case['id'] not in equations:
        continue
    equation, edges = equations[case['id']], case['resistor_definition']
    vref, bias = case['check']['vref'], case['check']['divider_model']['bias_current_a']
    options = [(equation(r, independent.fraction(v), independent.fraction(i)), r,
                independent.fraction(v), independent.fraction(i))
               for r in independent.resistor_corners(edges)
               for v in (vref['min'], vref['max']) for i in (bias['min'], bias['max'])]
    nominal_r = {e['ref']: independent.fraction(e['ohm']) for e in edges if e['populated']}
    nominal_v = independent.fraction(vref['typ'])
    probes = [('min', min(options, key=lambda entry: entry[0])),
              ('typ', (equation(nominal_r, nominal_v, 0), nominal_r, nominal_v, 0)),
              ('max', max(options, key=lambda entry: entry[0]))]
    for label, (voltage, resistances, required_fb, current) in probes:
        identifier = case['id'] + '-' + label
        lines = ['Independent oracle verification ' + identifier,
                 f'Vout out 0 {float(voltage):.17g}', f'Ibias fb 0 {float(current):.17g}']
        for edge in edges:
            if edge['populated']:
                lines.append(f"{edge['ref']} {net(edge['a'])} {net(edge['b'])} {float(resistances[edge['ref']]):.17g}")
        lines.extend(['.control', 'set numdgt=15', 'op', 'print v(fb)', 'quit', '.endc', '.end'])
        deck = OUT / (identifier + '.cir')
        deck.write_text('\n'.join(lines) + '\n')
        run = subprocess.run(['/opt/homebrew/bin/ngspice', '-b', str(deck)], text=True,
                             capture_output=True, cwd=OUT)
        (OUT / (identifier + '.log')).write_text(run.stdout + '\n' + run.stderr)
        match = re.search(r'v\(fb\)\s*=\s*([+\-\deE.]+)', run.stdout, re.I)
        measured = float(match.group(1)) if match else None
        error = abs(measured-float(required_fb)) if measured is not None else None
        rows.append(dict(id=identifier, imposed_output_v=float(voltage),
                         required_feedback_v=float(required_fb), measured_feedback_v=measured,
                         feedback_error_v=error, passed=run.returncode == 0 and error is not None and error < 1e-10))

report = dict(manifest_sha256=independent.digest(independent.MANIFEST),
              probe_count=len(rows), passed=sum(row['passed'] for row in rows),
              max_feedback_error_v=max(row['feedback_error_v'] for row in rows if row['feedback_error_v'] is not None),
              results=rows)
(OUT / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
print(json.dumps({key: value for key, value in report.items() if key != 'results'}, indent=2))
raise SystemExit(0 if all(row['passed'] for row in rows) else 1)
