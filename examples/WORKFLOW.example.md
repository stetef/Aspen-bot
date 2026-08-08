---
description: DFT workflow for transition-metal complexes — geometry optimization, TD-DFT and XES spectra, NEB reaction coordinates. ORCA-based, benchmarked against EXAFS/crystal structures.
---

<!--
An example workflow file, converted from a real one a group member wrote as a
plain document. Two things make it work as a workflow rather than a document:

  1. The `description:` line above. It is the ONE line that lands in every
     conversation's context, so it is what tells Aspen (and everyone browsing
     `read_workflow`) when this file is worth opening. Name the techniques, the
     systems, and the software.
  2. Headings that match how you'd ask for the work ("TD-DFT", "Reaction
     Coordinates"), so Aspen can quote the relevant part instead of the lot.

Everything else — ownership, your alias, timestamps — is stamped automatically
when the file is saved; don't write those by hand.

To install this for a user:
    ./aspen-users add U01ABC2DEF --alias arun --name "Arun N."
    mkdir -p "$ASPEN_STATE_DIR/workflows/arun__U01ABC2DEF"
    cp examples/WORKFLOW.example.md "$ASPEN_STATE_DIR/workflows/arun__U01ABC2DEF/WORKFLOW.md"

Or just paste the content to Aspen in Slack and ask it to save it as your
workflow — it will write the frontmatter for you.
-->

## Geometry Optimization

1. Perform geometry optimization.
   - Typically BP86/def2-TZVP for 3d transition metals, BP86/ZORA-def2-TZVP for 4d/5d.
   - If the spin state is unknown, run a separate calculation for each possible spin state.
2. Assess the output structure and run more calculations if needed.
   - Do bond lengths/angles agree with the crystal structure or EXAFS? If yes, proceed.
   - With multiple spin states, compare final single-point energies — which is most
     stable, and does that agree with experiment?
   - If the computed structure disagrees with experiment, repeat with a different
     exchange-correlation functional (B3LYP, TPSS, TPSSh, PBE0).
3. From a good DFT-optimized structure, extract:
   - Bond lengths/angles around the element of interest (usually the transition metal)
   - Mulliken and Loewdin charges, spin densities on the metal and its neighbors
   - Mayer bond orders between the metal and its neighbors
   - Final single-point energies or Gibbs free energies across spin states
   - A `feff.inp` for EXAFS fits, using the optimized structure to generate scattering paths
4. Natural bond order (NBO) calculations — not always performed.
   - Single-point NBO calculation on the optimized structure
   - Pull out natural charges and orbital occupancies of the bonding/antibonding
     orbitals of interest

## TD-DFT

1. Run the TD-DFT calculation.
   - On the DFT-optimized structure, at the same level of theory as the optimization.
   - Start with a large `nroots` (50–100).
   - Transitions are from 1s (K-edge) or 2p (L-edge) for XAS. UV-vis is done without
     specifying core orbitals.
2. Assess the output.
   - Plot individual transitions and the simulated spectrum against experiment.
   - Identify the energy range where they agree — typically the pre-edge region for
     3d transition metals.
   - Persistent disagreement may mean redoing the geometry optimization with a
     different functional.
3. Re-run with a lower `nroots` matching the range where TD-DFT agrees with
   experiment, and confirm the new spectrum still agrees.
4. Extract per transition: energy, intensity, acceptor MO index, and the composition
   of that MO (metal and ligand s/p/d percentages) — via MOAnalyzer in MATLAB.
   Examine acceptor MO contour plots in Chemcraft.

## DFT-computed XES

Follows the TD-DFT procedure: same optimized structure, plot the emission spectrum
against experiment (typically valence-to-core), tabulate transition
energies/intensities and donor MOs, and examine donor orbital contours in Chemcraft.

## Reaction Coordinates and Relaxed Scans

1. Compute independent reactant and product structures first (geometry optimization
   procedure above), and confirm their structures and Gibbs free energies are
   reasonable.
2. Compute the reaction coordinate with nudged-elastic band (NEB-TS), at the same
   level of theory as the optimization.
   - Reactant and product coordinates are the inputs; a transition-state guess is
     optional.
   - **Check the transition state's vibrational frequencies: there must be exactly
     one negative frequency.** More than one means the calculation likely missed the
     right trajectory — optimize the transition state independently (`!OptTS`) and
     supply it alongside the reactant/product coordinates.
3. Extract: energy vs. reaction coordinate, and Mayer bond orders, Mulliken/Loewdin
   charges and spin densities for the transition-state structure.
4. Relaxed scans follow the same pattern — vary a bond length/angle systematically,
   optimize at each step, and plot energy vs. distance/angle.
