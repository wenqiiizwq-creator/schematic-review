"""Prepare synthetic PST inputs and review evidence, without access to labels.

For RC only, the harness supplies an explicit voltage window. This measures the
checker's decision/evidence contract, NOT its ability to derive transients.
"""
import itertools
import math


def rc_evidence(case):
    if not case['voltage_evidence'] or case['cap_tol'] is None:
        return None
    r = case['resistors'][0]
    if r['tol'] is None:
        return None
    values = []
    for rs, cs, vf, v0, t in itertools.product(
            (-1, 1), (-1, 1), case['supply_v'], case['initial_v'], case['sample_s']):
        tau = r['ohm'] * (1 + rs * r['tol']) * case['cap_f'] * (1 + cs * case['cap_tol'])
        values.append(vf + (v0 - vf) * math.exp(-t / tau))
    return {'voltage_v': {'min': min(values), 'max': max(values)},
            'sample_window_s': dict(zip(('min', 'max'), case['sample_s'])),
            'method': 'Harness-supplied single-pole RC evidence',
            'calculation': 'Vf+(V0-Vf)*exp(-t/(R*C)); full independent parameter corners; see case specification',
            'loading': 'Synthetic ideal high-impedance input, no leakage or other branches',
            'conditions': 'Ideal stable source, stated initial voltage/R/C tolerance and sample interval; no ramp/parasitics'}


def prepare(case):
    names = {'OUT': 'VOUT_REG', 'FB': 'FB', 'GND': 'GND', 'N1': 'MID1', 'N2': 'MID2',
             'BUS': 'BOOT0' if case['kind'] == 'rc' else 'SDA', 'SUPPLY': 'VCC_3V3'}
    if case['naming'] == 'anonymous':
        names.update(OUT='SOURCE_A', FB='SENSE_A', N1='LINK_A', N2='LINK_B', BUS='SIGNAL_A', SUPPLY='SOURCE_B')
    parts = {}
    nets = {}
    pinname = {}

    def part(ref, value, pins, fitted=True):
        primitive = 'SYNTH_' + ref + ('' if fitted else '_DNP')
        parts[ref] = {'value': value, 'prim': primitive, 'nc': not fitted}
        for number, (net, function) in pins.items():
            node = ref + '.' + str(number)
            nets.setdefault(names[net], []).append(node)
            pinname[node] = function

    target = 'FB' if case['kind'] == 'feedback' else 'BUS'
    function = 'FB' if case['kind'] == 'feedback' else ('BOOT0' if case['kind'] == 'rc' else 'SDA')
    part('U1', 'SYNTHETIC-IDEAL-INPUT', {1: (target, function), 2: ('GND', 'VSS')})
    for resistor in case['resistors']:
        value = '%g' % resistor['ohm']
        if resistor['tol'] is not None:
            value += '/%g%%' % (100 * resistor['tol'])
        part(resistor['ref'], value, {1: (resistor['a'], 'P1'), 2: (resistor['b'], 'P2')}, resistor['fitted'])
    if case['kind'] == 'rc':
        part('C1', '%gF' % case['cap_f'], {1: ('BUS', 'P1'), 2: ('GND', 'P2')})
    primitive_text, instance_text, net_text = [], [], []
    for ref, item in parts.items():
        primitive_text += ["primitive '%s';" % item['prim'], "  PART_NAME='%s';" % item['value'],
                           "  VALUE='%s';" % item['value'], "  JEDEC_TYPE='SYNTHETIC';"]
        for node, name in pinname.items():
            if node.startswith(ref + '.'):
                primitive_text += ["  '%s':" % name, "    PIN_NUMBER='(%s)';" % node.split('.')[1],
                                   "    PINUSE='%s';" % ('IN' if ref == 'U1' else 'PASSIVE')]
        primitive_text += ['end_primitive;']
        instance_text += [" %s '%s':;" % (ref, item['prim']), "P_PATH='@SYNTH:page1_case';"]
    for net, nodes in nets.items():
        net_text += ['NET_NAME', "'%s'" % net, "C_SIGNAL='@SYNTH(SCH_1):%s';" % net]
        for node in nodes:
            ref, pin = node.split('.')
            net_text += ['NODE_NAME %s %s' % (ref, pin), "'@SYNTH';", "'%s':;" % pinname[node]]
    check = {'id': 'CHECK-1', 'node': 'U1.1', 'net': names[target],
             'citation': 'SYNTHETIC case specification; bounded model and criterion; not a real datasheet',
             'depends_on': [ref for ref, p in parts.items() if not p['nc']]}
    if case['kind'] == 'feedback':
        vref = {'typ': case['vref_typ']}
        if case['vref_bounds'] is not None:
            vref.update(zip(('min', 'max'), case['vref_bounds']))
        check.update(rule='Rule-08', kind='divider', vref=vref,
                     expected=dict(zip(('min', 'max'), case['limits'])),
                     divider_model={'source_net': names['OUT'], 'reference_net': names['GND'],
                                    'bias_current_a': dict(zip(('min', 'max'), case['bias_a'])),
                                    'ignored_nodes': {'U1.1': 'Synthetic FB input current explicitly included in bias range'}})
    elif case['kind'] == 'pull':
        check.update(rule='Rule-09', kind='required_pull', direction='up', to=names['SUPPLY'],
                     resistance_ohm=dict(zip(('min', 'max'), case['limits'])))
    else:
        check.update(rule='Rule-16', kind='strap', required=case['required'])
        check['vih_min_v' if case['required'] == 'high' else 'vil_max_v'] = case['threshold_v']
        evidence = rc_evidence(case)
        if evidence:
            check['voltage_analysis'] = evidence
    # Only engineering inputs go to the worker: no split, variant, expected status
    # or oracle window for native-calculation cases.
    spec = {k: v for k, v in case.items() if k not in ('id', 'split', 'family', 'variant', 'scale', 'track')}
    return {'schema_version': 1, 'synthetic_only': True, 'specification': spec,
            'evidence_state': case['evidence_state'], 'check': check,
            'pst': {'pstchip.dat': '\n'.join(primitive_text) + '\n',
                    'pstxprt.dat': '\n'.join(instance_text) + '\n',
                    'pstxnet.dat': '\n'.join(net_text) + '\n',
                    'netlist.log': 'SYNTHETIC fixture export; no real EDA export claimed\n'},
            'connectivity': {'nets': nets, 'pinname': pinname, 'parts': parts}}
