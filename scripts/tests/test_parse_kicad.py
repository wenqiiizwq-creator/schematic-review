import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from lint import Lint
from parse_kicad import build, parse
from parse_netlist import self_check
from plan_review import build_review_plan

LIBPARTS = """
  <libparts>
    <libpart lib="Device" part="R">
      <pins>
        <pin num="1" name="~" type="passive"/>
        <pin num="2" name="~" type="passive"/>
      </pins>
    </libpart>
    <libpart lib="Connector" part="Barrel_Jack">
      <pins>
        <pin num="1" name="+" type="passive"/>
        <pin num="2" name="-" type="passive"/>
      </pins>
    </libpart>
    <libpart lib="Regulator_Linear" part="LDO">
      <pins>
        <pin num="1" name="VIN" type="power_in"/>
        <pin num="2" name="GND" type="power_in"/>
        <pin num="3" name="VOUT" type="power_out"/>
        <pin num="4" name="EN" type="input"/>
        <pin num="5" name="NC" type="no_connect"/>
      </pins>
    </libpart>
  </libparts>
"""


def netlist(components, nets, sheets=('/',), libparts=LIBPARTS):
    sheet_xml = ''.join(
        '<sheet number="%d" name="%s" tstamps="%s"/>' % (index + 1, name, name)
        for index, name in enumerate(sheets))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<export version="E">\n'
            '  <design><source>demo.kicad_sch</source><tool>Eeschema 10.0.6</tool>'
            '<date>2026-09-16T00:00:00</date>' + sheet_xml + '</design>\n'
            '  <components>' + components + '</components>\n'
            + libparts +
            '  <nets>' + nets + '</nets>\n'
            '</export>\n')


def comp(ref, lib, part, value, *, footprint='', fields='', properties='', sheet='/'):
    return ('<comp ref="%s"><value>%s</value><footprint>%s</footprint>'
            '<fields>%s</fields>'
            '<libsource lib="%s" part="%s"/>%s'
            '<sheetpath names="%s" tstamps="%s"/></comp>'
            % (ref, value, footprint, fields, lib, part, properties, sheet, sheet))


def net(name, *nodes):
    body = ''.join('<node ref="%s" pin="%s"%s%s/>' % (
        ref, pin,
        ' pinfunction="%s"' % function if function else '',
        ' pintype="%s"' % pintype if pintype else '')
        for ref, pin, function, pintype in nodes)
    return '<net code="1" name="%s" class="Default">%s</net>' % (name, body)


def board():
    """LDO + 两只电阻；含一个声明 NC 的脚和一个真悬空的脚。"""
    components = (
        comp('U1', 'Regulator_Linear', 'LDO', 'LDO-3V3', footprint='Package_TO_SOT_SMD:SOT-23-5',
             fields='<field name="MPN">SYN-LDO-3V3</field>')
        + comp('R1', 'Device', 'R', '10k', footprint='Resistor_SMD:R_0603_1608Metric')
        + comp('R2', 'Device', 'R', '10k/NC')
        + comp('J1', 'Connector', 'Barrel_Jack', 'PWR_IN'))
    nets = (
        net('VIN_5V', ('U1', '1', 'VIN', 'power_in'), ('J1', '1', '+', 'passive'))
        + net('GND', ('U1', '2', 'GND', 'power_in'), ('R2', '2', '~', 'passive'),
              ('J1', '2', '-', 'passive'))
        + net('VCC_3V3', ('U1', '3', 'VOUT', 'power_out'), ('R1', '1', '~', 'passive'))
        + net('EN_3V3', ('U1', '4', 'EN', 'input'), ('R1', '2', '~', 'passive'))
        + net('unconnected-(U1-NC-Pad5)', ('U1', '5', 'NC', 'no_connect+passive'))
        + net('unconnected-(R2-Pad1)', ('R2', '1', '~', 'passive')))
    return netlist(components, nets)


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.db = parse(board())

    def test_nets_and_pin2net_are_mutually_consistent(self):
        self.assertEqual(self.db['nets']['VCC_3V3'], ['U1.3', 'R1.1'])
        self.assertEqual(self.db['pin2net']['U1.3'], 'VCC_3V3')
        self.assertTrue(self_check(self.db, strict=False))

    def test_pin_names_come_from_pinfunction_and_ignore_the_no_name_marker(self):
        self.assertEqual(self.db['pinname']['U1.1'], 'VIN')
        self.assertNotIn('R1.1', self.db['pinname'])

    def test_pin_types_map_to_the_existing_pinuse_vocabulary(self):
        self.assertEqual(self.db['pintype']['U1.1'], 'POWER')
        self.assertEqual(self.db['pintype']['U1.4'], 'IN')
        self.assertEqual(self.db['pintype']['R1.1'], 'UNSPEC')

    def test_parts_carry_mpn_footprint_and_library_identity(self):
        self.assertEqual(self.db['parts']['U1']['part'], 'SYN-LDO-3V3')
        self.assertEqual(self.db['parts']['U1']['prim'], 'Regulator_Linear:LDO')
        self.assertEqual(self.db['parts']['U1']['jedec'], 'Package_TO_SOT_SMD:SOT-23-5')
        self.assertEqual(self.db['parts']['R1']['part'], 'R')

    def test_declared_pin_inventory_covers_pins_absent_from_nets(self):
        self.assertEqual(self.db['declared_pinname']['U1.5'], 'NC')
        self.assertEqual(self.db['declared_pintype']['U1.3'], 'POWER')
        self.assertNotIn('R1.1', self.db['declared_pinname'])

    def test_export_metadata_is_recorded(self):
        self.assertEqual(self.db['export_meta']['tool'], 'Eeschema 10.0.6')
        self.assertEqual(self.db['export_meta']['format'], 'kicadxml')


