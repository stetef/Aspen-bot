"""
Validating quantum-chemistry input files that Aspen wrote or edited.

Aspen is allowed to modify these freely — change the functional, swap the basis
set, add a ``%tddft`` block, replace the geometry, adjust charge and multiplicity.
That flexibility is the point: it is how someone reuses an example they already
have. What this module exists to stop is the narrow set of directives that turn an
input file from *data a science code reads* into *a way to run an arbitrary program
or write outside the run directory*.

**Why a closed block vocabulary rather than a pure denylist.** A scan of the
group's real inputs (Arun's tree plus the pipeline's ORCA templates) turns up
exactly eight ``%`` blocks in use::

    %maxcore  %pal  %basis  %geom  %output  %method  %qmmm  %pointcharges

With a vocabulary that small, "refuse blocks nobody has ever needed" costs almost
nothing and closes the directive nobody thought to denylist — which is the failure
mode a denylist cannot fix. So blocks are allowlisted while their *contents* stay
open, and the ``!`` keyword line is entirely open. Every edit the group actually
makes is a keyword, a basis set, a resource number, or a geometry, and all four
remain free.

An unknown block is refused **by name**, with the config key that permits it, so
the answer to "I need %mdci" is a one-line change rather than an argument.

**Scope, honestly stated.** This is a guardrail, not a jail. A submitted job runs
unjailed on a compute node as the account Aspen runs as (THREAT_MODEL §8), so a
directive that slips through is not contained by anything downstream. Two
consequences worth keeping in mind:

* the denied list below was built from the group's real usage plus ORCA features
  known to invoke external programs — it has **not** yet been checked line by line
  against the ORCA 5.0.4 and 6.0.1 manuals, both of which are installed on this
  cluster. That pass is owed before this is relied on;
* what it protects against is the *careless* member and an *injected* model, which
  is what the threat model actually targets. It does not stop a determined author,
  and inheriting something dangerous from a user's own example is the risk the
  operator already accepted.
"""

import logging
import re
from typing import Iterable, Optional

from . import config

log = logging.getLogger("aspen")


# --------------------------------------------------------------------------- #
# ORCA
# --------------------------------------------------------------------------- #
# In real use across the group's inputs. Contents of these are unrestricted.
_ORCA_BLOCKS_IN_USE = {
    "maxcore", "pal", "basis", "geom", "output", "method", "qmmm", "pointcharges",
}

# Not yet used here but ordinary science, pre-approved so the first person to need
# one is not blocked mid-conversation. Deliberately excludes anything whose job is
# to reach outside the calculation.
_ORCA_BLOCKS_COMMON = {
    "scf", "tddft", "cpcm", "smd", "cis", "casscf", "nevpt2", "mp2", "mdci",
    "freq", "elprop", "eprnmr", "rel", "nbo", "plots", "loc", "mrci", "autoci",
    "esd", "docker", "irc", "neb", "goat", "solvator", "symmetry", "constraints",
}

# The blocks that are refused, and why. These are the "truly bad things".
_ORCA_BLOCKS_DENIED = {
    "compound": ("ORCA's Compound Scripting can run a sequence of jobs and other "
                 "programs, which is arbitrary execution on the compute node"),
    "base":     ("%base redirects the run's output basename, which can write "
                 "outside the run directory"),
}

# Sub-directives that are refused wherever they appear, because each one names a
# program or a path the input should not choose.
_ORCA_DIRECTIVES_DENIED = (
    ("ProgExt", re.compile(r"\bProgExt\b", re.I),
     "ProgExt names an external program for ORCA to execute"),
    ("ProgMOPAC/ProgXTB", re.compile(r"\bProgMOPAC\b|\bProgXTB\b", re.I),
     "that directive points ORCA at an external binary by path"),
    ("ExtOpt", re.compile(r"\bExtOpt\b", re.I),
     "ExtOpt hands the optimisation to an external program"),
    ("Script=", re.compile(r"\bScript\b\s*=", re.I),
     "a Script= assignment runs something outside ORCA"),
)

