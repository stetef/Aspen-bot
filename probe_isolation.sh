#!/usr/bin/env bash
# probe_isolation.sh — characterize this host's user-namespace / bwrap / seccomp /
# cgroups / IPC facts that drive Aspen's isolation design (sibling split, Unix-socket
# IPC, Option-1 seccomp on the analysis jail, the service-account move).
#
# READ-ONLY. It creates no users, changes no config, opens no network, and writes
# only inside a private temp dir it deletes on exit.
#
# RUN IT AS THE ACCOUNT ASPEN ACTUALLY USES (the service account, or your normal
# login) FROM A PLAIN SHELL:
#   - NOT inside a Claude Code session  -> a nested Claude session can apply a
#     seccomp filter / its own userns, which skews the "current process" readings.
#   - NOT as root                       -> root would mis-report the *unprivileged*
#     capabilities the bot really has.
#
# Usage:
#   bash probe_isolation.sh [ASPEN_DIR]
#     ASPEN_DIR  optional path to the Aspen-bot checkout, to audit .env/secret perms.
#
# Always exits 0 — it's a report, not a gate. Read the SUMMARY block at the end.

set -u

ASPEN_DIR="${1:-}"
INIT_USERNS="user:[4026531837]"   # the host/init user namespace inode
declare -A R                      # decision-relevant results for the summary

P() { printf '[PASS] %s\n' "$*"; }
F() { printf '[FAIL] %s\n' "$*"; }
I() { printf '[INFO] %s\n' "$*"; }
W() { printf '[WARN] %s\n' "$*"; }
hr(){ printf '\n========== %s ==========\n' "$*"; }

have(){ command -v "$1" >/dev/null 2>&1; }
# run quietly with a hard timeout and stdin detached from the tty, so a
# misbehaving nested shell can never block the probe on terminal input.
if command -v timeout >/dev/null 2>&1; then _TO="timeout 10"; else _TO=""; fi
q(){ $_TO "$@" >/dev/null 2>&1 </dev/null; }

TMP="$(mktemp -d 2>/dev/null || echo /tmp/probe.$$)"; mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------
hr "context"
I "date         : $(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"
I "host         : $(hostname 2>/dev/null)"
I "kernel       : $(uname -rm 2>/dev/null)"
I "whoami       : $(id 2>/dev/null)"

if [ "$(id -u)" = "0" ]; then
  W "running as ROOT — re-run as the unprivileged bot account; results below over-report privilege."
  R[as_root]=yes
else
  R[as_root]=no
fi

if [ -n "${CLAUDECODE:-}${CLAUDE_CODE_ENTRYPOINT:-}${CLAUDE_CODE_CHILD_SESSION:-}${CLAUDE_CODE_SANDBOXED:-}" ]; then
  W "a Claude Code session is in the environment (CLAUDECODE/CLAUDE_CODE_* set)."
  W "  -> 'current process' seccomp/caps and bwrap may reflect Claude's wrapper, not the bot."
  W "  -> open a plain terminal/SSH session and re-run for a clean reading."
  R[claude_nested]=yes
else
  R[claude_nested]=no
fi

# ---------------------------------------------------------------------------
hr "current process confinement"
SECCOMP="$(awk '/^Seccomp:/{print $2}' /proc/self/status 2>/dev/null)"
NNP="$(awk '/^NoNewPrivs:/{print $2}' /proc/self/status 2>/dev/null)"
CAPEFF="$(awk '/^CapEff:/{print $2}' /proc/self/status 2>/dev/null)"
MYNS="$(readlink /proc/self/ns/user 2>/dev/null)"
case "$SECCOMP" in
  0|"") I "Seccomp filter on this shell : none (Seccomp=${SECCOMP:-?})"; R[seccomp_filter]=none ;;
  1)    W "Seccomp=1 (STRICT mode) on this shell";                       R[seccomp_filter]=strict ;;
  2)    W "Seccomp=2 (a BPF FILTER is active) on this shell — readings may be constrained"; R[seccomp_filter]=filter ;;
  *)    I "Seccomp=$SECCOMP";                                            R[seccomp_filter]="$SECCOMP" ;;
