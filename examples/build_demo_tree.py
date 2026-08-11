#!/usr/bin/env python3
"""
Generate ``examples/demo-calculations/`` — the synthetic calculations tree the
DEMO walkthrough runs against.

Kept as a generator rather than just committed files so that the data is
*obviously* fabricated and stays easy to extend. Everything here is made up: the
energies come from a Morse curve, not from ORCA. The file *shapes* are real
though — the markers a scientist (and Aspen) actually greps for, in the places
ORCA puts them — because a demo that reads nothing like real output teaches the
wrong thing about what Aspen can do.

The scenario is deliberately small and plottable:

* ``fe-porphyrin-scan/`` — six constrained optimizations along an Fe–O distance,
  which gives a curve with a minimum near 1.95 Å. One run does **not** converge,
  because "which of my runs failed?" is a question people actually ask and a demo
  where everything worked is a demo that never shows the failure panel.
* ``spin-states/`` — a high-spin/low-spin pair, so there is a second question
  worth asking ("which spin state is lower?") that needs two files compared.

Run:  python examples/build_demo_tree.py
"""

import math
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "demo-calculations"

# Morse-ish curve: E(r) = E0 + D(1 - e^{-a(r-re)})^2, minimum at re.
E0, DEPTH, ALPHA, RE = -4038.285056, 0.0821, 2.31, 1.95
SCAN = [1.80, 1.90, 1.95, 2.00, 2.10, 2.30]
# The 2.30 Å point is the one that fails — a stretched bond that won't settle is
# a believable failure, not an arbitrary one.
FAILS_AT = 2.30


def energy(r: float) -> float:
    return E0 + DEPTH * (1.0 - math.exp(-ALPHA * (r - RE))) ** 2


def orca_input(name: str, distance: float, multiplicity: int = 5) -> str:
    return f"""! B97-3c CPCM(WATER) OPT

# Synthetic demo input — not a real calculation.
%pal nprocs 16
  end

%geom Constraints
  {{B 0 1 {distance:.2f} C}}   # Fe-O distance held at {distance:.2f} Angstrom
  end
  end

* xyz 0 {multiplicity}
Fe   0.000000   0.000000   0.000000
O    0.000000   0.000000   {distance:.6f}
N    2.010000   0.000000   0.000000
N   -2.010000   0.000000   0.000000
N    0.000000   2.010000   0.000000
N    0.000000  -2.010000   0.000000
*
"""


def orca_log(name: str, distance: float, converged: bool = True,
             multiplicity: int = 5) -> str:
    total = energy(distance)
    scf = total - 0.5451
    charge = 1.62 + 0.11 * (distance - RE)
    spin = 3.84 - 0.06 * (distance - RE)
    cycles = 23 if converged else 0

    head = f"""                                 *****************
                                 * O   R   C   A *
                                 *****************
                    (synthetic output generated for the Aspen demo)

INPUT FILE
================================================================================
NAME = {name}.in
|  1> ! B97-3c CPCM(WATER) OPT
|  2> %geom Constraints {{B 0 1 {distance:.2f} C}} end end

                       ORCA SCF
------------------------------------------------------------------------------
Total Charge           Charge          ....    0
Multiplicity           Mult            ....    {multiplicity}
Number of Electrons    NEL             ....   72
"""

    if not converged:
        return head + f"""
               *****************************************************
               *                     ERROR                         *
               *        SCF NOT CONVERGED AFTER  125 CYCLES        *
               *****************************************************

The geometry optimization did not converge. Last energy change was
2.7e-04 Eh over the final 20 cycles, and the Fe-O distance drifted by
0.08 Angstrom while constrained — check the constraint definition.

                             ****ORCA TERMINATED ABNORMALLY****
TOTAL RUN TIME: 0 days 4 hours 12 minutes 8 seconds
"""

    return head + f"""
               *****************************************************
               *           SCF CONVERGED AFTER  {cycles} CYCLES          *
               *****************************************************

----------------
TOTAL SCF ENERGY
----------------
Total Energy       :   {scf:.11f} Eh   {scf * 27.2114:.5f} eV

-----------------------
MULLIKEN ATOMIC CHARGES
-----------------------
   0 Fe:   {charge:8.6f}    {spin:8.6f}
   1 O :  -0.742318   0.121004
   2 N :  -0.418772   0.033911
   3 N :  -0.418640   0.033894
   4 N :  -0.418701   0.033902
   5 N :  -0.418655   0.033898
Sum of atomic charges         :    0.0000000

-------------------------
GEOMETRY OPTIMIZATION CYCLE
-------------------------
                    ***********************HURRAY********************
                    ***        THE OPTIMIZATION HAS CONVERGED     ***
                    *************************************************

---------------------------------
FINAL SINGLE POINT ENERGY   {total:.9f}
---------------------------------

Fe-O distance (constrained)  ....  {distance:.4f} Angstrom

                             ****ORCA TERMINATED NORMALLY****
TOTAL RUN TIME: 0 days 2 hours 41 minutes 3 seconds
"""