# Keywords on the ``!`` line that are refused for the same reason as above. The
# rest of that line — functionals, basis sets, convergence, nbo, defgrid, … — is
# entirely open, because that is where nearly every legitimate edit lives.
_ORCA_KEYWORDS_DENIED = {
    "extopt": "ExtOpt hands the optimisation to an external program",
    "compound": "Compound scripting can execute other programs",
}

# Directives that legitimately name a file. The path must stay in the run
# directory: no absolute paths, no traversal. ORCA is given a staging directory to
# work in and has no business reading or writing outside it.
_ORCA_FILE_DIRECTIVES = re.compile(
    r"\b(moinp|inhessname|xyzfile|pointcharges|coordfile|gbwname)\b\s*"
    r"[=\s]\s*\"?([^\"\s]+)\"?", re.I
)

_ORCA_BLOCK_OPEN = re.compile(r"^\s*%\s*([A-Za-z_]+)")
_ORCA_GEOM_HEADER = re.compile(
    r"^\s*\*\s*(xyz|xyzfile|int|gzmt)\s+(-?\d+)\s+(\d+)\s*(\S+)?\s*$", re.I
)


class InputError(Exception):
    """The input file is not acceptable. Message is user-facing."""


def orca_allowed_blocks() -> set:
    """Every ``%`` block this deployment permits.

    ``ASPEN_ORCA_EXTRA_BLOCKS`` is the one-line escape hatch for a legitimate
    block nobody anticipated. It cannot re-enable a denied one — that check runs
    after, so an operator cannot accidentally allow ``%compound`` by adding it here.
    """
    extra = {b.strip().lower() for b in config.ORCA_EXTRA_BLOCKS if b.strip()}
    return (_ORCA_BLOCKS_IN_USE | _ORCA_BLOCKS_COMMON | extra) - set(_ORCA_BLOCKS_DENIED)


def _unsafe_path(value: str) -> Optional[str]:
    """Why ``value`` is not a safe in-run-directory filename, or None."""
    raw = (value or "").strip().strip('"').strip("'")
    if not raw:
        return "it is empty"
    if raw.startswith(("/", "~")):
        return "it is an absolute path"
    if ".." in raw.replace("\\", "/").split("/"):
        return "it traverses out of the run directory with '..'"
    if any(ch in raw for ch in ";|&$`\n><*?"):
        return "it contains shell or glob characters"
    return None


