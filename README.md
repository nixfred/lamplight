# Lamplight

![Lamplight](docs/banner.png)

Drive a fleet of Govee lights from Omarchy: an Omarchy bar plugin plus the
`govee-lamp` CLI that does the work. One repo, because `omarchy plugin add`
clones a repo and makes it the plugin directory, so the engine ships with the
widget rather than being a separate thing you have to go and find.

The lights **always wear the active Omarchy theme**. There is no colour picker,
by design: the only thing you choose is what they are *doing*.

![panel](docs/panel.png)

## Install

```
omarchy plugin add https://github.com/nixfred/lamplight
~/.config/omarchy/plugins/nixfred.lamplight/install.sh
```

The widget works as soon as the plugin is added, because it calls
`bin/govee-lamp` relative to itself. `install.sh` additionally puts the CLI on
your PATH, wires the theme-change hook and installs the background effect unit.

## How it fits together

![architecture](docs/architecture.png)

## Layout

- `manifest.json`, `Service.qml`, `BarWidget.qml`, `LampPanel.qml` at the root,
  because the plugin manager requires the manifest at the repo root and copies
  the whole tree into `~/.config/omarchy/plugins/<id>/`
- `bin/govee-lamp` the CLI: LAN protocol, effects, fleet, persisted mode
- `hooks/`, `systemd/` reference copies; `install.sh` generates the real ones
  pointed at your checkout


## The fleet (more than one device)

`govee-lamp` drives a **group**, not a single lamp. Every effect was written
against one device; rather than teach each effect to loop, the group exposes the
same method names and fans each call out, so ambient / workspace / gauge / music
are unchanged and extra hardware is pure configuration.

Devices live in `~/.config/govee-lamp.json` under `devices`. An old config with
a single top-level `ip` is migrated on the fly, so nothing breaks.

```
govee-lamp devices                 # list the fleet with live state
govee-lamp devices scan            # add every unit answering on the LAN
govee-lamp devices disable "Bar R" # keep it configured, stop driving it
```

### Adding hardware

1. Set the device up in the Govee Home app as normal.
2. **Turn LAN Control on for it.** Every Govee unit ships with it off and will
   silently drop every packet until you do. It answers ping the whole time.
3. `govee-lamp devices scan`
4. Rename the entry in the config if you want something friendlier.

No firewall change is needed: the ufw rule is subnet scoped
(`from <your-lan>/24 to any port 4002`), so new units are already covered.

### Two performance rules learned the hard way

**Fades are paced by the group, not per device.** Fading each device in turn
would stagger them by the full duration. Measured: a 2000ms fade across four
devices takes 2.00s, not 8s.

**`json` probes only the primary device.** Probing every unit serially costs a
full timeout per unreachable one: measured **11.3s** on a fleet of four with
three down, which overruns the bar's poll interval and stacks polls up. The
frequent path now costs 0.13s. Use `govee-lamp json --all` when you actually
want every device's state.

An unreachable device never blocks the others; the fan-out swallows its errors.


## Stereo pairing two Table Lamp 2 Pros

Stereo audio is a **Govee app feature**, not something this code does: the two
lamps link over their own 5.8GHz channel and you pair them in the app. This code
does not touch audio.

What this code gives you is both lamps showing the **same colour**, which is
automatic once they are both in the fleet.


## Mix mode: every device a different theme colour

`govee-lamp mode mix` gives each device its own colour from the current theme,
chosen so all of them look different from each other.

```
govee-lamp mix            # show the current mix
govee-lamp mix next       # advance (this is what the left pedal calls)
govee-lamp mix 3          # jump to a specific one
```

Picking colours by walking the palette in order does not work. An even stride
paired `#cddbf4` with `#d1fffe` on hackerman: deltaE 3.4, two near-identical
pale whites that sit far apart in the list but not in the eye. So each mix is
built greedily for **maximum minimum pairwise separation**, seeded from a
different palette entry, which means every mix is internally as spread as the
theme allows and each one is genuinely different from the last.

Measured worst case within any mix, before and after that change:

- hackerman 3.4 to 25.4
- tokyo-night 2.4 to 45.1
- ristretto 8.0 to 38.6
- matte-black 7.6 to 25.3
- vantablack 1.1 to 5.3, and it stays bad because the theme is literally all
  greys. No algorithm fixes a monochrome palette.

Above about 10 reads as different; above 25 is comfortable.

The mix index lives in the config and the running effect re-reads it, so
`mix next` applies live with no restart. Verified: same pid across three
advances.

Fades are per device but paced **once** for the whole group, so all four land
together. Measured 1.50s across four devices for a 1500ms fade.


## Effects

One at a time -- a pidfile enforces it, because two writers at 20 Hz looks like
a fault, not a feature. Each effect re-reads `colors.toml` when the theme
changes, so switching themes recolours a *running* effect instead of needing a
restart. The theme-set hook deliberately no-ops while an effect is running, for
the same reason.

```
govee-lamp ambient [--hold S] [--fade MS]
```
Drifts through the theme's **whole** palette, not just the accent. Tokyo Night
gives 13 colours, Gruvbox 12. Long cross-fades, interpolated in linear light so
a red-to-blue fade looks like mixing light rather than passing through mud.

```
govee-lamp workspace [--ms N]
```
One theme colour per Hyprland workspace:
`palette[(workspace - 1) % len(palette)]`, so the mapping is stable per theme
and wraps past the end. Driven off Hyprland's **event socket**, not polling, so
it turns over with the workspace.