esac
I "NoNewPrivs   : ${NNP:-?}"
I "CapEff       : ${CAPEFF:-?}  (0000000000000000 = no effective caps, expected for unprivileged)"
if [ "$MYNS" = "$INIT_USERNS" ]; then
  P "user namespace: init/host ($MYNS) — a clean top-level reading"
  R[in_init_userns]=yes
else
  W "user namespace: $MYNS (NOT init $INIT_USERNS) — you're already inside a userns (container/sandbox?)"
  R[in_init_userns]=no
fi

# ---------------------------------------------------------------------------
hr "tooling"
for t in bwrap unshare newuidmap newgidmap prlimit apptainer singularity podman socat python3; do
  if have "$t"; then P "$(printf '%-11s' "$t") $(command -v "$t")"
  else               I "$(printf '%-11s' "$t") (absent)"; fi
done
R[bwrap]=$(have bwrap && echo yes || echo no)
R[apptainer]=$( (have apptainer || have singularity) && echo yes || echo no)
if have bwrap; then I "bwrap version: $(bwrap --version 2>/dev/null)"; fi
if have apptainer; then I "apptainer version: $(apptainer --version 2>/dev/null)"; fi

# ---------------------------------------------------------------------------
hr "kernel sysctls (user namespaces)"
for k in user.max_user_namespaces kernel.unprivileged_userns_clone user.max_net_namespaces; do
  v="$(sysctl -n "$k" 2>/dev/null)"
  if [ -n "$v" ]; then I "$k = $v"; else I "$k = (not present)"; fi
done

# ---------------------------------------------------------------------------
hr "subordinate UID/GID allocation (for the /etc/subuid mapping route)"
U="$(id -un 2>/dev/null)"
SUB="$(grep -E "^($U|$(id -u)):" /etc/subuid 2>/dev/null)"
SUG="$(grep -E "^($U|$(id -u)):" /etc/subgid 2>/dev/null)"
if [ -n "$SUB" ] || [ -n "$SUG" ]; then
  P "subuid/subgid range allocated to you:"; [ -n "$SUB" ] && I "  subuid: $SUB"; [ -n "$SUG" ] && I "  subgid: $SUG"
  R[subuid]=yes
else
  I "no /etc/subuid|subgid range for you (subuid mapping route unavailable without admin)"
  R[subuid]=no
fi

# ---------------------------------------------------------------------------
hr "user-namespace creation"
# Top-level userns WITH a uid map (the meaningful test; -r maps you to root inside).
if q unshare -Ur true; then P "top-level unprivileged userns (mapped): WORKS"; R[userns_toplevel]=yes
else                        F "top-level unprivileged userns (mapped): FAILS"; R[userns_toplevel]=no; fi

# Nested userns, each level mapped (kernel-level feasibility of stacking namespaces).
if q unshare -Ur sh -c 'unshare -Ur true'; then P "NESTED userns (both mapped): WORKS"; R[userns_nested]=yes
else                                           I "NESTED userns (both mapped): fails"; R[userns_nested]=no; fi

# How deep can unprivileged mapped userns nest? (informational, fixed-depth
# literal tests — no recursive quoting, each stdin-detached + timed out via q()).
d=1
q unshare -Ur sh -c 'unshare -Ur true' && d=2
[ "$d" = 2 ] && q unshare -Ur sh -c 'unshare -Ur sh -c "unshare -Ur true"' && d=3
I "max unprivileged mapped userns nesting depth observed: >= $d"

# Common footgun reproduction: userns WITHOUT a map can't spawn a child userns.
if q unshare -U sh -c 'unshare -U true'; then I "(unmapped) nested userns also works"
else I "(note) an UNMAPPED userns cannot create a child userns — expected; bwrap/apptainer always map, so this is not a real blocker"; fi