def validate_orca(text: str) -> list:
    """Every problem with an ORCA input, as user-facing strings. Empty = acceptable.

    Returns all problems rather than the first, so someone fixing an input hears
    about the whole set in one reply instead of one per round trip.
    """
    problems, allowed = [], orca_allowed_blocks()
    lines = (text or "").splitlines()

    if not (text or "").strip():
        return ["the input file is empty"]

    for n, line in enumerate(lines, 1):
        stripped = line.strip()

        # --- % blocks: closed vocabulary ---------------------------------- #
        opened = _ORCA_BLOCK_OPEN.match(line)
        if opened:
            name = opened.group(1).lower()
            if name in _ORCA_BLOCKS_DENIED:
                problems.append(f"line {n}: %{name} is not permitted — "
                                f"{_ORCA_BLOCKS_DENIED[name]}")
            elif name not in allowed:
                problems.append(
                    f"line {n}: %{name} is not on this deployment's list of allowed "
                    "ORCA blocks. If it is legitimate, an operator can add it to "
                    "ASPEN_ORCA_EXTRA_BLOCKS."
                )

        # --- denied sub-directives, anywhere ------------------------------ #
        for label, pattern, why in _ORCA_DIRECTIVES_DENIED:
            if pattern.search(line):
                problems.append(f"line {n}: {label} — {why}")

        # --- the ! keyword line: open, minus a couple of keywords --------- #
        if stripped.startswith("!"):
            for token in stripped[1:].replace(",", " ").split():
                why = _ORCA_KEYWORDS_DENIED.get(token.strip().lower())
                if why:
                    problems.append(f"line {n}: the {token!r} keyword — {why}")

        # --- file references must stay in the run directory --------------- #
        for match in _ORCA_FILE_DIRECTIVES.finditer(line):
            directive, value = match.group(1), match.group(2)
            bad = _unsafe_path(value)
            if bad:
                problems.append(
                    f"line {n}: {directive} points at {value!r} and {bad}; a job may "
                    "only read and write inside its own run directory"
                )

        # --- geometry header: charge/multiplicity/file --------------------- #
        geom = _ORCA_GEOM_HEADER.match(line)
        if geom:
            kind, charge, mult, ref = geom.groups()
            if abs(int(charge)) > config.ORCA_MAX_ABS_CHARGE:
                problems.append(
                    f"line {n}: charge {charge} looks wrong (limit "
                    f"±{config.ORCA_MAX_ABS_CHARGE}) — check the geometry header"
                )
            if not 1 <= int(mult) <= config.ORCA_MAX_MULTIPLICITY:
                problems.append(
                    f"line {n}: multiplicity {mult} is outside 1–"
                    f"{config.ORCA_MAX_MULTIPLICITY}"
                )
            if kind.lower() == "xyzfile" and ref:
                bad = _unsafe_path(ref)
                if bad:
                    problems.append(f"line {n}: the geometry file {ref!r} {bad}")

    problems.extend(_orca_resource_problems(text))
    return problems


def _orca_resource_problems(text: str) -> list:
    """Bound the two numbers that turn a typo into a queue-hogging job.

    Not security — a careless ``%pal nprocs 512`` is exactly the "runaway compute"
    the threat model names, and it is cheaper to catch here than in `squeue`.
    """
    problems = []
    pal = re.search(r"%\s*pal\b.*?nprocs\s+(\d+)", text, re.I | re.S)
    if pal and int(pal.group(1)) > config.ORCA_MAX_NPROCS:
        problems.append(
            f"%pal nprocs {pal.group(1)} exceeds the {config.ORCA_MAX_NPROCS}-core "
            "limit for Aspen-submitted jobs"
        )
    core = re.search(r"%\s*maxcore\s+(\d+)", text, re.I)
    if core and int(core.group(1)) > config.ORCA_MAX_MAXCORE_MB:
        problems.append(
            f"%maxcore {core.group(1)} MB exceeds the "
            f"{config.ORCA_MAX_MAXCORE_MB} MB per-process limit"
        )
    return problems


# --------------------------------------------------------------------------- #
# Dispatch — one entry point, so callers never pick a validator by hand
# --------------------------------------------------------------------------- #
VALIDATORS = {"orca": validate_orca}


def validate(text: str, code: str = "orca") -> list:
    """Problems with ``text`` as an input file for ``code``.

    An unknown code is a refusal, not a pass. Adding a code means writing its
    validator; until then Aspen must not be able to submit input for it, or the
    guardrail would be silently absent exactly where nobody has looked yet.
    """
    validator = VALIDATORS.get((code or "").strip().lower())
    if validator is None:
        return [f"no input validator exists for {code!r}, so Aspen will not write or "
                f"submit one. Supported: {', '.join(sorted(VALIDATORS))}"]
    return validator(text)


def check(text: str, code: str = "orca") -> None:
    """Raise :class:`InputError` listing every problem, or return quietly."""
    problems = validate(text, code)
    if problems:
        raise InputError(
            f"That {code.upper()} input was refused ({len(problems)} problem(s)):\n"
            + "\n".join(f"  • {p}" for p in problems)
        )


