# Plan — meviusisback.dropdown-terminal

**Date:** 2026-09-06 10:40 · **Status:** awaiting user approval
**Goal:** a persistent drop-down terminal summoned from the top with a smooth drop animation, on an unclaimed SUPER+letter keybind, shipped as an Omarchy shell plugin.

## Current context / assumptions

- Omarchy on Hyprland v0.56.2 (Lua config engine), Quickshell 0.3.1 shell, foot 1.27.0, Fish shell, single monitor 2560x1440.
- Keybind survey (live `hyprctl binds -j`, 232 binds): **SUPER+U is completely free** — the only letter with zero SUPER+anything. Also safe: Q, Z, H, I, M, N, R, Y (only modified variants exist there). All other letters conflict.
- No windowrule/layerrule mentions `foot` app-ids other than the terminal tag regex `(Alacritty|kitty|com.mitchellh.ghostty|foot|org\.codeberg\.dnkl\.foot|wezterm|org\.omarchy\..*|TUI\..*)` — my app-id `org.omarchy.dropdown-terminal` will match the `org.omarchy.*` terminal tag (fine: gets terminal theming/opacity; I will NOT rely on `org.omarchy.*` exclusivity, rules target the full exact class).
- Scratchpad (`SUPER+S`, `hl.dsp.workspace.toggle_special("scratchpad")`) is the closest precedent but is slot-based and doesn't drop from the top. A dedicated special workspace + window rules gives the true dropdown geometry + slide animation + persistence.
- The user's keybinds plugin edits `~/.config/hypr/bindings.lua` with sanitized `o.bind()` lines; the workspace-layout plugin installs a generated lua file via a `dofile` hook line appended to `~/.config/hypr/hyprland.lua` (exact line visible there today). This plugin reuses that **workspace-layout pattern**.
- Repo: Orca-managed bare repo `/home/alberto/orca/projects/dropdown-terminal`, worktree branch `Main` (initial commit `e73580c`, no remote yet). Plugin is created in-place on this branch.
- foot ships `/usr/lib/systemd/user/foot-server.service` (non-parameterized, default app-id). We ship our own parameterized unit so the default foot usage is untouched.

## Architecture

Two cooperating halves. Zero network, zero `shell=True`, subprocess argv-arrays only, all file writes atomic with fixed markers.

```
┌─ Hyprland (Lua engine) ─────────────────────────────────────────────────┐
│ bindings.lua:  o.bind("SUPER + U", "Toggle drop-down terminal",          │
│                     "omarchy-dropdown-terminal toggle")                  │
│ dropdown-terminal.lua (plugin-installed, dofile'd from hyprland.lua):    │
│   windowrules for class "org.omarchy.dropdown-terminal":                 │
│   workspace special:dropdown, float, size {monitor_w, monitor_h*55/100}, │
│   pin = true, move {(monitor_w/2), (0-monitor_h*9/100)},                 │
│   animation "slide top", border_size 0, stay_focused                     │
└──────────────────────────────────────────────────────────────────────────┘
                     │ keybind → argv-only exec (no shell)
                     ▼
┌─ bin/omarchy-dropdown-terminal (bash) ───────────────────────────────────┐
│ ensure foot server (systemd --user foot-server@dropdown-terminal) →      │
│ hyprctl dispatch togglespecialworkspace dropdown → focus →               │
│ toggle-eater re-show fix (argv-only hyprctl -j clients check)            │
└──────────────────────────────────────────────────────────────────────────┘
                     │ togglespecialworkspace → Hyprland plays
                     ▼ specialWorkspaceIn/Out (slidevert, easeOutQuint)
┌─ foot server (systemd user unit, id "dropdown-terminal") ────────────────┐
│ One long-lived Wayland client, app-id org.omarchy.dropdown-terminal.     │
│ Every dropdown summon = footclient attaching to the SAME server →        │
│ the same shell session (scrollback, vim, ssh…) persists while hidden.    │
└──────────────────────────────────────────────────────────────────────────┘
```

**Why a foot server:** if the toggle key spawned/killed a terminal, scrollback and running jobs would die on every hide — violating requirement #1. `foot --server` keeps one long-lived Wayland client alive; `footclient` windows attach instantly (no startup latency) and the shell session persists as long as the server lives. systemd gives clean lifecycle + auto-restart; self-healing if the server dies (next toggle restarts it).

