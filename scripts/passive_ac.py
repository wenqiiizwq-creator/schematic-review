#!/usr/bin/env python3
"""Small-signal RLC analysis; no default tolerances, source impedance or load.

Nominal complex nodal analysis plus outward-rounded interval arithmetic. Bounds
at specified frequencies enclose continuous component ranges (not just corners).
Interval dependency/pivot uncertainty is an explicit failure to prove, never PASS.
"""
import math


class Interval:
    def __init__(self, lo, hi=None):
        self.lo, self.hi = float(lo), float(lo if hi is None else hi)
        if not math.isfinite(self.lo) or not math.isfinite(self.hi) or self.lo > self.hi:
            raise ValueError('invalid/nonfinite interval')

    @staticmethod
    def of(x):
        return x if isinstance(x, Interval) else Interval(x)

    @staticmethod
    def outward(lo, hi):
        return Interval(math.nextafter(lo, -math.inf), math.nextafter(hi, math.inf))

    def __add__(self, other):
        o = self.of(other)
        return self.outward(self.lo + o.lo, self.hi + o.hi)

    __radd__ = __add__

    def __neg__(self):
        return Interval(-self.hi, -self.lo)

    def __sub__(self, other):
        return self + -self.of(other)

    def __rsub__(self, other):
        return self.of(other) - self

    def __mul__(self, other):
        o = self.of(other)
        v = [a * b for a in (self.lo, self.hi) for b in (o.lo, o.hi)]
        return self.outward(min(v), max(v))

    __rmul__ = __mul__

    def __truediv__(self, other):
        o = self.of(other)
        if o.lo <= 0 <= o.hi:
            raise ValueError('interval denominator contains zero; cannot prove bound')
        return self * self.outward(1 / o.hi, 1 / o.lo)

    def __rtruediv__(self, other):
        return self.of(other) / self

    def square(self):
        lo = 0.0 if self.lo <= 0 <= self.hi else min(self.lo ** 2, self.hi ** 2)
        hi = max(self.lo ** 2, self.hi ** 2)
        return Interval(max(0, math.nextafter(lo, -math.inf)), math.nextafter(hi, math.inf))

    def sqrt(self):
        if self.lo < 0:
            raise ValueError('square root includes negative range')
        return Interval(max(0, math.nextafter(math.sqrt(self.lo), -math.inf)),
                        math.nextafter(math.sqrt(self.hi), math.inf))

    def json(self):
        return {'min': self.lo, 'max': self.hi}


class ComplexBox:
    def __init__(self, real=0, imag=0):
        self.real, self.imag = Interval.of(real), Interval.of(imag)

    @staticmethod
    def of(x):
        return x if isinstance(x, ComplexBox) else ComplexBox(x)

    def __add__(self, other):
        o = self.of(other)
        return ComplexBox(self.real + o.real, self.imag + o.imag)

    __radd__ = __add__

    def __neg__(self):
        return ComplexBox(-self.real, -self.imag)

    def __sub__(self, other):
        return self + -self.of(other)

    def __mul__(self, other):
        o = self.of(other)
        return ComplexBox(self.real * o.real - self.imag * o.imag,
                          self.real * o.imag + self.imag * o.real)

    __rmul__ = __mul__

    def __truediv__(self, other):
        o = self.of(other)
        denominator = o.real.square() + o.imag.square()
        numerator = self * ComplexBox(o.real, -o.imag)
        return ComplexBox(numerator.real / denominator, numerator.imag / denominator)

    def magnitude(self):
        x = self.real.square() + self.imag.square()
        return Interval(max(0, x.lo), x.hi).sqrt()


