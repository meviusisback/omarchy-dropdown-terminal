# Plan — dropdown-terminal: fix stranded-window bug + VM test harness

Date: 2026-09-06 13:21 · Branch: feat/dropdown-terminal · Status: DRAFT — awaiting Alberto's approval

## Goal (one sentence)

Make SUPER+U reliably open the dropdown on its special workspace in every toggle
sequence, using the Lua engine's own `hl.dsp.window.move` object form (the form
Omarchy's own scratchpad bind proves works), and validate it in the Omarchy test VM
so the host desktop is never touched for testing.

## Current context / findings (all verified by running read-only commands)

- Plugin is enabled and live: `~/.config/omarchy/plugins/meviusisback.dropdown-terminal`
  → symlinks to this Orca worktree. `plugin list --json` shows
  `meviusisback.dropdown-terminal: enabled`.
- Keybind, rules file, hook line, foot-server unit all installed and active; 11/11
  unit tests pass (hermetic, sandboxed HOME); `bash -n` clean.
- **BUG (live):** `status` says `visible: true`, but the dropdown foot window
  (`0x561233bb7af0`) sits on normal workspace **2** (floating, 2048x633 @ y=29),
  while `special:dropdown` is shown empty on HDMI-A-1. Sequence that strands it:
  SUPER+U (opens in special ws) → hide → switch to another workspace → SUPER+U.
  The script then toggles `special:dropdown` visible, but the window had already
  spawned on ws 2 (rule misses when the special workspace isn't visible), so the
  empty special workspace covers the screen while the terminal sits elsewhere.
- **Root cause candidate:** `pull_window_into_special()` calls
  `hyprctl dispatch 'hl.dsp.window.move({ workspace = "special:dropdown" })'` — a
  string form of the Lua-engine dispatch. This build has no string dispatcher that
  takes an expression; the call silently no-ops (matches the known `hyprctl eval`
  prints-only-ok trap). Omarchy's own working bind is
  `hl.dsp.window.move({ workspace = "special:scratchpad", follow = false })` — the
  Lua object form via a bound keybind.
- Host discipline: everything so far was read-only. Per the updated omarchy-plugin-dev
  skill, the fix's runtime validation happens in the **Omarchy test VM**, not on the
  host. (One slip during this check: I invoked the script with no args on the host,
  which toggled the dropdown once — state restored by the next read-only probe;
  noted as a process miss.)

## Architecture / proposed approach

1. **Route the window move through the Lua engine via a scratch bind, not `hyprctl`.**
   In `pull_window_into_special`, instead of the no-op `hyprctl dispatch 'hl.dsp...'`:
   - Install a throwaway bind (via `hl.dsp` string is NOT possible) — use
     `hyprctl binds`-independent mechanism: write a small Lua file and register it.
   Actually simplest reliable path: **fix the spawn condition** so the window always
   spawns into the visible special workspace:
   - `do_open` already toggles `special:dropdown` visible BEFORE spawning the client
     (new code) — so a fresh spawn should honor the rule.
   - The stranding happens when the window ALREADY exists on a normal ws (spawned
     during the buggy period or before rules loaded). For that case use the
     Omarchy-native scratchpad-style move: register a one-shot bind through the Lua
     engine and trigger it.
2. **Persisted helper bind** — in the backend `install()`, also install (marker-gated,
   idempotent) a hidden bind:
   `o.bind("SUPER ALT SHIFT + U", "Pull dropdown window to special workspace",
    hl.dsp.window.move({ workspace = "special:dropdown" }))` in `dropdown-terminal.lua`
   (rules file). The bash script then triggers it with
   `hyprctl dispatch ...` — needs a triggerable form.
   **Open question:** how to trigger a Lua bind from CLI (`hyprctl plugin`-less build).
   Options: (a) `hyprctl dispatch` with a string expression form if one exists;
   (b) skip binds entirely — check if `hyprctl dispatch movetoworkspacesilent
   special:dropdown` works on this build (plain dispatchers may still exist);
   (c) focus-first trick: `hyprctl dispatch focuswindow class:...` works?
3. **Preferred concrete fix (step 2 resolution):** probe in the **test VM**
   (throwaway disk) which CLI trigger actually moves a window:
   - `hyprctl dispatch movetoworkspacesilent special:dropdown` (plain dispatcher —
     may work even with Lua engine)
   - `hyprctl dispatch focuswindow class:org.omarchy.dropdown-terminal` (to make the
     stranded window focused — it already is, per live data)
   - Lua-object bind triggered via… unknown CLI hook.
4. **Fallback (zero-Hyprland-risk) fix:** in `do_close`/`do_toggle`, treat "window
   exists but not in special" as **open** (show the special ws AND pull), and in
   `do_open`, after showing the special ws, if the window is still on a normal ws,
   close-and-respawn the foot client (kill client, spawn again now that the special
   ws is visible → rule applies). No Hyprland dispatcher dependency at all — pure
   client respawn, uses only `footclient` + existing toggles that are PROVEN to work
   (the special-ws toggle dispatch string works, we've seen it flip `visible: true`).
5. **Selection logic fix regardless of move mechanism:** `do_toggle` must treat
   "window present but NOT in special ws" as NEEDING a close-side fix, not "open".
   Current code: `if dropdown_visible && window_in_special → close else open` — when
   the window is stranded while special ws is visible, toggle re-opens (no-op-ish),
   leaving the stranded window behind. New logic: if window exists and not in
   special → attempt pull; if pull still fails → respawn client.

## Step-by-step tasks

1. **Set up the test VM** (host package install needs Alberto's OK: `qemu-full`,
   `edk2-ovmf` via the wrapper's `omarchy-pkg-install`; ISO ~6.8GB download from
   omarchy.org; one interactive install pass in the VM window — ~5-10 min; disk at
   `~/vms/omarchy-test/disk.qcow2`).
2. **Probe in VM** (read-only Hyprland state + dispatch probes on the VM's own
   session, not the host): which of `movetoworkspacesilent special:dropdown`,
   `focuswindow`, `hl.dsp.window.move` string form moves a floating pinned window to
   a special workspace on Hyprland 0.56.2 + Omarchy Lua engine. Record results.
3. **Implement the fix** in `bin/omarchy-dropdown-terminal` (and backend only if the
   chosen mechanism needs an installed helper bind):
   - `do_toggle`/`do_open`/`do_close` treat stranded state as fixable: prefer
     respawn-client fallback if no reliable move dispatcher exists.
   - Add regression tests where hermetic (toggle-decision matrix as a pure function
     if extractable).
4. **Test in VM** (rsync worktree → VM plugin dir, enable, restart VM shell, drive
   the exact repro: open → hide → switch ws → open → assert window in special ws,
   repeated across several toggle/hide/switch sequences + monitor config).
5. VM tests pass → **commit** `fix(dropdown-terminal): ...` on
   `feat/dropdown-terminal` (worktree is already the live copy; commit does not
   touch the host session).
6. **Security review** per skill (subagent reviewers on the diff; analysis-only,
   no PoC execution on host).
7. **Docs** (README keybind section), push, recap with VM verification evidence.
   Host apply step (nothing needed here — the worktree IS the live copy; the new
   script is picked up by the keybind on next invocation; the installed rules file
   only changes if the backend's rules body changes).

## Risks / tradeoffs

- Respawning the client (fallback) loses scrollback on pull failure — acceptable:
  it only happens in the rare stranding case; normal open/close keeps the session.
- A fixed helper bind in `dropdown-terminal.lua` is global (SUPER+ALT+SHIFT+U);
  hidden from help but discoverable — low risk, argv-only, idempotent.
- VM setup cost: ~7GB download + one install pass. Reusable for every future
  plugin cycle (the skill now standardizes on it).
- The window rule uses relative monitor geometry `monitor_h*55/100` with scale 1.25
  — sizes observed live are correct (2048x633), no change planned.
- Host stability: ZERO host execution planned; all runtime probes in VM.

## Open questions for Alberto

1. Approve VM setup (package install + ~7GB ISO download + 5-10 min install pass)?
2. OK to probe the 2-3 candidate dispatch mechanisms in the VM (not host)?
3. Fallback choice if no move dispatcher works: respawn client (lose scrollback only
   in stranding case) — acceptable?
4. Should the fixed helper bind route exist at all, or keep the script pure CLI?

## Security review (plan stage)

Not yet run — will spawn the plan reviewer once the approach is approved (per skill,
before coding; VM choice already addresses the main risk: no host execution).