- **Persistence:** server survives toggles, workspace switches, monitor changes; ends only on logout/manual kill. Special workspace + `pin = true` keeps the window out of the tiling flow so it never steals a tiling slot.
- **Animation:** `togglespecialworkspace` triggers `specialWorkspaceIn`/`Out` — default `slidevert` easeOutQuint speed 3, already enabled in looknfeel.lua line 89 — a genuine top slide. Belt-and-braces: rule `animation = "slide top"` + `border_size 0`.
- **Geometry:** width 100% (Quake style), height 55%, y = `0-monitor_h*9/100` so it hangs flush off the top edge (accounts for border), centered horizontally.
- **Keybind:** SUPER+U (surveyed free). Install appends ONE marker-marked `o.bind(...)` line to `bindings.lua` + ONE dofile hook line to `hyprland.lua`, both idempotent, then `hyprctl reload`. Nothing else touched. Uninstall removes exactly those lines.
- **Plugin shape:** `manifest.json` `kinds: ["bar-widget"]` → bar button shows state (up/down), left-click toggle, right-click menu (toggle / kill server). IPC `IpcHandler` target `meviusisback.dropdown-terminal` with `open`/`close`/`toggle`/`status`/`kill`; Process calls argv-only. Menu/panel summoning also works.

## File map (all paths relative to repo root)

| File | Purpose |
|---|---|
| `manifest.json` | Plugin manifest; `kinds: ["bar-widget"]`, entryPoints barWidget → `Panel.qml` |
| `Panel.qml` | Bar button + IPC (open/close/toggle/status/kill); argv-only Process calls to `bin/omarchy-dropdown-terminal`; `textFormat: Text.PlainText` everywhere |
| `bin/omarchy-dropdown-terminal` | CLI: `toggle\|open\|close\|status\|kill\|install\|uninstall` |
| `backend/dropdown_terminal.py` | Idempotent install/uninstall of lua rules file + hook + bind line; status reporting (argv-only `hyprctl -j`) |
| `backend/foot-server@.service` | Parameterized systemd user unit (app-id override, PartOf graphical-session.target) |
| `install.sh` | One-shot installer: unit → `~/.config/systemd/user/`, daemon-reload, enable --now, run backend install, `hyprctl reload` |
| `install.sh uninstall` | Full revert (bind line, hook line, rules file, unit, server kill) |
| `README.md` | Install, keybind, usage, architecture, uninstall |
| `tests/test_backend.py` | pytest: install idempotency, marker integrity, uninstall removes all traces |

## Final Hyprland rules file (installed to `~/.config/hypr/dropdown-terminal.lua`)

```lua
-- BEGIN meviusisback.dropdown-terminal (generated; do not edit)
local ddws = "special:dropdown"

-- Drop-down terminal: full-width top panel on its own special workspace.
o.window("org.omarchy.dropdown-terminal", {
  workspace = ddws,
  float = true,
  pin = true,
  size = { "(monitor_w)", "(monitor_h*55/100)" },
  move = { "(monitor_w/2)", "(0-monitor_h*9/100)" },
  animation = "slide top",
  border_size = 0,
  stay_focused = true,
})
-- END meviusisback.dropdown-terminal
```

