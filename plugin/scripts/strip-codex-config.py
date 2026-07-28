#!/usr/bin/env python3
# Remove Medley's own tables from a Codex ~/.codex/config.toml, for the uninstaller.
#
# `codex plugin add` writes three table families, and `codex plugin remove` leaves them behind once
# the plugin source is gone:
#   [plugins."medley@<market>"]                                  + dotted sub-tables, e.g.
#   [plugins."medley@<market>".mcp_servers.medley.tools.<tool>]     per-tool approval_mode
#   [marketplaces.<market>]
#   [hooks.state."medley@<market>:hooks/hooks.json:<event>:i:j"]  one per hook event (trust hashes)
#
# A stale hooks.state entry keeps a trust hash for a plugin that no longer exists, and a stale
# marketplace entry makes `codex plugin list` advertise a plugin whose source path is gone.
#
# Deliberately a separate file rather than a heredoc inside uninstall.sh: it rewrites a user's config,
# so it needs to be unit-testable on its own (see test_strip_codex_config.py) — the same reason
# edit-conflict-gate.py and session-mission-binder.py are standalone.
#
# Usage: strip-codex-config.py <config.toml> <plugin-key> <marketplace>
#          e.g. strip-codex-config.py ~/.codex/config.toml medley@medley-dev medley-dev
# Writes <config>.medley.bak before rewriting. Prints one summary line iff something was removed.
# Exit 0 always when the file is readable (nothing to do is success); 1 only on an unreadable file.
import re
import shutil
import sys

# A TOML table runs from its header line until the next header at column 0. Anything that is not one
# of ours — [projects."…"] trust entries, [mcp_servers.…], other marketplaces — is preserved
# byte-for-byte, including blank lines and comments inside it.
HEADER = re.compile(r"^\[")


def is_ours(header: str, key: str, market: str) -> bool:
    """True iff this table header belongs to the medley install being removed."""
    h = header.strip()
    return (
        # [plugins."medley@market"] and every dotted sub-table under it. The trailing-char check
        # keeps a DIFFERENT plugin whose name merely starts with ours (e.g. "medley@medley-dev-2")
        # from being swept up: after the key the next char must close the quote.
        h.startswith('[plugins."%s"' % key)
        or h == "[marketplaces.%s]" % market
        or h.startswith('[hooks.state."%s:' % key)
    )


def strip(text, key, market):
    """Return (new_text, tables_removed). Unannotated to stay runnable on an old system python3."""
    out = []
    dropping = False
    removed = 0
    for line in text.splitlines(keepends=True):
        if HEADER.match(line):
            dropping = is_ours(line, key, market)
            if dropping:
                removed += 1
                continue
        if not dropping:
            out.append(line)
    return "".join(out), removed


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: strip-codex-config.py <config.toml> <plugin-key> <marketplace>", file=sys.stderr)
        return 1
    path, key, market = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
    except Exception:
        return 1  # missing/unreadable — the caller only invokes us when it saw our key
    new, removed = strip(original, key, market)
    if not removed:
        return 0
    try:
        shutil.copyfile(path, path + ".medley.bak")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
    except Exception as err:
        print("  could not rewrite %s (%s)" % (path, err), file=sys.stderr)
        return 1
    print("  removed %d medley table(s) from %s (backup: %s.medley.bak)" % (removed, path, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
