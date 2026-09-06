# Fix: dropdown terminal escapes to a normal workspace and cannot be hidden

Date: 2026-09-06
Branch: feat/dropdown-terminal
Status: emergency fix (user stuck in the dropdown on the live host; already freed via manual unpin + move)

## Goal

Stop the drop-down terminal from stranding on a normal workspace as a pinned
always-on-top window that ignores the SUPER+U toggle (the bug that trapped the
user today), and make the toggle self-healing for any window that is stranded.

## Root cause (confirmed live)

The generated window rule sets `pin = true` (+ `stay_focused = true`). When a
fresh foot window spawns while `special:dropdown` is hidden, the `workspace =
special:dropdown` assignment does not take effect and the window lands on the
current normal workspace. `pin` then makes it render on every workspace and
ignore special-workspace visibility; `stay_focused` keeps keyboard focus glued
to it. SUPER+U only toggles the (empty) special workspace, so the window can
never be hidden.

Live-proven dispatcher facts on this engine (Hyprland 0.56.2 Lua fork):
- `hl.dsp.workspace.toggle_special("dropdown")` toggles the special workspace
- `hl.dsp.window.move({ workspace = "special:dropdown" })` moves the ACTIVE window
- `hl.dsp.window.pin("<address>")` toggles pin (worked on the active window)
- dispatchers act on the active window; move() has no address/window key

## Changes

1. backend/dropdown_terminal.py — RULES_BODY: remove `pin = true` and
   `stay_focused = true`; keep float/size/move/animation/border; update the
   header comment with the why. A window on its own special workspace does not
   need pin to stay out of the tiling flow, and pin is exactly what turns a
   mis-routed spawn into an unkillable overlay.
2. bin/omarchy-dropdown-terminal — harden the toggle:
   - new `active_is_dropdown`, `dropdown_pinned`, `unpin_if_pinned` helpers
   - `pull_window_into_special` = guarded (only when the dropdown is the active
     window, which fresh spawns always are), unpin-if-pinned, then focused
     move, then verify with retries
   - do_open: make the special workspace visible BEFORE spawning a client so
     the workspace rule has a target; pull after spawn if still stranded
   - do_close: pull a stranded window in before toggling the workspace away
3. tests/test_backend.py — regression assertions: RULES_BODY must not contain
   `pin = true` or `stay_focused = true`; must keep float/workspace/size.
4. README.md — drop the "pinned" wording (float keeps it out of the tiling flow).

## Verification

- Host-static only: py_compile, bash -n, hermetic unit tests (sandboxed HOME,
  DDT_TEST=1).
- Runtime/VM: Omarchy test VM — BUT no VM exists on this machine (~/vms absent,
  no disk image, no omarchy-iso-boot). VM bring-up needs the one-time setup
  (ISO download + interactive install in a QEMU window). Pending user go-ahead.
- Host note: ~/.config/omarchy/plugins/meviusisback.dropdown-terminal is a
  symlink to this repo, and the SUPER+U keybind calls the symlinked
  ~/.local/bin/omarchy-dropdown-terminal → the script half of this fix is live
  the moment it is written. The generated rule file (~/.config/hypr/...) only
  changes on reinstall (host state change → explicit user approval only).

## Risks / open questions

- Live host still runs the OLD generated rule (pin=true) until reinstall. The
  hardened script mitigates (show-first spawn, guarded pull, unpin-if-pinned)
  but a reinstall is the real fix. User approval required.
- focus-by-window and move-by-address are not exposed on this engine; the pull
  guard covers the observed failure modes. Any residual edge (stranded window
  that is not the active window) gets a deterministic fix during VM iteration.