# --------------------------------------------------------------------------- #
# Structured edits — the common cases, done without rewriting the file
# --------------------------------------------------------------------------- #
def replace_geometry(text: str, *, charge: Optional[int] = None,
                     multiplicity: Optional[int] = None,
                     coordinates: Optional[Iterable] = None,
                     xyz_filename: str = "") -> str:
    """Return ``text`` with its geometry block adjusted.

    Offered alongside free editing rather than instead of it: charge, multiplicity
    and geometry are the overwhelmingly common changes, and doing them by
    substitution means the rest of a working input is copied byte for byte instead
    of being re-emitted by a model that might drop a line. Free editing stays
    available for everything else (functionals, new blocks, basis sets).

    ``coordinates`` is an iterable of ``"Sym x y z"`` lines; ``xyz_filename``
    switches the block to ``* xyzfile`` form instead. The result still goes through
    :func:`check` at the call site — this function is convenience, not a boundary.
    """
    lines = (text or "").splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if _ORCA_GEOM_HEADER.match(line):
            start = i
            break
    if start is None:
        raise InputError("that input has no '* xyz <charge> <mult>' geometry header, "
                         "so there is no geometry block to replace")

    header = _ORCA_GEOM_HEADER.match(lines[start])
    kind, old_charge, old_mult, old_ref = header.groups()
    inline = kind.lower() != "xyzfile"
    if inline:
        for j in range(start + 1, len(lines)):
            if lines[j].strip() == "*":
                end = j
                break
        if end is None:
            raise InputError("the geometry block is not closed with '*'")
    else:
        end = start

    new_charge = old_charge if charge is None else str(int(charge))
    new_mult = old_mult if multiplicity is None else str(int(multiplicity))

    if xyz_filename:
        bad = _unsafe_path(xyz_filename)
        if bad:
            raise InputError(f"the geometry file {xyz_filename!r} {bad}")
        block = [f"* xyzfile {new_charge} {new_mult} {xyz_filename}"]
    elif coordinates is not None:
        body = [str(c).rstrip() for c in coordinates if str(c).strip()]
        if not body:
            raise InputError("no coordinates were supplied")
        block = [f"* xyz {new_charge} {new_mult}", *body, "*"]
    else:
        # Charge/multiplicity only — keep whatever the block already was.
        if inline:
            block = [f"* xyz {new_charge} {new_mult}", *lines[start + 1:end], "*"]
        else:
            block = [f"* xyzfile {new_charge} {new_mult} {old_ref or ''}".rstrip()]

    return "\n".join([*lines[:start], *block, *lines[end + 1:]]) + "\n"


def coordinates_from_xyz(text: str) -> list:
    """The ``Sym x y z`` lines from a ``.xyz`` file, ready for an ORCA block.

    A ``.xyz`` file is a count, a comment, then coordinates. Only the element
    symbol and three floats are carried across — the comment line is dropped
    rather than copied, since it is free text from a file Aspen did not write and
    has no business appearing in a generated input.
    """
    lines = (text or "").splitlines()
    if len(lines) < 3:
        raise InputError("that .xyz file is too short to contain a structure")
    try:
        count = int(lines[0].strip())
    except ValueError:
        raise InputError("the first line of a .xyz file must be an atom count") from None

    out = []
    for raw in lines[2:]:
        parts = raw.split()
        if len(parts) < 4:
            continue
        symbol = parts[0]
        if not re.fullmatch(r"[A-Za-z]{1,3}\d*", symbol):
            raise InputError(f"{symbol!r} is not an element symbol")
        try:
            x, y, z = (float(p) for p in parts[1:4])
        except ValueError:
            raise InputError(f"could not read coordinates from {raw.strip()!r}") from None
        out.append(f"{symbol:<4} {x:>14.8f} {y:>14.8f} {z:>14.8f}")

    if len(out) != count:
        raise InputError(
            f"that .xyz file says {count} atoms but {len(out)} coordinate lines "
            "could be read — it may be truncated"
        )
    return out