(The `on-created-empty` idea was dropped — it's a workspace-rule option, not a window-rule property, and the bash script handles first-focus.)

## Step-by-step tasks

### Phase A — skeleton
1. Dirs `bin/ backend/ tests/ assets/` in the worktree; `manifest.json` (id `meviusisback.dropdown-terminal`, version 0.1.0, author meviusisback, MIT, displayName "Drop-down terminal", category "Compositor").

### Phase B — Hyprland integration backend
2. `backend/dropdown_terminal.py`:
   - `install()`: (a) write rules file above (constant string → `~/.config/hypr/dropdown-terminal.lua`, temp+`os.replace`); (b) if `dropdown-terminal.lua` hook line absent in `~/.config/hypr/hyprland.lua`, append the workspace-layout-style dofile line; (c) if literal `SUPER + U` absent from `~/.config/hypr/bindings.lua`, append the `o.bind` line. All lines are fixed constants — no user data interpolation, no format strings.
   - `uninstall()`: remove hook line, rules file, bind line (marker-matched), `systemctl --user disable --now foot-server@dropdown-terminal.service`, `pkill -f 'foot --server.*dropdown-terminal'` (argv array, no shell).
   - `status()`: parse `hyprctl clients -j` via `json.loads(subprocess.run([...], capture_output=True).stdout)`; report server unit state, window address, workspace visibility.
3. `bin/omarchy-dropdown-terminal` (bash, `set -euo pipefail`, no eval):
   - `ensure_server`: `systemctl --user is-active foot-server@dropdown-terminal.service || systemctl --user start foot-server@dropdown-terminal.service` (short sleep-wait loop with timeout).
   - `toggle`: ensure_server → `hyprctl dispatch togglespecialworkspace dropdown` → focus dropdown window if now visible → toggle-eater fix: argv-only `hyprctl -j clients` check; if a hidden special-window client still exists after a hide toggle, re-dispatch show once (exact fix verified live at code time).
   - `open`/`close` explicit; `status` prints JSON; `install`/`uninstall` delegate to the python backend.
4. `backend/foot-server@.service`:
   ```ini
   [Unit]
   Description=Foot terminal server (dropdown terminal, instance %i)
   PartOf=graphical-session.target
   After=graphical-session.target
   ConditionEnvironment=WAYLAND_DISPLAY

   [Service]
   ExecStart=/usr/bin/foot --server=3 --app-id=org.omarchy.dropdown-terminal
   Restart=on-failure
   NonBlocking=true
   UnsetEnvironment=LISTEN_PID LISTEN_FDS LISTEN_FDNAMES

   [Install]
   WantedBy=graphical-session.target
   ```
   Install path: `~/.config/systemd/user/foot-server@.service`, then `systemctl --user daemon-reload && systemctl --user enable --now foot-server@dropdown-terminal.service`.

### Phase C — plugin surface
5. `Panel.qml`: bar icon button (terminal glyph) with open/closed state, tooltip, left-click `toggle`, right-click mini-menu (Toggle / Kill server); `IpcHandler` target `meviusisback.dropdown-terminal` (`open`, `close`, `toggle`, `status`, `kill`); `Process` with argv arrays only; all dynamic text rendered `textFormat: Text.PlainText`; no RichText/StyledText anywhere.
6. `install.sh` + uninstall path (wraps backend; verifies foot + systemd present; friendly errors).
7. `tests/test_backend.py`: idempotent double-install produces identical files; uninstall removes hook/bind/rules/unit; bind line constant exactly matches spec.

### Phase D — verification
8. `omarchy plugin validate <folder>` passes.
9. Live: run `install.sh`, press SUPER+U → terminal drops from top; press again → hides; open a program in it (e.g. `htop`), hide, switch workspaces, re-summon → **same session still running**; restart shell (`omarchy restart shell`) → keybind + special workspace still work (hyprland-level, independent of shell); foot server unit survives `systemctl --user restart graphical-session.target`… (target restart not forced; unit verified enabled).
10. Toggle-eater behavior exercised at least 10 rapid toggles; no stuck/ghost windows (`hyprctl clients` clean).

### Phase E — review + ship
11. Security review per omarchy-plugin-dev Phase 4 (2 base reviewers + targeted injection + path-traversal reviewers — diff has file I/O + subprocess).
12. Docs, README, infographic; conventional commits (code, then docs); push branch; recap + ask before PR.

## Risks / tradeoffs / open questions

- **specialWorkspace animation ≠ perfectly flush dropdown:** the slidevert animation slides the whole special workspace layer vertically — visually very close to a Quake dropdown. If Alberto wants pixel-perfect "slides only from top edge", alternative is a Quickshell `PanelWindow` hosting an embedded terminal — but Quickshell 0.3.1 has no terminal-emulator component, so a real terminal requires embedding a foreign Wayland surface, which is not feasible cleanly. Chosen approach is the standard, robust one (same class of solution as `tdrop`/`la-terminal` scripts).
- **First summon latency:** footclient attaches in milliseconds; no perceptible delay expected.
- **Toggle-eater bug:** known Hyprland quirk with special-workspace windows; the script includes the re-show fix and it's verified live in Phase D.
- **SUPER+U muscle memory:** U sits near the left hand cluster (I, O, P area is right; U is right-hand top row) — easy reach; can switch to Q or Z (also free of plain SUPER) trivially by editing one line.
- **Monitor changes:** rules use `(monitor_w/H)` formulas, so it adapts without re-install.
- **Open question for Alberto:** height 55% OK? Keybind SUPER+U OK? (Alternatives: Q/Z/H/M/N — all safe.)

## Security review of the plan itself

**Subagent review verdict: `safe: true`, no design-level blockers.** Hardening commitments adopted from the review (mandatory at code time):

1. **No `pkill -f`** — kill the server only via systemd (`systemctl --user kill --kill-whom=main foot-server@dropdown-terminal.service`); a cmdline-regex kill can hit unrelated same-UID processes.
2. **Uninstall ordering:** `disable --now` → poll `is-active` until inactive (timeout) → residual kill → optionally `mask` during uninstall to defeat the `Restart=on-failure` respawn race.
3. **Config edits serialized** with `fcntl.flock` on a lockfile around every check+append/remove of `bindings.lua` / `hyprland.lua`; ALL rewrites (including uninstall line-removal) go through temp + `os.replace` preserving mode/ownership.
4. **Unit-file collision guard:** if `~/.config/systemd/user/foot-server@.service` exists WITHOUT the plugin marker, back it up (`*.pre-dropdown-terminal.bak`) and warn — never silently clobber.
5. **Bind idempotency keyed on the plugin's own marker comment**, not a loose "SUPER + U" substring; if another bind already claims SUPER+U, warn loudly instead of silently skipping.
6. **`status()` JSON parsing wrapped in try/except** with a safe fallback; review checklist item: no window-class/title-derived string ever rendered as RichText/StyledText in QML.

- Spawns: argv-array subprocess only; no `shell=True`; bash CLI uses `set -euo pipefail`, no eval.
- File I/O: fixed constant paths under `~/.config/hypr` and `~/.config/systemd/user`; no user-controlled paths; atomic writes everywhere.
- No network, no credentials, no env modification, no QML dynamic-content risk (PlainText only, no external data rendered).
