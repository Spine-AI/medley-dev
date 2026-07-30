#!/usr/bin/env bash
# Tests uninstall.sh's PLAN against a fake $HOME — `--dry-run` only, so nothing is ever removed and the
# real ~/.medley, ~/.claude and ~/.codex are never touched.
#
# What matters here:
#   • the DAEMON is announced for removal even with --keep-data (it is part of the plugin);
#   • plugin-data dirs are claimed by OWNERSHIP (a `bin/medley-engine-*` inside), never by name, so a
#     third-party `medley-foo@bar` is left alone while the dev and inline channels are swept;
#   • the Codex config.toml detection is as precise as strip-codex-config.py, so the STABLE uninstaller
#     stops promising to edit a config that only holds `medley@medley-dev` tables;
#   • the engine-backed and fallback path lists agree, since a user on an engine older than
#     `service purge-plan` gets the fallback.
# Run: bash plugin/scripts/test_uninstall.sh
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNINSTALL="$DIR/uninstall.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0

pass() { echo "  ok: $1"; }
check() { # $1=haystack $2=needle $3=label — needle MUST be present
  case "$1" in *"$2"*) pass "$3" ;; *) echo "FAIL [$3]: expected to find '$2'"; fail=1 ;; esac
}
refute() { # $1=haystack $2=needle $3=label — needle must NOT be present
  case "$1" in *"$2"*) echo "FAIL [$3]: did NOT expect '$2'"; fail=1 ;; *) pass "$3" ;; esac
}

# ── a fake machine with medley installed on both hosts, on three channels ──────────────────────────
HOME_DIR="$tmp/home"
CC_DATA_ROOT="$HOME_DIR/.claude/plugins/data"
CX_DATA_ROOT="$HOME_DIR/.codex/plugins/data"
mkdir -p "$HOME_DIR/.medley/state" "$HOME_DIR/.medley/bin" "$HOME_DIR/.codex"

make_data_dir() { # $1=root $2=name $3=1 to include an engine binary (= ownership proof)
  mkdir -p "$1/$2/bin"
  if [ "$3" = 1 ]; then
    printf '#!/bin/bash\nexit 1\n' > "$1/$2/bin/medley-engine-0.9.0"
    chmod +x "$1/$2/bin/medley-engine-0.9.0"
  fi
}
make_data_dir "$CC_DATA_ROOT" medley-medley 1        # stable, Claude Code
make_data_dir "$CC_DATA_ROOT" medley-inline 1        # `claude --plugin-dir` dev install
make_data_dir "$CX_DATA_ROOT" medley-medley-dev 1    # dev channel, Codex
make_data_dir "$CC_DATA_ROOT" medley-foo-bar 0       # third-party `medley-foo@bar` — NOT ours
make_data_dir "$CC_DATA_ROOT" medley-probe-market 0  # ours by name, no binary → nothing to reclaim

# Daemon + regenerable artifacts that must show up in the plan.
: > "$HOME_DIR/.medley/bin/medley-daemon"
: > "$HOME_DIR/.medley/bin/medley-engine"
: > "$HOME_DIR/.medley/bin/medley-mcp"
: > "$HOME_DIR/.medley/statusline.sh"
: > "$HOME_DIR/.medley/engine-path"
: > "$HOME_DIR/.medley/statusline-autowired"
: > "$HOME_DIR/.medley/state/daemon.log"
# Data that must be kept under --keep-data.
: > "$HOME_DIR/.medley/state/medley.db"
: > "$HOME_DIR/.medley/state/config.toml"
: > "$HOME_DIR/.medley/state/openrouter.key"

# A Codex config holding ONLY stable-channel tables. The DEV uninstaller must not claim it.
cat > "$HOME_DIR/.codex/config.toml" <<'TOML'
[projects."/some/repo"]
trust_level = "trusted"

[hooks.state."medley@medley:hooks/hooks.json:stop:0:0"]
trusted_hash = "abc"

[marketplaces.medley]
source = "https://github.com/Spine-AI/medley.git"

[plugins."medley@medley"]
enabled = true
TOML

