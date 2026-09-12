# Drop-down terminal (meviusisback.dropdown-terminal)

A drop-down terminal for Omarchy: press **SUPER + U** and a terminal slides
down from the top of your screen, centered and slightly narrower than the
display. Press again and it slides back up. The session is **persistent** -
shell, scrollback and running programs survive every hide/show, workspace
switch, and even a shell restart.

![preview](preview.png)

## Features

- **Persistent session** - powered by a `foot` terminal *server* (systemd user
  unit). Hiding the dropdown never kills your session; only logout or an
  explicit kill does. If the server ever dies, the next toggle self-heals it.
- **Real drop animation** - the terminal lives on its own special workspace
  at the top edge; toggling plays Hyprland's `specialWorkspace`
  slidevert animation (easeOutQuint) plus a `slide top` window rule.
- **Centered geometry** - 80% width, 45% height, just below the top bar, with
  a rounded 3px border. Geometry is expressed in `monitor_w/H` formulas, so it
  adapts to any monitor/resolution change without reinstalling.
- **Click outside to dismiss** - a focus watcher closes the dropdown the moment
  it loses focus. Dismissal is click-based: the plugin sets
  `input:follow_mouse = 0` and `float_switch_override_focus = 0` so moving the
  mouse never steals focus, and `input:special_fallthrough` lets your click
  reach the window underneath.
- **Bar widget** - shows terminal state at a glance (accent = shown). Left
  click toggles, right click opens a small menu (show/hide, kill server).
- **IPC control** - `omarchy-shell shell toggle meviusisback.dropdown-terminal`
  plus `open`, `close`, `kill`, `status` methods.
- **Clean install/uninstall** - one marker-anchored block in your Hyprland
  config, one line, one systemd unit. Uninstall removes every trace.

## Requirements

- **Omarchy** with its Hyprland build - toggling uses the Lua dispatch engine
  (`hl.dsp.workspace.toggle_special`), not the legacy
  `dispatch togglespecialworkspace` syntax.
- **foot** - provides `foot` and `footclient` (Arch: `pacman -S foot`). The
  persistent session is a foot *server*; your own foot config and windows stay
  untouched, because the dropdown uses its own app-id and socket.
- **systemd user instance** - the session lives in the user unit
  `foot-server@dropdown-terminal.service`.
- **python3** - standard library only. The install/uninstall backend and the
  focus watcher use no third-party packages.
- **omarchy-shell** - for the bar widget and the
  `omarchy-shell shell toggle meviusisback.dropdown-terminal` IPC method.

Everything runs in your own user session: no network access, no root, no
long-lived daemon beyond the foot server unit.

## Install

```bash
omarchy plugin add https://github.com/meviusisback/omarchy-dropdown-terminal
omarchy plugin enable meviusisback.dropdown-terminal   # adds the bar widget
cd ~/.config/omarchy/plugins/meviusisback.dropdown-terminal
./install.sh                                           # keybind + rules + foot server
```

`omarchy plugin add` only puts the files on disk; `./install.sh` is the part a
plugin cannot do for itself (writing your Hyprland config and enabling a user
unit).

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
./install.sh uninstall                               # keybind, rules, unit, server
omarchy plugin remove meviusisback.dropdown-terminal # plugin files + bar entry
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
- Dismissal is **click-based**: clicking another window closes the dropdown and
  focuses that window. Clicking *bare desktop background* (no window under the
  cursor) raises no Hyprland event, and clicking the bar does not move keyboard
  focus, so neither closes it - use `SUPER + U` for those.
- The plugin sets three global input options in
  `~/.config/hypr/dropdown-terminal.lua`: `special_fallthrough = true`,
  `follow_mouse = 0` and `float_switch_override_focus = 0`. The last two turn
  off hover-to-focus **for the whole desktop** (focus changes on click only) -
  that is what makes dismissal click-based. Uninstall removes them. To keep
  hover-to-focus, delete that `hl.config` block and set
  `special_fallthrough = true` yourself; dismissal then reverts to
  closing as soon as the mouse leaves the dropdown.

## License

MIT - see [LICENSE](LICENSE).