class NotConnectedTest(unittest.TestCase):
    def test_declared_no_connect_becomes_a_pseudo_net(self):
        db = parse(board())
        self.assertEqual(db['pseudo_nets'], ['unconnected-(U1-NC-Pad5)'])
        self.assertEqual(db['no_connect_nodes'], ['U1.5'])

    def test_dangling_pin_stays_a_real_single_node_net(self):
        db = parse(board())
        self.assertIn('unconnected-(R2-Pad1)', db['nets'])
        self.assertNotIn('unconnected-(R2-Pad1)', db['pseudo_nets'])
        hits = [f for f in Lint(db).run() if f['rule'] == 'Rule-01']
        self.assertEqual([f['detail'].split(' <- ')[0] for f in hits],
                         ['unconnected-(R2-Pad1)'])

    def test_dnp_property_marks_the_part_unpopulated(self):
        db = parse(netlist(
            comp('R3', 'Device', 'R', '4.7k', properties='<property name="dnp"/>')
            + comp('R4', 'Device', 'R', '4.7k',
                   properties='<property name="exclude_from_bom"/>'),
            net('SIG', ('R3', '1', '~', 'passive'), ('R4', '1', '~', 'passive'))))
        self.assertTrue(db['parts']['R3']['nc'])
        self.assertFalse(db['parts']['R4']['nc'])

    def test_nc_mark_in_value_still_counts(self):
        self.assertTrue(parse(board())['parts']['R2']['nc'])


class RobustnessTest(unittest.TestCase):
    def test_missing_libpart_is_reported(self):
        db = parse(netlist(comp('U9', 'Custom', 'MCU', 'SYN-MCU'),
                           net('SIG', ('U9', '1', 'PA0', 'bidirectional'))))
        self.assertEqual(db['missing_primitives'], ['Custom:MCU'])
        self.assertEqual(db['pintype']['U9.1'], 'BI')

    def test_unknown_pin_type_is_preserved_not_silently_unspec(self):
        db = parse(netlist(comp('U9', 'Device', 'R', 'X'),
                           net('SIG', ('U9', '1', 'P', 'future_type'))))
        self.assertEqual(db['pintype']['U9.1'], 'FUTURE_TYPE')

    def test_exporter_pin_number_suffix_is_stripped(self):
        db = parse(netlist(comp('U1', 'Regulator_Linear', 'LDO', 'LDO'),
                           net('EN_3V3', ('U1', '4', 'EN_4', 'input'))))
        self.assertEqual(db['pinname']['U1.4'], 'EN')

    def test_alternate_pin_function_is_kept(self):
        db = parse(netlist(comp('U1', 'Regulator_Linear', 'LDO', 'LDO'),
                           net('SYS_EN', ('U1', '4', 'PWREN', 'input'))))
        self.assertEqual(db['pinname']['U1.4'], 'PWREN')

    def test_pin_name_falls_back_to_the_library_symbol(self):
        db = parse(netlist(comp('U1', 'Regulator_Linear', 'LDO', 'LDO'),
                           net('VIN_5V', ('U1', '1', '', 'power_in'))))
        self.assertEqual(db['pinname']['U1.1'], 'VIN')

    def test_hierarchical_sheets_become_page_numbers(self):
        db = parse(netlist(comp('R1', 'Device', 'R', '10k', sheet='/power/')
                           + comp('R2', 'Device', 'R', '10k', sheet='/'),
                           net('SIG', ('R1', '1', '~', 'passive'), ('R2', '1', '~', 'passive')),
                           sheets=('/', '/power/')))
        self.assertEqual(db['ref2page'], {'R1': 2, 'R2': 1})

    def test_malformed_input_is_rejected(self):
        with self.assertRaises(ValueError):
            parse('<export><nets>')
        with self.assertRaises(ValueError):
            parse('<netlist><nets/></netlist>')

    def test_missing_file_is_reported(self):
        with self.assertRaises(RuntimeError):
            build(str(SCRIPTS / 'no-such-netlist.xml'))


class PipelineTest(unittest.TestCase):
    def test_plan_and_lint_run_on_kicad_input(self):
        db = parse(board())
        plan = build_review_plan(db)
        self.assertTrue(plan['checks'])
        self.assertEqual(plan['power_up_version'], 1)
        regulators = [item for state in plan['power_up']['states']
                      for item in state['regulators']]
        self.assertEqual([item['ref'] for item in regulators], ['U1'])
        self.assertEqual(regulators[0]['enable_source'], 'pulled')
        self.assertTrue(Lint(db).run())


if __name__ == '__main__':
    unittest.main()