def xyz(distance: float, comment: str) -> str:
    return f"""6
{comment}
Fe    0.000000    0.000000    0.000000
O     0.000000    0.000000   {distance:.6f}
N     2.008431    0.000000    0.010224
N    -2.008118    0.000000    0.010199
N     0.000000    2.008377    0.010211
N     0.000000   -2.008290    0.010207
"""


def build() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)

    (ROOT / "README.md").write_text("""# Demo calculations (synthetic)

Nothing in this directory came out of a real calculation. The numbers are drawn
from a Morse curve and the ORCA output is a stub carrying the markers a real log
carries — enough for Aspen to browse, grep, and plot, and not enough to mistake
for data.

It exists so anyone can watch Aspen work without being added as a user and
without being shown someone's actual research. See `DEMO` in the README.

* `fe-porphyrin-scan/` — six constrained optimizations along the Fe–O distance.
  One (2.30 Å) does not converge, on purpose.
* `spin-states/` — a high-spin / low-spin pair to compare.

Regenerate with `python examples/build_demo_tree.py`.
""", encoding="utf-8")

    # -- the distance scan --------------------------------------------------- #
    scan = ROOT / "fe-porphyrin-scan"
    scan.mkdir()
    report = ["# Convergence report — Fe-porphyrin Fe-O scan (synthetic)", ""]
    for distance in SCAN:
        name = f"feo-{distance:.2f}".replace(".", "p")
        run = scan / f"d{distance:.2f}"
        run.mkdir()
        converged = distance != FAILS_AT
        (run / f"{name}.in").write_text(orca_input(name, distance), encoding="utf-8")
        (run / f"{name}-orca.log").write_text(
            orca_log(name, distance, converged), encoding="utf-8")
        if converged:
            (run / "optimized.xyz").write_text(
                xyz(distance, f"Fe-O = {distance:.2f} A, E = {energy(distance):.6f} Eh"),
                encoding="utf-8")
        report.append(
            f"d{distance:.2f}  {'converged' if converged else 'FAILED (SCF not converged)'}"
            + (f"  E = {energy(distance):.6f} Eh" if converged else "")
        )
    (scan / "convergence_report.log").write_text("\n".join(report) + "\n", encoding="utf-8")

    # -- spin states --------------------------------------------------------- #
    spins = ROOT / "spin-states"
    spins.mkdir()
    for label, mult, offset in (("high-spin", 5, 0.0), ("low-spin", 1, 0.0271)):
        run = spins / label
        run.mkdir()
        name = f"feo-{label}"
        (run / f"{name}.in").write_text(orca_input(name, RE, mult), encoding="utf-8")
        text = orca_log(name, RE, True, mult)
        # Offset the low-spin state so the comparison has an answer.
        text = text.replace(f"{energy(RE):.9f}", f"{energy(RE) + offset:.9f}")
        (run / f"{name}-orca.log").write_text(text, encoding="utf-8")
        (run / "optimized.xyz").write_text(
            xyz(RE, f"{label}, mult={mult}"), encoding="utf-8")

    files = sum(1 for _ in ROOT.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in ROOT.rglob("*") if f.is_file())
    print(f"Wrote {ROOT} — {files} files, {size / 1024:.0f} KB")


if __name__ == "__main__":
    build()