def solve(matrix, rhs, boxed=False):
    """Gaussian elimination with a provably nonzero pivot for interval solves."""
    a = [list(row) + [value] for row, value in zip(matrix, rhs)]
    size = len(a)
    for col in range(size):
        def score(row):
            x = a[row][col]
            return x.magnitude().lo if boxed else abs(x)
        pivot = max(range(col, size), key=score)
        if score(pivot) <= 0:
            raise ValueError('singular/floating/interval-uncertain passive network')
        a[pivot], a[col] = a[col], a[pivot]
        p = a[col][col]
        for j in range(col, size + 1):
            a[col][j] = a[col][j] / p
        for row in range(col + 1, size):
            factor = a[row][col]
            for j in range(col + 1, size + 1):
                a[row][j] = a[row][j] - factor * a[col][j]
    out = [None] * size
    for row in reversed(range(size)):
        value = a[row][size]
        for j in range(row + 1, size):
            value = value - a[row][j] * out[j]
        out[row] = value
    return out


def _value(spec, boxed):
    return Interval(spec['min'], spec['max']) if boxed else spec['nominal']


def response(edges, model, frequency, boxed=False):
    """Transfer from a 1 V Thevenin source to output, relative to reference."""
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError('frequency must be finite and positive')
    source, output, ground = (model[k] for k in ('input_net', 'output_net', 'reference_net'))
    rs = model['source_resistance_ohm']
    zero_source = rs['min'] == rs['max'] == 0
    fixed = {ground: 0}
    if zero_source:
        fixed[source] = 1
    nets = sorted({n for edge in edges for n in edge['nets']} - set(fixed))
    if len(nets) > 12 or len(edges) > 24:
        raise ValueError('model-size limit: at most 12 unknown nodes and 24 elements')
    index = {n: i for i, n in enumerate(nets)}
    number = ComplexBox if boxed else complex
    a = [[number(0) for _ in nets] for _ in nets]
    b = [number(0) for _ in nets]
    omega = 2 * math.pi * frequency
    # Enclose the floating representation of omega as well as component data.
    w = Interval.outward(omega, omega) if boxed else omega

    def stamp(left, right, y):
        for n, other in ((left, right), (right, left)):
            if n in fixed:
                continue
            i = index[n]
            a[i][i] = a[i][i] + y
            if other in fixed:
                b[i] = b[i] + y * number(fixed[other])
            else:
                a[i][index[other]] = a[i][index[other]] - y

    for edge in edges:
        spec = model['components'][edge['ref']]
        value = _value(spec, boxed)
        if edge['kind'] == 'resistor':
            z = number(value)
        else:
            loss = _value(spec['series_resistance_ohm'], boxed)
            reactance = w * value if edge['kind'] == 'inductor' else -1 / (w * value)
            z = number(loss, reactance) if boxed else complex(loss, reactance)
        stamp(*edge['nets'], number(1) / z)
    if not zero_source:
        y = number(1) / number(_value(rs, boxed))
        i = index[source]
        a[i][i], b[i] = a[i][i] + y, b[i] + y
    if not model.get('load_open'):
        stamp(output, ground, number(1) / number(_value(model['load_resistance_ohm'], boxed)))
    capacitance = _value(model['load_capacitance_f'], boxed)
    stamp(output, ground, number(0, w * capacitance))
    solution = solve(a, b, boxed)
    result = solution[index[output]]
    if boxed:
        return result.magnitude().json()
    if not math.isfinite(abs(result)):
        raise ValueError('nonfinite AC solution')
    return {'gain': abs(result), 'phase_deg': math.degrees(math.atan2(result.imag, result.real))}


