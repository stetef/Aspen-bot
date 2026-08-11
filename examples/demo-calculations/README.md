# Demo calculations (synthetic)

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
