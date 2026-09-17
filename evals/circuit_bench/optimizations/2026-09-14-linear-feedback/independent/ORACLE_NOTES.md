# Frozen independent circuit answers

The 22 cases were frozen before any candidate run. No benchmark answers,
generators or historical scores were read. Production code was not modified.

The suite contains 7 expected PASS, 3 expected FAIL, and 12 expected INSUFFICIENT.
All 10 numerical cases check min, typ and max using a 1e-9 V absolute or 2e-9
relative numerical tolerance.

Independent answers use exact rational arithmetic and explicit scalar equations:

- Arms: O = F*(1 + Ru/Rl) + Ib*Ru.
- Shared trunks: series/parallel equivalence counts each physical resistor once.
- Bridge: eliminate its two KCL equations. Nominal gain is 29/13 and input-current
  coefficient is 140000/13 ohms.
- Three-row grid: equal horizontal strings imply equipotential vertical columns,
  zero vertical currents, and O = 3F.
- Open stub: terminal KCL gives zero current.

Each bound enumerates physical component endpoints consistently. For the bridge,
fixing every other conductance makes output a linear fractional function of the
remaining conductance with positive denominator; each coordinate attains an
extremum at an endpoint, including when negative bias reverses sensitivity.

Missing parameters, undefined rails/grounds, nonlinear loads without state/I-V
models and impossible feedback shorts require INSUFFICIENT. DNP means open.

The before source tree supplies only its existing synthetic evidence-binding
fixture, never expected electrical results. Test documents are synthetic software
inputs, not manufacturer datasheets or physical hardware signoff.

The companion ngspice check reuses frozen analytic min/typ/max corners and sets
the predicted output voltage as an ideal supply. It independently verifies that
the resistor network then reaches the required feedback voltage. It does not
change the frozen answers and does not invoke production code.