# ---------------------------------------------------------------------------
hr "bwrap (the analysis-jail engine)"
if have bwrap; then
  # Top level — this is exactly what the bot needs for run_python_analysis.
  if q bwrap --unshare-all --ro-bind / / true; then
    P "bwrap at TOP LEVEL (bot's analysis jail): WORKS"; R[bwrap_toplevel]=yes
  elif q bwrap --unshare-user --unshare-net --unshare-ipc --unshare-pid --unshare-uts --ro-bind / / true; then
    P "bwrap at TOP LEVEL (without --unshare-cgroup): WORKS"; R[bwrap_toplevel]=yes
    W "  --unshare-all failed but per-namespace unshare worked — likely a cgroup-namespace quirk"
  else
    F "bwrap at TOP LEVEL: FAILS — the analysis jail will not start here"; R[bwrap_toplevel]=no
  fi

  # Does bwrap support --seccomp? (needed for Option 1: tighten the jail's syscall surface)
  if bwrap --help 2>&1 | grep -q -- '--seccomp'; then
    P "bwrap supports --seccomp (Option 1: add a syscall filter to the jail)"; R[bwrap_seccomp]=yes
  else
    W "bwrap does NOT advertise --seccomp — Option 1 would need a newer bwrap"; R[bwrap_seccomp]=no
  fi

  # bwrap nested inside a mapped userns = the apptainer-inner proxy. You've decided
  # NOT to use this; reported only to quantify the cost (it needs the outer runtime
  # to NOT impose no_new_privs / a clone-blocking seccomp filter).
  if q unshare -Ur sh -c 'bwrap --unshare-all --ro-bind / / true'; then
    I "bwrap nested in a mapped userns: WORKS at the kernel level"
    I "  -> bwrap-inside-Apptainer is blocked only by Apptainer's OWN no_new_privs/seccomp,"
    I "     which is exactly the profile you'd have to loosen. Decision stands: don't nest."
    R[bwrap_nested]=yes
  else
    I "bwrap nested in a mapped userns: fails here (so nesting would need kernel/seccomp changes)"
    R[bwrap_nested]=no
  fi
else
  F "bwrap absent — the analysis jail cannot run"; R[bwrap_toplevel]=no; R[bwrap_seccomp]=no; R[bwrap_nested]=no
fi

# ---------------------------------------------------------------------------
hr "Apptainer confinement profile (informational; you are NOT nesting bwrap in it)"
if have apptainer || have singularity; then
  conf=""
  for c in /etc/apptainer/apptainer.conf /etc/singularity/singularity.conf; do
    [ -r "$c" ] && conf="$c" && break
  done
  if [ -n "$conf" ]; then
    I "readable config: $conf"
    grep -iE '^\s*(allow setuid|allow user ns|allow pid ns|systemd cgroups|root default capabilities)\b' "$conf" 2>/dev/null \
      | sed 's/^/      /' || true
  else
    I "no readable apptainer/singularity config (would need it to confirm its seccomp/no_new_privs policy)"
  fi
  I "to definitively test bwrap-inside-apptainer (if ever revisited), with an image you trust:"
  I "    apptainer exec <image.sif> bwrap --unshare-all --ro-bind / / true"
else
  I "apptainer/singularity absent"
fi

# ---------------------------------------------------------------------------
hr "cgroups (per-task memory limits)"
CGT="$(stat -fc %T /sys/fs/cgroup 2>/dev/null)"
case "$CGT" in
  cgroup2fs) I "cgroups v2 (unified) — rootless memory limits possible"; R[cgroups]=v2 ;;
  tmpfs)     I "cgroups v1 (legacy)  — rootless --memory unavailable; keep prlimit RLIMIT_AS as the cap"; R[cgroups]=v1 ;;
  *)         I "cgroups fstype: ${CGT:-unknown}"; R[cgroups]="${CGT:-unknown}" ;;
esac

# ---------------------------------------------------------------------------
hr "sibling IPC: Unix-domain socket + SO_PEERCRED"
if have python3; then
  out="$(python3 - <<'PY' 2>/dev/null
import socket, struct
a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
sz = struct.calcsize('3i')
pid, uid, gid = struct.unpack('3i', a.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, sz))
print(f"ok pid={pid} uid={uid} gid={gid}")
PY
)"
  if [ -n "$out" ]; then
    P "AF_UNIX + SO_PEERCRED works ($out) — kernel-verified peer identity for the bot<->tool-server link"
    R[unix_socket]=yes
  else
    W "python3 present but SO_PEERCRED test produced no output"; R[unix_socket]=unknown
  fi
