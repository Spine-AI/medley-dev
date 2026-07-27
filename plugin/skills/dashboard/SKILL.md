---
name: dashboard
description: Open the Medley web dashboard — one shared UI listing every repo's missions, with live worker streams, approvals, steering, mission history, and the Settings page (providers, routing tiers, permission defaults). Works any time the medley plugin is loaded, no mission required.
---

# /dashboard — surface the Medley web UI

Call the `dashboard_url` tool on the `medley` MCP server (if it isn't loaded yet:
ToolSearch `"select:mcp__plugin_medley_medley__dashboard_url"`, then call it) and give the
user **exactly the URL it returns** — e.g. "Medley dashboard: http://127.0.0.1:8730/?mission=… — the
settings button (top right) has providers, routing tiers, and approval defaults."

**Relay the tool's URL verbatim — never assume `localhost` or port `8730`.** The daemon advertises the
`127.0.0.1` literal (a bare `localhost` URL hangs on macOS, which resolves it to IPv6 `::1` where the
IPv4-bound server isn't listening), and it walks to the next free port if `8730` is taken — so the
exact host and port can differ. If the user set up the branded domain (`service dashboard --setup`),
the tool returns `http://dashboard.medley:<port>/` instead.

Notes:
- **One shared dashboard for every repo.** A single background **daemon** serves all your repos, so
  the dashboard lists every repo's missions in the mission switcher. The URL the tool returns
  deep-links straight to *your current mission* (`?mission=…`) so the user lands on their own work;
  they can switch to any other mission from the header.
- The daemon outlives your session, so the URL stays live across sessions (it only changes when the
  daemon restarts, e.g. after an upgrade or reboot). This skill always returns the current one.
- Outside a Claude Code session, the daemon's dashboard is usually already up; check with
  `"$(cat ~/.medley/engine-path)" service status` (the engine is a self-contained binary — run it
  directly). If none is running, start one with `"$(cat ~/.medley/engine-path)" service start`, or
  open the dashboard in the browser directly with `"$(cat ~/.medley/engine-path)" service dashboard --open`.
- If the tool reports the server isn't running, relay that honestly — don't invent a URL.
