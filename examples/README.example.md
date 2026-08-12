<!--
An example project README — what Aspen offers to draft when it finds a project
directory that nothing describes.

Two things worth knowing before you write one:

  1. NOTHING here is required. Aspen reads and analyses projects with no README
     at all; this only makes its answers better, because it stops guessing what
     your run directories mean.
  2. You do NOT need to know markdown. Only one section is ever parsed — the
     library list at the bottom, found by the word "libraries" in its heading.
     Everything else is read as plain prose. Bullet points and '#' headings are
     habit, not syntax; ordinary sentences work exactly as well.

Where it goes: in the project directory itself, as README.md, saved by you.
Aspen cannot write it — every calculations root is read-only to it. Ask it to
draft one from what's actually in the directory and paste the result.

If you would rather not keep a file at all, tell Aspen to keep the notes on its
side instead: it stores them outside your tree and reads them back the same way.
Either is fine, and so is neither.
-->

# thermolysin — Zn K-edge XAS and DFT on thermolysin active-site variants

## Summary
DFT-optimised models of the thermolysin Zn site with different first-shell
ligand sets, used to work out which coordination environment reproduces the
measured Zn K-edge spectrum. ORCA 5.0.4, B3LYP/def2-TZVP, CPCM(water).

## Questions of interest
- Which model reproduces the measured edge position and pre-edge intensity?
- How much does swapping the Glu166 carboxylate for water shift the edge?
- Do the optimised Zn–O/N distances agree with the EXAFS fit (1.99–2.05 Å)?

## Runs
- `run_001`–`run_012` — 4-coordinate models, one per ligand permutation
- `run_013`–`run_028` — the same set, 5-coordinate with an added water
- `run_029`–`run_042` — 5-coordinate rerun after the geometry fix of 2026-03; use
  these rather than 013–028 for anything quantitative

## Where the files are
For a run `run_007`: input `run_007/run_007.in`, optimised structure
`run_007/run_007.xyz`, output log `run_007/run_007.log`, computed spectrum
`run_007/run_007.spectrum.dat` (two columns: energy in eV, intensity).

## Python libraries available for analysis
- numpy
- pandas
- matplotlib
- scipy