run_plan() { # $@ = extra uninstall.sh flags; always includes --dry-run
  HOME="$HOME_DIR" TMPDIR="$tmp/tmpdir" MEDLEY_ENGINE="${STUB_ENGINE:-/nonexistent}" \
    bash "$UNINSTALL" --dry-run "$@" </dev/null 2>&1
}
mkdir -p "$tmp/tmpdir"

# ── 1. fallback mode (engine absent / too old — what shipped users hit today) ───────────────────────
echo "fallback mode (no usable engine):"
out="$(run_plan)"
check "$out" "the shared daemon" "announces the daemon"
check "$out" "$CC_DATA_ROOT/medley-medley " "sweeps the stable Claude Code binaries"
check "$out" "$CC_DATA_ROOT/medley-inline " "sweeps an inline (--plugin-dir) dev install"
check "$out" "$CX_DATA_ROOT/medley-medley-dev " "sweeps the dev channel under ~/.codex"
refute "$out" "medley-foo-bar" "leaves a third-party medley-* plugin alone"
refute "$out" "medley-probe-market" "ignores a medley-* dir with no engine binary"
check "$out" "ALL of $HOME_DIR/.medley" "default removes the data too"
refute "$out" "medley entries in $HOME_DIR/.codex/config.toml" "dev does not claim stable-only Codex tables"

# ── 2. --keep-data still removes the daemon ────────────────────────────────────────────────────────
echo "--keep-data:"
out="$(run_plan --keep-data)"
check "$out" "KEEPING your data" "says the data is kept"
check "$out" "The daemon still goes." "is explicit that the daemon is removed anyway"
refute "$out" "ALL of $HOME_DIR/.medley" "does not announce a full wipe"
# The regression this closes: --keep-data used to preserve ~/.medley/bin wholesale, i.e. the entire
# daemon (trampoline + the ~83MB TCC-stable hard link).
check "$out" "the launcher, the TCC-stable exec link" "still removes the launcher + exec link"

# ── 3. the engine's plan is preferred when the binary answers ──────────────────────────────────────
# A stub that implements `service purge-plan --paths`; the plan must come from IT, not the fallback.
echo "engine-backed mode:"
STUB="$tmp/stub-engine"
cat > "$STUB" <<STUBEOF
#!/bin/bash
if [ "\$1" = "service" ] && [ "\$2" = "purge-plan" ]; then
  printf 'daemon\t%s\n' "$HOME_DIR/.medley/bin/medley-daemon"
  printf 'regenerable\t%s\n' "$HOME_DIR/.medley/only-the-engine-knows-this-one"
  exit 0
fi
exit 1
STUBEOF
chmod +x "$STUB"
out="$(STUB_ENGINE="$STUB" run_plan)"
check "$out" "2 daemon + regenerable path(s)" "uses the engine's list verbatim"
check "$out" "see the exact list:" "offers the purge-plan command when the engine supports it"

# An engine that does NOT know the subcommand must fall back silently — and must NOT advertise it.
echo "old-engine mode:"
OLD="$tmp/old-engine"; printf '#!/bin/bash\nexit 2\n' > "$OLD"; chmod +x "$OLD"
out="$(STUB_ENGINE="$OLD" run_plan)"
refute "$out" "see the exact list:" "hides the hint on an engine without purge-plan"
check "$out" "$CC_DATA_ROOT/medley-medley " "still sweeps the data dirs from the fallback"

# ── 4. dry run really is dry ───────────────────────────────────────────────────────────────────────
echo "dry run is dry:"
for f in bin/medley-daemon bin/medley-engine statusline.sh state/medley.db state/openrouter.key; do
  if [ ! -e "$HOME_DIR/.medley/$f" ]; then echo "FAIL: --dry-run removed $f"; fail=1; fi
done
if [ ! -d "$CC_DATA_ROOT/medley-medley" ]; then echo "FAIL: --dry-run removed a data dir"; fail=1; fi
pass "nothing was removed"

if [ "$fail" = 0 ]; then echo "ok: uninstall plan"; else exit 1; fi