else
  I "python3 absent — cannot self-test SO_PEERCRED (AF_UNIX is still available)"; R[unix_socket]=unknown
fi

# ---------------------------------------------------------------------------
hr "secret exposure on this (shared) host"
check_perm() {  # $1 path  $2 label
  local p="$1" lbl="$2"
  [ -e "$p" ] || { I "$lbl: not present ($p)"; return; }
  local mode owner; mode="$(stat -c '%A' "$p" 2>/dev/null)"; owner="$(stat -c '%U:%G' "$p" 2>/dev/null)"
  local oth="${mode:7:3}"   # other-permission triad
  if printf '%s' "$oth" | grep -q 'r'; then
    F "$lbl is OTHER-READABLE  $mode $owner  $p"
    return 1
  else
    P "$lbl not other-readable $mode $owner  $p"
  fi
}
ENV_BAD=no
if [ -n "$ASPEN_DIR" ]; then
  check_perm "$ASPEN_DIR/.env" ".env" || ENV_BAD=yes
  if have getfacl && [ -e "$ASPEN_DIR/.env" ]; then
    I "ACL on .env (verify no extra read grants):"; getfacl -p "$ASPEN_DIR/.env" 2>/dev/null | sed 's/^/      /'
  fi
  check_perm "$ASPEN_DIR" "repo dir" || true
else
  I "no ASPEN_DIR given — pass it to audit .env perms, e.g.: bash $0 /path/to/Aspen-bot"
fi
check_perm "$HOME/.ssh/id_ed25519"            "ssh private key" || ENV_BAD=yes
check_perm "$HOME/.claude/.credentials.json"  "claude credentials" || true
R[env_world_readable]="$ENV_BAD"

# Other local users present? (rough shared-node signal)
NU="$(getent passwd 2>/dev/null | awk -F: '$3>=1000 && $3<60000 {c++} END{print c+0}')"
[ -n "$NU" ] && I "local accounts with uid>=1000: ~$NU (shared node if many)"

# ---------------------------------------------------------------------------
hr "SUMMARY — what this means for the design"
sv(){ printf '  %-46s %s\n' "$1" "${R[$2]:-?}"; }
echo "Reading is CLEAN only if both of these are reassuring:"
sv "run inside a Claude session (skews readings)" claude_nested
sv "running as root (over-reports privilege)" as_root
sv "current shell in init userns" in_init_userns
echo
echo "Core capabilities:"
sv "bwrap analysis jail runs (top level)" bwrap_toplevel
sv "bwrap --seccomp available (Option 1)" bwrap_seccomp
sv "AF_UNIX + SO_PEERCRED (sibling IPC)" unix_socket
sv "cgroups version (memory-limit story)" cgroups
echo
echo "Nesting (you chose NOT to nest bwrap in Apptainer):"
sv "kernel allows nested mapped userns" userns_nested
sv "bwrap nests in a userns" bwrap_nested
echo "  -> if 'yes', the only blocker to nesting is Apptainer's own seccomp/no_new_privs"
echo "     profile; loosening it re-arms unprivileged CLONE_NEWUSER (a top local-root"
echo "     primitive). Sibling domains avoid this entirely."
echo
echo "Identity / secrets:"
sv "subuid range without admin" subuid
echo "  -> a SEPARATE lower-privileged UID always needs admin (useradd). Sibling"
echo "     service accounts are the real identity boundary; confinement is not."
sv "secrets OTHER-readable on this host (bad)" env_world_readable
echo
echo "Decision checklist:"
echo "  [*] bwrap_toplevel=yes      -> keep bwrap as the analysis jail"
echo "  [*] bwrap_seccomp=yes       -> do Option 1 (add --seccomp to the jail)"
echo "  [*] unix_socket=yes         -> replace 127.0.0.1 TCP with AF_UNIX + SO_PEERCRED"
echo "  [*] env_world_readable=no   -> if 'yes', chmod 600 the .env NOW"
echo "  [*] sibling UIDs            -> file the service-account ticket (needs admin)"
echo
I "done."
exit 0