def analytic_metrics(edges, model):
    """Guaranteed algebraic bounds for isolated RC LP/HP and LC LP stages.

Capacitor ESR must explicitly be zero in this model; a nonzero ESR is handled by
the general AC solver instead. f0 (natural resonance) != peak frequency != -3 dB.
    """
    if len(edges) != 2:
        return {}, ['closed-form metrics require an isolated two-element stage']
    inp, out, gnd = (model[k] for k in ('input_net', 'output_net', 'reference_net'))
    series = next((x for x in edges if set(x['nets']) == {inp, out}), None)
    shunt = next((x for x in edges if set(x['nets']) == {out, gnd}), None)
    if not series or not shunt:
        return {}, ['closed-form topology unavailable; retain general AC analysis']
    pars = model['components']
    for edge in edges:
        if edge['kind'] == 'capacitor':
            esr = pars[edge['ref']]['series_resistance_ohm']
            if esr['min'] != 0 or esr['max'] != 0:
                return {}, ['nonzero capacitor ESR: no first/second-order closed-form shortcut']
    v = lambda ref: _value(pars[ref], True)
    rs = _value(model['source_resistance_ohm'], True)
    rl = None if model.get('load_open') else _value(model['load_resistance_ohm'], True)
    cload = _value(model['load_capacitance_f'], True)
    two_pi = Interval.outward(2 * math.pi, 2 * math.pi)
    parallel = lambda r, load: r if load is None else 1 / (1 / r + 1 / load)
    kinds = series['kind'], shunt['kind']
    metrics = {}
    if kinds == ('resistor', 'capacitor'):
        r, c = rs + v(series['ref']), v(shunt['ref']) + cload
        metrics['dc_gain'] = Interval(1) if rl is None else rl / (r + rl)
        metrics['cutoff_hz'] = 1 / (two_pi * parallel(r, rl) * c)
        metrics['time_constant_s'] = parallel(r, rl) * c
    elif kinds == ('capacitor', 'resistor'):
        if model['load_capacitance_f']['max'] > 0:
            return {}, ['capacitive load adds an RC high-pass pole; use general AC analysis']
        r, c = parallel(v(shunt['ref']), rl), v(series['ref'])
        metrics['dc_gain'] = Interval(0)
        metrics['high_frequency_gain'] = r / (rs + r)
        metrics['cutoff_hz'] = 1 / (two_pi * (rs + r) * c)
        metrics['time_constant_s'] = (rs + r) * c
    elif kinds == ('inductor', 'capacitor'):
        l, c = v(series['ref']), v(shunt['ref']) + cload
        r = rs + _value(pars[series['ref']]['series_resistance_ohm'], True)
        # H(s)=1/(a*s^2+b*s+d); d includes the resistive load insertion loss.
        a, b, d = l * c, r * c, Interval(1)
        if rl is not None:
            b, d = b + l / rl, d + r / rl
        f0 = (d / a).sqrt() / two_pi
        metrics['dc_gain'], metrics['natural_frequency_hz'] = 1 / d, f0
        metrics['ideal_lc_frequency_hz'] = 1 / (two_pi * (l * c).sqrt())
        if b.lo > 0:
            q = (a * d).sqrt() / b
            metrics['q'], metrics['damping_ratio'] = q, b / (2 * (a * d).sqrt())
            # Relative-to-DC -3dB solution: x^2+(1/Q^2-2)x-1=0, x=(w/w0)^2.
            # Stable expression avoids cancellation for strongly damped filters.
            def factor(qvalue):
                k = 1 / (qvalue * qvalue) - 2
                root = 2 / (math.sqrt(k*k + 4) + k) if k >= 0 else (math.sqrt(k*k + 4) - k) / 2
                return math.sqrt(root)
            metrics['cutoff_hz'] = f0 * Interval.outward(factor(q.lo), factor(q.hi))
            if q.lo > 1 / math.sqrt(2):
                k = Interval(1) - 1 / (2 * q.square())
                metrics['peak_frequency_hz'] = f0 * k.sqrt()
            elif q.hi <= 1 / math.sqrt(2):
                metrics['peak_frequency_hz'] = Interval(0)
            else:
                metrics['peak_frequency_hz'] = Interval(0, math.nextafter(
                    f0.hi * math.sqrt(1 - 1/(2*q.hi*q.hi)), math.inf))
            def peak_gain(qvalue):
                return 1 if qvalue <= 1/math.sqrt(2) else qvalue / math.sqrt(1 - 1/(4*qvalue*qvalue))
            metrics['peak_gain'] = metrics['dc_gain'] * Interval.outward(peak_gain(q.lo), peak_gain(q.hi))
        else:
            return {k: x.json() for k, x in metrics.items()}, ['undamped/uncertain damping; Q and cutoff require further analysis']
    else:
        return {}, ['no closed-form metrics for this topology; use general AC and independent derivation']
    return {k: x.json() for k, x in metrics.items()}, []
