# 🪙 mm2-coin-bot

**External computer-vision coin farmer for Roblox Murder Mystery 2 — built for Linux + KDE Plasma (Wayland) + [Sober](https://sober.vinegarhq.org/).**

No injection. No executors. No memory reading. No client modification of any kind.
The bot *looks at your screen* and *presses keys / moves the mouse* — exactly like a human would, using the same desktop APIs that OBS (screen capture) and remote-desktop tools (input) use.

```text
┌──────────────────────────── KDE Plasma (Wayland) ────────────────────────────┐
│                                                                              │
│   ScreenCast portal ──► PipeWire ──► GStreamer ──► OpenCV coin detection     │
│                                                        │                     │
│   RemoteDesktop portal ◄── D-Bus ◄── bot logic ◄───────┘                     │
│         │                                                                    │
│         ▼ synthetic keyboard/mouse (compositor level)                        │
│   ┌───────────┐                                                              │
│   │   Sober   │  ◄── never touched: no process access, no files, no memory   │
│   └───────────┘                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## ⚠️ Disclaimer

- Automated farming **violates Roblox's Terms of Service**. This design avoids *technical* client-tampering detection by never touching the client, but **behavioral detection server-side is always possible**. Use a throwaway/alt account you can afford to lose.
- This project is for educational purposes (computer vision, Wayland portals, automation).
- Not affiliated with Roblox, Nikilis/MM2, or the Sober/Vinegar project.

---

## ✨ Features

- 🎯 **Target lock** — picks **one** coin, chases it until collected, then moves to the nearest neighbor (no flip-flopping between coins), with a session counter
- 🖱️ **Two movement modes** — `click` (right-click-to-move, clicks the floor next to the coin for better pathing) or `wasd` (holds `W`, strafes with `A`/`D`, walks **backward** with `S` when the coin is behind/below the character)
- 🔄 **45° search turns** — no coins for 5 s → snap-turn 45° (arrow keys, Classic camera); then rapid 1 s scan steps; after a full 360° it explores forward and rescans — all non-blocking, it keeps detecting *while* moving
- 🧱 **Stuck recovery** — frozen screen while moving → back up, jump, turn, continue
- 🎨 **Interactive color tuner** — click real coins in a (freezable) live preview to calibrate detection for your maps/monitor; saved to `~/.config/mm2bot.json`
- 🤖 **Optional player avoidance** — drop in a YOLO model you train yourself (`mm2_players.pt`) and it will skip coins near players and back off + flee from anyone too close
- 🛑 **Safe by design** — dry-run mode, startup test jump, live status output, Ctrl+C / `Q` / stop-file kill switches, all held keys released on exit

---

## 📦 Requirements

- Linux with a **Wayland** session and **xdg-desktop-portal-kde** (KDE Plasma 5.27+/6.x; GNOME works too, dialogs just look different)
- **Sober** running Roblox (or any windowed/fullscreen Roblox — the bot doesn't care what renders the game)
- Python 3.10+
- Xwayland (for the preview windows only; stock Plasma ships it)

### System packages

> `dbus-python` and `PyGObject` must come from your distro (don't pip-install them).

```bash
# Arch
sudo pacman -S python-dbus python-gobject gst-plugin-pipewire \
               gst-plugins-good python-opencv python-numpy

# Ubuntu / Debian
sudo apt install python3-dbus python3-gi gir1.2-gst-plugins-base-1.0 \
                 gstreamer1.0-pipewire gstreamer1.0-plugins-good \
                 python3-opencv python3-numpy

# Fedora
sudo dnf install python3-dbus python3-gobject pipewire-gstreamer \
                 gstreamer1-plugins-good python3-opencv python3-numpy
```

pip-installed `opencv-python`/`numpy` also work (the script auto-handles pip-OpenCV's bundled-Qt quirks on Wayland).

Optional, only for player avoidance:

```bash
pip install --user ultralytics
```

---

## 🎮 In-game settings (important!)

| Setting | Value | Why |
|---|---|---|
| Camera Mode | **Classic** | arrow keys snap the camera in exact 45° steps |
| Movement Mode | **Click to Move** | only needed for `--mode click` (uses **right**-click) |
| Shift lock | **Off** | keeps camera-relative movement predictable |
| Camera zoom | zoomed out a few notches (`O`) | more coins visible per frame |
| Window | fullscreen on the shared monitor, **focused** | synthetic keys go to the focused window |

---

## 🚀 Quick start

```bash
chmod +x mm2_coin_bot.py

# 1) Calibrate coin colors (one-time per monitor/graphics setting)
./mm2_coin_bot.py --tune

# 2) Dry run — detection overlay, sends ZERO input
./mm2_coin_bot.py --debug --no-act

# 3) Farm
./mm2_coin_bot.py                  # click-to-move mode (right-click)
./mm2_coin_bot.py --mode wasd      # or WASD mode
```

On first run, KDE shows a permission dialog — pick the monitor the game is on (and allow remote input for real runs). The approval is remembered via a portal restore token (`~/.cache/mm2bot.token`).

**Watch the startup test jump**: if your character doesn't hop, the game window wasn't focused and no input is reaching it.

### Calibrating with `--tune`

Color samples come from clicks **on the preview window** — not the game. Keys work in **both** the terminal and the preview window:

| Key | Action |
|---|---|
| `F` | freeze the frame after a 5 s countdown |
| `L` | back to live view |
| left-click | sample coin color under the cursor |
| `S` | save to `~/.config/mm2bot.json` |
| `R` | reset samples |
| `Q` | quit |

Fullscreen / single-monitor workflow:

1. Press `F` (terminal is fine) → switch to the game within 5 s with coins visible
2. Frame freezes → alt-tab back to the **mm2bot tune** window
3. Left-click 3–5 coins on the frozen image (near + far ones)
4. Good = yellow boxes on coins **only** → `S`, then `Q`

Re-run the tuner for event coins (candy, presents, snowflakes…) — it's just a color range.

---

## 🖥️ CLI reference

| Flag | Default | Description |
|---|---|---|
| `--mode {click,wasd}` | `click` | right-click-to-move vs. WASD walking |
| `--turn {auto,keys,comma,drag}` | `auto`→`keys` | 45° turn method: arrow keys / `,` / RMB camera sweep |
| `--turn-hold SEC` | `0` | hold the turn key instead of tapping (use `~0.35` if your camera rotates smoothly instead of snapping) |
| `--button {right,left}` | `right` | which button Click-to-Move uses |
| `--tune` | | interactive color calibration (sends no input) |
| `--debug` | | overlay + mask preview windows (`Q` quits) |
| `--no-act` | | dry run: capture + detect only, never send input |
| `--hz N` | `15` | main loop rate |
| `--save-frames DIR` | | save a frame every 1.5 s (build a YOLO dataset) |

---

## ⚙️ Configuration — `~/.config/mm2bot.json`

Created by the tuner; every field of the `Cfg` dataclass is a valid key. The most useful:

```json
{
  "hsv_lo": [16, 96, 118],
  "hsv_hi": [38, 255, 255],
  "loop_hz": 20,
  "no_coin_turn_sec": 5.0,
  "scan_step_sec": 0.7,
  "click_interval": 0.35,
  "turn_settle_sec": 0.25,
  "back_y_frac": 0.74,
  "lock_miss_frames": 4,
  "roi_top": 0.13
}
```

| Key | Meaning |
|---|---|
| `hsv_lo` / `hsv_hi` | coin color range (set by `--tune`) |
| `no_coin_turn_sec` | first no-coin timeout before scanning starts (the "5 second rule") |
| `scan_step_sec` / `scan_full_circle` / `explore_sec` | scan cadence, turns per full circle, forward-walk time |
| `turn_settle_sec` | pause after each 45° turn |
| `back_y_frac` | coin below this screen fraction → walk backward (raise to `0.80` if it backs up too eagerly) |
| `lock_match_frac` / `lock_miss_frames` | target-lock re-matching radius / frames before a lock is dropped (raise to `6` if locks drop behind props) |
| `roi_top` / `roi_bottom` / `roi_side` | screen margins ignored by detection (UI zones) |
| `min_area_frac` / `max_area_frac` | blob size limits (fraction of screen area) |
| `player_danger_frac` / `player_coin_margin` | YOLO evade distance / coin-near-player exclusion |

---

## 🤖 Optional: player avoidance (YOLO)

Generic models don't know Roblox avatars — you train a tiny one on your own screenshots:

```bash
# 1) collect frames while playing normally (~300-500 across maps)
./mm2_coin_bot.py --no-act --save-frames ~/mm2_dataset

# 2) label class "player" (Roboflow / CVAT / labelImg), export YOLO format

# 3) train (overnight on CPU, minutes on any GPU)
pip install --user ultralytics
yolo detect train model=yolov8n.pt data=~/mm2_dataset/data.yaml imgsz=640 epochs=60

# 4) install next to the script
cp runs/detect/train/weights/best.pt mm2_players.pt
```

With the model present: coins overlapping player boxes are ignored, and a player filling >3.5% of the screen triggers back-off + ~90° evade.

---

## 🛑 Stopping

| Method | Effect |
|---|---|
| `Ctrl+C` | clean stop (releases all held keys) |
| `Ctrl+C` ×2 | force kill |
| `Q` / `Esc` in a preview window | clean stop |
| `touch /tmp/mm2bot.stop` | clean stop from any terminal/script |

---

## 🔧 Troubleshooting

| Symptom | Fix |
|---|---|
| No KDE permission dialog / `request timed out` | `xdg-desktop-portal` + `xdg-desktop-portal-kde` installed and running? |
| `no frames yet` warnings | you picked the wrong monitor in the dialog, or cancelled sharing — rerun |
| Test jump doesn't happen | game window not focused; click it during the 4 s countdown |
| Turns don't snap 45° | Camera Mode must be **Classic**; or add `--turn-hold 0.35` |
| Arrow keys do nothing in Sober | use `--turn comma` or `--turn drag` |
| Detects UI / lamps as coins | re-run `--tune` with tighter samples; raise `roi_top`; raise `min_area_frac` |
| Coins not detected | run `--tune` — the default HSV range is a guess; map lighting varies a lot |
| `Could not find the Qt platform plugin "wayland"` | handled automatically (xcb/Xwayland is forced for preview windows); ensure Xwayland exists, or use distro `python-opencv` with `MM2BOT_KEEP_QT=1` |
| `QFontDatabase` warnings | cosmetic (pip-OpenCV's bundled Qt); a system font dir is auto-detected when possible |
| Walks away from close coins | raise `back_y_frac` in the config |

---

## 🚧 Limitations

- It farms coins; it does **not** "play MM2" — it can't identify the murderer, grab the gun, or dodge a knife (train the YOLO model if you want basic keep-away behavior).
- Color-based detection needs one `--tune` pass per monitor/graphics setup, and may need re-tuning for unusually lit maps or event coin skins.
- Between rounds / in the lobby it will scan and wander until coins exist again.
- Wayland-only by design (X11 users: plenty of simpler tools exist there).

---

<details>
<summary><b>📜 Version history</b></summary>

- **v5** — target lock (one coin at a time + neighbor bias), collected counter, backward walking (`S`), faster loop (15 Hz) / clicks / turns, rapid-scan search state machine, non-blocking exploration, floor-aimed clicks, stuck recovery backs up first
- **v4** — tuner keys from the terminal, freeze-frame workflow (`F` countdown), topmost preview with placeholder, case-insensitive keys, font-dir autodetect
- **v3** — removed background GLib thread (Qt/GLib main-context conflict → freezes), forced Qt xcb for pip-OpenCV on Wayland, robust Ctrl+C shutdown
- **v2** — arrow-key camera turns (the period key opens the MM2 emote wheel — never pressed), WASD strafing, `--tune` mode + JSON config, startup test jump, status line, 2-frame confirmation filter
- **v1** — initial: portal capture + input, HSV coin detection, 5 s / 45° search rule, stuck detection, optional YOLO avoidance

</details>

---

## 🙏 Credits

- [Spencer-Macro-Utilities](https://github.com/Spencer0187/Spencer-Macro-Utilities) — inspiration for the "external macro, zero client tampering" philosophy
- [Sober](https://sober.vinegarhq.org/) by the VinegarHQ team — Roblox on Linux
- freedesktop **ScreenCast / RemoteDesktop** portals, PipeWire, GStreamer, OpenCV

## 📄 License

MIT — see [LICENSE](LICENSE). Use at your own risk; you are responsible for what happens to your Roblox account.