The socket is found by globbing `$XDG_RUNTIME_DIR/hypr/*/.socket2.sock` and
taking the newest, because a `systemd --user` unit does not inherit
`HYPRLAND_INSTANCE_SIGNATURE` and stale instance directories linger. It reads
`workspacev2` (id first, since names are arbitrary once the workspace-names
plugin is in play), falls back to `workspace`, and re-queries on `focusedmon`.

```
govee-lamp gauge [--source S] [--exec CMD] [--every S]
```
The lamp as a meter: theme colour when idle, through the theme's yellow, to the
theme's red at full. Above 90% it breathes, because by then it has stopped being
ambient and started being a warning. Sources: `claude` (Omarchy's own
agent-usage cache -- note it stores **fractions**, `0.95` means 95%),
`claude-session`, `claude-weekly`, `cpu`, `gpu`, `mem`, `battery`, or `--exec`
with any command printing a number in 0..1 or 0..100.

```
govee-lamp music [--rainbow] [--gain N]
```
Reads the lamp's **own** PipeWire monitor, so it reacts to exactly what the JBL
speaker is playing rather than to the room. Bass/mid/treble split by numpy FFT;
a rolling peak keeps quiet tracks using the full range. By default it stays
inside the theme palette so the lamp still reads as part of the desk;
`--rainbow` lets it off the leash.


## Why everything is animated from this host

The H6020 renders **one colour at a time**. Per-segment writes
(`0x33 05 15 ...` via `ptReal`) are accepted and then flattened -- verified by
painting a rainbow across 12 segment masks and watching the lamp show one
colour.

What *does* work is `ptReal`, which tunnels raw 20-byte Govee BLE packets
(19 bytes zero-padded + XOR checksum, base64) over the same UDP socket.
Confirmed by setting brightness with `0x33 0x04 <pct>` and reading it back via
`devStatus`, so the whole BLE command space is reachable if a future effect
needs it.

The lamp was measured tracking **20 colour updates/sec with no drops**, which is
what makes host-side animation smooth enough to be worth doing.


## Bluetooth (the JBL speaker)

The lamp is also a Bluetooth speaker. Its BR/EDR radio only advertises while in
pairing mode, so it is invisible to a normal scan -- and it cannot be woken over
the LAN API.

> Hold **Play/Pause** on the lamp until it flashes blue and plays the pairing
> tone (or Govee Home app -> Device Settings -> Bluetooth pairing), then run
> `govee-lamp pair`.

`pair` waits for it to appear, then pairs, trusts and connects, and records
`bt_mac`. Select it as an audio output the usual way
(`pactl list short sinks | grep -i bluez`, or the Omarchy sound menu).


## Two gotchas that cost real time

**1. LAN Control ships off.** The lamp answers ping and shows on the UDM while
dropping every LAN API packet. Enable it at
Govee Home app -> Office Light -> settings (gear) -> **LAN Control**.

**2. ufw silently eats the replies.** Commands are one-way UDP to port 4003, so
they appear to work while every *reply* (which arrives on our port 4002 from a
fresh flow, so conntrack does not class it as RELATED) is dropped by
`deny (incoming)`. `discover` and `status` look dead; the light still changes.
Fixed with:

```
sudo ufw allow from <your-lan>/24 to any port 4002 proto udp comment 'Govee LAN API replies'
```

Diagnose it with `nmap -sU -p 4001,4002,4003 <lamp-ip>`: ports 4001/4003
`open|filtered` mean the lamp *is* listening and the problem is on this end.


## Cloud fallback

Set `GOVEE_API_KEY` (or `api_key` in the config) and the `device_id` is already
recorded, for control from off the LAN. Request a key in the app:
Profile -> About Us -> Apply for API Key.

## Layout rule

Everything is on one screen. No `Flickable`, no `ScrollView`, nothing hidden
behind a gesture — width is the remedy, never height.

The first cut got this wrong: a two-column mode grid beside a tall right-hand
rail holding a single slider, which left a dead column about a third of the
panel wide *while* the mode descriptions elided mid-word. The grid now runs the
full width at four across, the live mode/brightness readout sits in what was an
empty gap in the header, and the controls are full-width rows underneath. The
per-mode controls (gauge source, event tints) are rows that collapse when they
do not apply, because a row that disappears beats a rail that is empty for six
modes out of eight.


## Notes

### Testing the LAN transport

Run `python3 -m unittest discover -s tests -v`. The tests use ephemeral UDP
ports on loopback and a temporary state directory; no Govee hardware or
third-party Python packages are required. They cover replies from the wrong
lamp, interleaved scan/status responses, malformed envelopes, and discovery
of multiple devices, plus hostname handling: the host is resolved once per
status probe, the request goes to that address, and replies are accepted only
from it. A host that does not resolve reads as unreachable: `govee-lamp status`
and `govee-lamp devices` mark it UNRESOLVED with the resolver's error, and
`govee-lamp json` adds an `error` field to that device.

### Plugin implementation

- `KeyboardPanel` is a `PanelWindow` and its content must be nested inside it;
  a bare `ColumnLayout` under `Panel` renders into the bar itself.
- Do not name the panel file `Panel.qml`: it inherits `Panel`, and the type
  resolves to the file, not the base.
- `fittedContentWidth`/`fittedContentHeight` are `KeyboardPanel` methods, not
  `Panel` ones.
- `WidgetButton` has no `contentItem`; children go in directly alongside
  `hasVisualContent: true`.
- Font tokens are `Style.font.bodySmall` / `.body` / `.subtitle` — there is no
  `Style.font.size.*`.
