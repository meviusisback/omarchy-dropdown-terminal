# Drop-down terminal (meviusisback.dropdown-terminal)

A Quake-style drop-down terminal for Omarchy: press **SUPER + U** and a
terminal slides down from the top of your screen, full width. Press again and
it slides back up. The session is **persistent** - shell, scrollback and
running programs survive every hide/show, workspace switch, and even a shell
restart.

![preview](preview.png)

## Features

- **Persistent session** - powered by a `foot` terminal *server* (systemd user
  unit). Hiding the dropdown never kills your session; only logout or an
  explicit kill does. If the server ever dies, the next toggle self-heals it.
- **Real drop animation** - the terminal lives on its own special workspace
  at the top edge; toggling plays Hyprland's `specialWorkspace`
  slidevert animation (easeOutQuint) plus a `slide top` window rule.
- **Quake geometry** - 100% width, 55% height, flush to the top, borderless.
  Geometry is expressed in `monitor_w/H` formulas, so it adapts to any
  monitor/resolution change without reinstalling.
- **Bar widget** - shows terminal state at a glance (accent = shown). Left
  click toggles, right click opens a small menu (show/hide, kill server).
- **IPC control** - `omarchy-shell shell toggle meviusisback.dropdown-terminal`
  plus `open`, `close`, `kill`, `status` methods.
- **Clean install/uninstall** - one marker-anchored block in your Hyprland
  config, one line, one systemd unit. Uninstall removes every trace.

## Install

```bash
cd ~/.config/omarchy/plugins/meviusisback.dropdown-terminal   # (or the repo dir)
./install.sh
```

What it does:

1. Installs a parameterized systemd user unit `foot-server@.service`
   (backing up any pre-existing file of that name) and enables the
   `dropdown-terminal` instance - a persistent `foot` server with a dedicated
   app-id (`org.omarchy.dropdown-terminal`) and its own socket, so it never
   touches your normal foot setup.
2. Writes `~/.config/hypr/dropdown-terminal.lua` (window rules) and hooks it
   into `~/.config/hypr/hyprland.lua` (same pattern as the Omarchy
   workspace-layout plugin).
3. Appends a marker-anchored keybind block to `~/.config/hypr/bindings.lua`:
   `SUPER + U` -> `omarchy-dropdown-terminal toggle`.
4. Reloads Hyprland. Press **SUPER + U**.

### Uninstall

```bash
./install.sh uninstall
```

Stops and disables the server (ordered to defeat the systemd respawn race),
removes the keybind block, the hook lines, the rules file and the unit.

## Usage

| Key / action | Effect |
|---|---|
| `SUPER + U` | Toggle the drop-down terminal |
| Bar icon click | Toggle |
| Bar icon right-click | Menu: show/hide, kill server |
| `omarchy-shell shell toggle meviusisback.dropdown-terminal` | Toggle via IPC |

CLI (`bin/omarchy-dropdown-terminal`, on PATH once enabled as a plugin):

```
toggle   show/hide the drop-down terminal (default)
open     show it if hidden
close    hide it if shown
status   print JSON state (server, window, visibility)
kill     stop the foot server (ends the session)
install  install keybind + rules + systemd unit
uninstall remove everything the plugin installed
```

## How it works

```
SUPER+U  ->  omarchy-dropdown-terminal toggle
              |-- systemctl --user start foot-server@dropdown-terminal (if dead)
              |-- footclient -> attaches to the persistent server
              '-> hyprctl dispatch togglespecialworkspace dropdown
                    (specialWorkspaceIn/Out slidevert animation)
```

The foot server keeps ONE long-lived Wayland client alive. Every dropdown
summon attaches a `footclient` to it through a dedicated socket
(`$XDG_RUNTIME_DIR/foot-dropdown-terminal.sock`), so startup is instant and
the shell session (scrollback, vim, ssh...) persists as long as the server
does. The dedicated app-id scopes the window rules to this terminal only -
your regular foot windows are untouched.

## Not supported / notes

- Only one dropdown window at a time (by design - it's a Quake dropdown).
- Wayland-only (Hyprland). X11 is not supported.
- The bar widget reflects state on a 5 s poll; toggling via keybind updates
  it on the next tick.

## License

MIT - see [LICENSE](LICENSE).
