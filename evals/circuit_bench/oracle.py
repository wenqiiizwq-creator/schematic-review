"""Independent electrical oracle: nodal KCL and 50-digit RC exponentials.

Never imports the checker. Bounds apply only to the stated linear, ideal models.
"""
from decimal import Decimal, localcontext
from itertools import product


def solve_linear(matrix, rhs):
    a = [list(map(float, row)) + [float(b)] for row, b in zip(matrix, rhs)]
    n = len(a)
    for col in range(n):
        pivot = max(range(col, n), key=lambda i: abs(a[i][col]))
        if abs(a[pivot][col]) < 1e-16:
            raise ValueError('Singular or disconnected reference circuit')
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [x / scale for x in a[col]]
        for row in range(n):
            if row != col:
                scale = a[row][col]
                a[row] = [x - scale * y for x, y in zip(a[row], a[col])]
    return [row[-1] for row in a]


def nodal(branches, known, rows, unknown, injection=None):
    injection = injection or {}
    matrix, rhs = [], []
    for node in rows:
        coefficients = dict.fromkeys(unknown, 0.0)
        constant = injection.get(node, 0.0)
        for r in branches:
            if node not in (r['a'], r['b']):
                continue
            other = r['b'] if node == r['a'] else r['a']
            for endpoint, sign in ((node, 1), (other, -1)):
                conductance = sign / r['ohm']
                if endpoint in coefficients:
                    coefficients[endpoint] += conductance
                else:
                    constant -= conductance * known[endpoint]
        matrix.append([coefficients[n] for n in unknown])
        rhs.append(constant)
    return dict(zip(unknown, solve_linear(matrix, rhs)))


def active(case):
    return [r for r in case['resistors'] if r['fitted']]


def resistor_corners(case):
    resistors = active(case)
    for signs in product((-1, 1), repeat=len(resistors)):
        yield [dict(r, ohm=r['ohm'] * (1 + sign * r['tol']))
               for r, sign in zip(resistors, signs)]


def feedback_value(case, resistors, vref, bias):
    nodes = {n for r in resistors for n in (r['a'], r['b'])}
    unknown = sorted(nodes - {'FB', 'GND'})
    rows = sorted(nodes - {'OUT', 'GND'})
    return nodal(resistors, {'FB': vref, 'GND': 0.0}, rows, unknown,
                 {'FB': -bias})['OUT']


def pull_value(resistors):
    return nodal(resistors, {'SUPPLY': 0.0}, ['BUS'], ['BUS'], {'BUS': 1.0})['BUS']


def rc_value(resistance, capacitance, supply, initial, sample):
    with localcontext() as context:
        context.prec = 50
        r, c, vf, v0, t = map(lambda v: Decimal(str(v)),
                              (resistance, capacitance, supply, initial, sample))
        return float(vf + (v0 - vf) * (-t / (r * c)).exp())


def missing_inputs(case):
    missing = []
    if any(r['tol'] is None for r in active(case)):
        missing.append('resistor tolerance')
    if case['kind'] == 'feedback' and case['vref_bounds'] is None:
        missing.append('reference bounds')
    if case['kind'] == 'rc':
        if case['cap_tol'] is None:
            missing.append('capacitance tolerance')
        if not case['voltage_evidence']:
            missing.append('sampled voltage evidence')
    if case['evidence_state'] != 'current':
        missing.append(case['evidence_state'])
    return missing


def numerical_corners(case):
    """Reproducible physical inputs/outputs, also used for independent SPICE checks."""
    values = []
    if case['kind'] == 'feedback':
        for rs in resistor_corners(case):
            for vref, bias in product(case['vref_bounds'], case['bias_a']):
                values.append({'resistors': rs, 'vref': vref, 'bias': bias,
                               'value': feedback_value(case, rs, vref, bias)})
    elif case['kind'] == 'pull':
        for rs in resistor_corners(case):
            if rs:
                values.append({'resistors': rs, 'value': pull_value(rs)})
    elif case['kind'] == 'rc':
        for rs in resistor_corners(case):
            for sign, supply, initial, t in product(
                    (-1, 1), case['supply_v'], case['initial_v'], case['sample_s']):
                cap = case['cap_f'] * (1 + sign * case['cap_tol'])
                values.append({'resistors': rs, 'cap': cap, 'supply': supply,
                               'initial': initial, 'time': t,
                               'value': rc_value(rs[0]['ohm'], cap, supply, initial, t)})
    else:
        raise ValueError('Unknown circuit kind')
    return values


def judge(case):
    missing = missing_inputs(case)
    if missing:
        return {'status': 'INSUFFICIENT', 'missing': missing, 'window': None,
                'reason': 'Required case evidence is incomplete or stale'}
    if case['kind'] == 'pull' and not active(case):
        return {'status': 'FAIL', 'missing': [], 'window': None,
                'reason': 'No fitted pull resistor; existence requirement violated'}
    corners = numerical_corners(case)
    numbers = [c['value'] for c in corners]
    window = {'min': min(numbers), 'max': max(numbers)}
    if case['kind'] == 'rc':
        good = (window['min'] >= case['threshold_v'] if case['required'] == 'high'
                else window['max'] <= case['threshold_v'])
    else:
        good = window['min'] >= case['limits'][0] and window['max'] <= case['limits'][1]
    return {'status': 'PASS' if good else 'FAIL', 'window': window, 'missing': [],
            'corner_count': len(corners),
            'reason': 'All specified corners comply' if good else 'A specified corner violates the criterion'}
