#!/usr/bin/env python3
"""
mm2_coin_bot.py  (v5) — external computer-vision coin collector for Roblox
Murder Mystery 2, built for:  Linux + KDE Plasma (Wayland) + Sober.

Philosophy (same as Spencer-Macro-Utilities, but Wayland-native):
  * NOTHING touches the Roblox/Sober process, memory, or files.
  * Screen frames come from the freedesktop ScreenCast portal (PipeWire) —
    the same mechanism OBS uses to record your screen.
  * Mouse/keyboard events are injected through the RemoteDesktop portal —
    the same mechanism KRfb / RustDesk use. To the game it looks like a
    normal user typing. No /dev/uinput permissions needed, no root.

v5 changes (speed + targeting + movement):
  * TARGET LOCK: the bot now picks ONE coin and stays locked on it until
    it is collected or lost, then moves to the next one (biased toward
    the nearest neighbor). No more flip-flopping between two coins.
  * Collected-coin counter with feedback:  [+] coin collected! (total N)
  * BACKWARD movement: if the locked coin is below/behind the character
    on screen, it walks backwards (S) toward it. Stuck recovery now backs
    up + jumps + turns (instead of jumping into the wall), and evading
    players also backs off first.
  * FASTER: 15 Hz loop (was 10), re-clicks every 0.5 s (was 0.85), turn
    settle 0.35 s (was 0.8), tighter drag sweeps.
  * SMARTER SEARCH: first no-coin timeout is still 5 s (your rule), but
    after that it scans in rapid 45° steps (1 s each). After a full 360°
    with nothing found it explores forward, then rescans. Exploration is
    non-blocking now — it keeps detecting coins WHILE walking and locks
    on instantly if any appear.
  * Click mode clicks the floor just below the coin (better click-to-move
    pathing than clicking the coin sprite itself).
  * All timings/behavior are tunable via ~/.config/mm2bot.json (any Cfg
    field name works as a key, e.g. {"loop_hz": 20, "scan_step_sec": 0.8}).

v4: tuner keys work from the terminal too; freeze workflow (F -> 5 s
  countdown -> click coins on the frozen preview); topmost preview with
  placeholder; case-insensitive keys; font-dir autodetect.
v3: no background GLib thread (Qt/GLib main-context fight -> freezes);
  forced Qt xcb on Wayland for pip-opencv; robust Ctrl+C shutdown.
v2: arrow-key camera turns (period key = MM2 emote wheel, never pressed);
  WASD strafing; --tune mode with saved config; test jump; status line;
  2-frame confirmation filter.

------------------------------------------------------------------------
DEPENDENCIES (system packages — do NOT pip-install gi/dbus):
  Arch:    sudo pacman -S python-dbus python-gobject gst-plugin-pipewire \
                          gst-plugins-good python-opencv python-numpy
  Ubuntu:  sudo apt install python3-dbus python3-gi gir1.2-gst-plugins-base-1.0 \
                          gstreamer1.0-pipewire gstreamer1.0-plugins-good \
                          python3-opencv python3-numpy
  Fedora:  sudo dnf install python3-dbus python3-gobject pipewire-gstreamer \
                          gstreamer1-plugins-good python3-opencv python3-numpy
  (pip-installed opencv-python + numpy also work — Qt quirks are handled.)
  Optional (player avoidance): pip install --user ultralytics

ROBLOX / SOBER SETTINGS BEFORE RUNNING:
  * Roblox Settings -> Camera Mode: "Classic"  (arrow keys snap 45°)
  * For --mode click: Movement Mode "Click to Move" (right-click by default)
  * Shift lock OFF, 3rd person, zoom out a bit (press O a few times)
  * Sober fullscreen on the monitor you share, and KEEP IT FOCUSED —
    keyboard input goes to whatever window has focus!

RECOMMENDED WORKFLOW:
  1) ./mm2_coin_bot.py --tune
       press F (in terminal or preview) -> switch to the game within 5 s
       -> frame freezes -> alt-tab back -> LEFT-CLICK 3-5 coins on the
       frozen preview -> S to save -> Q to quit
  2) ./mm2_coin_bot.py --debug --no-act   # watch detection, sends no input
  3) ./mm2_coin_bot.py --mode wasd        # real run (or default click mode)

TURN CALIBRATION:
  Watch the first search turn. If it only nudges instead of snapping 45°,
  set Camera Mode to Classic, or use e.g.  --turn-hold 0.35  to hold the
  arrow key longer. If arrows do nothing in Sober: --turn comma or
  --turn drag. If turns feel unsettled, raise "turn_settle_sec" in
  ~/.config/mm2bot.json (0.35 default).

STOPPING IT:
  Ctrl+C once = clean stop, twice = force kill.
  Q in the terminal or a preview window also stops the tuner/debug view.
  From anywhere else:  touch /tmp/mm2bot.stop
------------------------------------------------------------------------
"""

import argparse
import json
import math
import os
import random
import select
import signal
import sys
import termios
import time
import tty
from collections import deque
from dataclasses import dataclass

# --- Qt/OpenCV environment fixes — MUST happen before importing cv2 -------
# pip's opencv bundles a minimal Qt with no "wayland" platform plugin, so on
# Wayland sessions we force the xcb backend (runs via Xwayland). Opt out with
# MM2BOT_KEEP_QT=1 if your cv2 uses a system Qt that has proper Wayland support.
if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("MM2BOT_KEEP_QT"):
    os.environ["QT_QPA_PLATFORM"] = "xcb"


def _find_font_dir():
    import glob as _g
    for d in ("/usr/share/fonts/TTF", "/usr/share/fonts/truetype/dejavu",
              "/usr/share/fonts/dejavu", "/usr/share/fonts/noto",
              "/usr/share/fonts/gnu-free", "/usr/share/fonts/liberation",
              "/usr/share/fonts/truetype", "/usr/share/fonts/opentype"):
        if os.path.isdir(d) and (_g.glob(os.path.join(d, "*.ttf"))
                                 or _g.glob(os.path.join(d, "*.otf"))):
            return d
    return None


if "QT_QPA_FONTDIR" not in os.environ:
    _fontdir = _find_font_dir()
    if _fontdir:
        os.environ["QT_QPA_FONTDIR"] = _fontdir
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import numpy as np
import cv2

try:
    import dbus
    import dbus.mainloop.glib
    from dbus.mainloop.glib import DBusGMainLoop
except ImportError:
    sys.exit("[x] python dbus bindings missing (install python3-dbus / python-dbus)")

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
except (ImportError, ValueError):
    sys.exit("[x] PyGObject / GStreamer missing (install python3-gi / python-gobject "
             "+ gstreamer 'pipewire' and 'good' plugin packages)")


# ===================== global stop flag / signals =====================

STOP = False
_sig_count = 0


def request_stop(reason=""):
    global STOP
    if not STOP:
        STOP = True
        if reason:
            print(f"\n[i] stopping: {reason}")


def _on_signal(signum, frame):
    global _sig_count
    _sig_count += 1
    if _sig_count >= 2:
        print("\n[!] force exit")
        os._exit(1)
    request_stop("Ctrl+C / signal (press Ctrl+C again to force-kill)")


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)


class TerminalKeys:
    """Non-blocking single-key reads from the terminal, so tuner keys work
    when the TERMINAL is focused too (cv2 windows only get keys while the
    window itself is focused). Ctrl+C still works (cbreak keeps ISIG)."""

    def __init__(self):
        self.fd = None
        self.saved = None

    def __enter__(self):
        try:
            if sys.stdin.isatty():
                self.fd = sys.stdin.fileno()
                self.saved = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
        except Exception:
            self.fd = None
        return self

    def poll(self):
        keys = []
        if self.fd is None:
            return keys
        try:
            while select.select([sys.stdin], [], [], 0)[0]:
                ch = os.read(self.fd, 1).decode(errors="ignore")
                if not ch:
                    break
                keys.append(ch.lower())
        except Exception:
            pass
        return keys

    def __exit__(self, *exc):
        if self.fd is not None and self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except Exception:
                pass


# ========================== configuration ==========================

@dataclass
class Cfg:
    mode: str = "click"            # "click" = right-click-to-move, "wasd" = hold W
    turn_method: str = "keys"      # "keys" = arrow keys | "comma" = , | "drag" = RMB
    turn_hold: float = 0.0         # hold turn key this long (0 = quick tap);
                                   # use ~0.35 if your camera rotates smoothly
    click_button: str = "right"    # button used by Roblox "Click to Move"
    loop_hz: float = 15.0          # main loop rate
    no_coin_turn_sec: float = 5.0  # FIRST no-coin timeout -> start scanning
    scan_step_sec: float = 1.0     # wait between rapid 45° scan steps
    scan_full_circle: int = 8      # scan steps before walking somewhere new
    explore_sec: float = 2.2       # forward walk time when a scan found nothing
    min_coins: int = 1             # fewer than this counts as "too few coins"
    confirm_frames: int = 2        # coins must persist this many frames
    click_interval: float = 0.5    # re-click the locked coin this often (s)
    turn_settle_sec: float = 0.35  # wait after a 45° turn before next decision
    pixels_per_45deg: int = 320    # drag-turn calibration (only for --turn drag)
    strafe_deadzone: float = 0.10  # wasd: no strafe when coin is this centered
    nudge_dx: float = 0.55         # wasd: arrow-tap camera if coin past this
    nudge_cooldown: float = 0.8
    back_y_frac: float = 0.74      # coin below this screen fraction -> walk BACKWARD
    lock_match_frac: float = 0.14  # re-match lock to nearest coin within this * width
    lock_miss_frames: int = 4      # lock lost after this many missed frames
    status_every: float = 2.0      # terminal status line interval (s)
    freeze_delay: float = 5.0      # --tune: countdown before freezing frame

    # --- gold coin HSV range (OpenCV hue 0..179). USE --tune, don't guess! ---
    hsv_lo: tuple = (18, 110, 140)
    hsv_hi: tuple = (40, 255, 255)

    min_area_frac: float = 2.2e-5  # min blob size as fraction of frame pixels
    max_area_frac: float = 6e-3    # bigger = probably UI / light, not a coin
    roi_top: float = 0.13          # ignore top 13% (timer + coin counter UI)
    roi_bottom: float = 0.10       # ignore bottom 10% (hotbar/emotes)
    roi_side: float = 0.03         # ignore 3% left/right edges

    # --- stuck detection ---
    stuck_sec: float = 3.0         # moving this long with frozen screen = stuck
    stuck_diff: float = 1.1        # mean gray-diff threshold

    # --- optional player avoidance (YOLO) ---
    yolo_model: str = "mm2_players.pt"
    yolo_every: int = 3
    yolo_conf: float = 0.45
    player_danger_frac: float = 0.035
    player_coin_margin: float = 1.4
    evade_cooldown: float = 4.0

    stop_file: str = "/tmp/mm2bot.stop"
    restore_token_file: str = os.path.expanduser("~/.cache/mm2bot.token")
    config_file: str = os.path.expanduser("~/.config/mm2bot.json")


def load_config(cfg: Cfg):
    """Overlay ~/.config/mm2bot.json onto the defaults. Any Cfg field is
    accepted, e.g. {"hsv_lo":[15,90,120], "loop_hz":20, "roi_top":0.18}."""
    try:
        with open(cfg.config_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] could not read {cfg.config_file}: {e}")
        return
    for k, v in data.items():
        if hasattr(cfg, k):
            setattr(cfg, k, tuple(v) if isinstance(v, list) else v)
    print(f"[i] loaded config: {cfg.config_file}  "
          f"(hsv {list(cfg.hsv_lo)} .. {list(cfg.hsv_hi)})")


def save_config(cfg: Cfg):
    """Write the tuned HSV range, keeping any other keys the user added."""
    data = {}
    try:
        with open(cfg.config_file) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    data["hsv_lo"] = [int(x) for x in cfg.hsv_lo]
    data["hsv_hi"] = [int(x) for x in cfg.hsv_hi]
    os.makedirs(os.path.dirname(cfg.config_file), exist_ok=True)
    with open(cfg.config_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[i] saved {cfg.config_file}  "
          f"(hsv {data['hsv_lo']} .. {data['hsv_hi']})")


# Linux evdev keycodes (what the RemoteDesktop portal expects).
# NOTE: no period/dot key here on purpose — '.' opens the MM2 emote wheel!
KEY = dict(w=17, a=30, s=31, d=32, space=57, comma=51, left=105, right=106)
BTN_LEFT, BTN_RIGHT = 0x110, 0x111


# ====================== portal (capture + input) ======================

class Portal:
    """One xdg-desktop-portal session providing BOTH the PipeWire screen
    stream and compositor-level input injection (KDE implements both).

    No background thread: during setup we pump the default GLib main
    context inline to receive portal Response signals. After setup, input
    injection is plain blocking D-Bus calls — no event loop needed, so
    cv2/Qt gets the main context all to itself."""

    BUS = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"

    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()
        obj = self.bus.get_object(self.BUS, self.PATH)
        self.sc = dbus.Interface(obj, "org.freedesktop.portal.ScreenCast")
        self.rd = dbus.Interface(obj, "org.freedesktop.portal.RemoteDesktop")
        self._ctx = GLib.MainContext.default()
        self._tok = 0
        self._nop = dbus.Dictionary(signature="sv")
        self.session = None
        self.node_id = None
        self.pw_fd = -1
        self.act = True                    # False = --no-act (never send input)
        self._held_keys = set()
        self._held_btns = set()

    # ---- low-level request/response dance ----
    def _sender(self):
        return self.bus.get_unique_name()[1:].replace(".", "_")

    def _call(self, method, *args, options=None, timeout=300.0):
        """Invoke a portal method and wait for its Request's Response signal,
        pumping the GLib default context in THIS thread (no helper thread)."""
        self._tok += 1
        token = f"mm2bot{os.getpid()}_{self._tok}"
        req_path = f"/org/freedesktop/portal/desktop/request/{self._sender()}/{token}"
        got = {}

        def on_resp(code, results):
            got["code"], got["results"] = int(code), results

        match = self.bus.add_signal_receiver(
            on_resp, "Response", "org.freedesktop.portal.Request",
            self.BUS, req_path)
        opts = dbus.Dictionary(signature="sv")
        for k, v in (options or {}).items():
            opts[k] = v
        opts["handle_token"] = token
        method(*args, opts)

        # keepalive source so ctx.iteration(True) wakes up regularly
        keepalive = GLib.timeout_add(200, lambda: True)
        deadline = time.time() + timeout
        try:
            while "code" not in got:
                if STOP:
                    raise RuntimeError("aborted by user during portal setup")
                if time.time() > deadline:
                    raise TimeoutError(
                        "portal request timed out (permission dialog ignored?)")
                self._ctx.iteration(True)
        finally:
            try:
                GLib.source_remove(keepalive)
            except Exception:
                pass
            match.remove()
        if got["code"] != 0:
            raise RuntimeError(f"portal request cancelled/denied (code {got['code']})")
        return got["results"]

    # ---- session setup ----
    def open(self, want_input=True):
        iface = self.rd if want_input else self.sc
        r = self._call(iface.CreateSession,
                       options={"session_handle_token": f"mm2bot{os.getpid()}"})
        self.session = dbus.ObjectPath(str(r["session_handle"]))

        if want_input:
            opts = {"types": dbus.UInt32(1 | 2),          # keyboard | pointer
                    "persist_mode": dbus.UInt32(2)}       # remember approval
            try:
                tok = open(self.cfg.restore_token_file).read().strip()
                if tok:
                    opts["restore_token"] = tok
            except OSError:
                pass
            self._call(self.rd.SelectDevices, self.session, options=opts)

        self._call(self.sc.SelectSources, self.session,
                   options={"types": dbus.UInt32(1),      # 1 = full monitor
                            "multiple": False,
                            "cursor_mode": dbus.UInt32(2)})  # cursor in frames

        r = self._call(iface.Start, self.session, "", options={})
        if "restore_token" in r:
            try:
                os.makedirs(os.path.dirname(self.cfg.restore_token_file),
                            exist_ok=True)
                with open(self.cfg.restore_token_file, "w") as f:
                    f.write(str(r["restore_token"]))
            except OSError:
                pass
        streams = r.get("streams") or []
        if not streams:
            raise RuntimeError("no screencast stream returned — did you pick a screen?")
        self.node_id = int(streams[0][0])

        fd = self.sc.OpenPipeWireRemote(self.session, self._nop)
        self.pw_fd = fd.take()
        return self.pw_fd, self.node_id

    # ---- input injection (plain blocking calls, no event loop needed) ----
    def key(self, code, down):
        if not self.act:
            return
        self.rd.NotifyKeyboardKeycode(self.session, self._nop,
                                      dbus.Int32(code), dbus.UInt32(1 if down else 0))
        (self._held_keys.add if down else self._held_keys.discard)(code)

    def tap(self, code, hold=0.05):
        self.key(code, True)
        time.sleep(max(0.03, hold))
        self.key(code, False)

    def button(self, btn, down):
        if not self.act:
            return
        self.rd.NotifyPointerButton(self.session, self._nop,
                                    dbus.Int32(btn), dbus.UInt32(1 if down else 0))
        (self._held_btns.add if down else self._held_btns.discard)(btn)

    def move_abs(self, x, y):
        if not self.act:
            return
        self.rd.NotifyPointerMotionAbsolute(self.session, self._nop,
                                            dbus.UInt32(self.node_id),
                                            dbus.Double(float(x)),
                                            dbus.Double(float(y)))

    def move_rel(self, dx, dy):
        if not self.act:
            return
        self.rd.NotifyPointerMotion(self.session, self._nop,
                                    dbus.Double(float(dx)), dbus.Double(float(dy)))

    def click(self, x, y, btn=BTN_RIGHT):
        # tiny humanizing jitter so clicks aren't pixel-identical
        self.move_abs(x + random.uniform(-3, 3), y + random.uniform(-3, 3))
        time.sleep(0.04)
        self.button(btn, True)
        time.sleep(0.05 + random.uniform(0, 0.03))
        self.button(btn, False)

    def release_all(self):
        for k in list(self._held_keys):
            try:
                self.key(k, False)
            except Exception:
                pass
        for b in list(self._held_btns):
            try:
                self.button(b, False)
            except Exception:
                pass

    def close(self):
        self.release_all()
        try:
            if self.session:
                dbus.Interface(self.bus.get_object(self.BUS, str(self.session)),
                               "org.freedesktop.portal.Session").Close()
        except Exception:
            pass


# ========================= screen grabber =========================

class Grabber:
    def __init__(self, fd, node_id):
        Gst.init(None)
        desc = (f"pipewiresrc fd={fd} path={node_id} do-timestamp=true ! "
                "videoconvert ! video/x-raw,format=BGRx ! "
                "appsink name=sink sync=false drop=true max-buffers=1")
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            sys.exit(f"[x] GStreamer pipeline failed ({e}). Is the PipeWire GStreamer "
                     "plugin installed? (gst-plugin-pipewire / gstreamer1.0-pipewire)")
        self.sink = self.pipeline.get_by_name("sink")
        self.pipeline.set_state(Gst.State.PLAYING)

    def frame(self, timeout_s=2.0):
        sample = self.sink.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
        if sample is None:
            return None
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            data = np.frombuffer(mi.data, dtype=np.uint8)
            stride = data.size // h                      # handles row padding
            img = data[:h * stride].reshape(h, stride)[:, :w * 4]
            return img.reshape(h, w, 4)[:, :, :3].copy()  # BGRx -> BGR
        finally:
            buf.unmap(mi)

    def close(self):
        try:
            self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass


# ========================= vision =========================

K3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
K5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))


def detect_coins(frame, cfg: Cfg):
    """Return ([(cx, cy, area, bbox), ...], mask) for coin-looking blobs."""
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(cfg.hsv_lo, np.uint8),
                       np.array(cfg.hsv_hi, np.uint8))
    # blank out UI regions
    mask[:int(h * cfg.roi_top)] = 0
    mask[h - int(h * cfg.roi_bottom):] = 0
    mask[:, :int(w * cfg.roi_side)] = 0
    mask[:, w - int(w * cfg.roi_side):] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, K3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, K5)

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    lo, hi = cfg.min_area_frac * w * h, cfg.max_area_frac * w * h
    coins = []
    for c in cnts:
        a = cv2.contourArea(c)
        if not (lo <= a <= hi):
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        aspect = cw / max(ch, 1)
        if not (0.35 <= aspect <= 2.6):        # coins: circle -> tilted ellipse
            continue
        if a / (cw * ch) < 0.42:               # roundish blobs fill their bbox
            continue
        coins.append((x + cw / 2.0, y + ch / 2.0, a, (x, y, cw, ch)))
    return coins, mask


def pick_target(coins, w, prev=None):
    """Prefer big (=near) coins, with a bonus for clusters, and a strong
    bias toward the position of the PREVIOUS target so that after
    collecting one coin the bot goes for its neighbor next."""
    best, best_score = None, -1.0
    for i, (cx, cy, a, _) in enumerate(coins):
        s = a
        for j, (ox, oy, oa, _) in enumerate(coins):
            if i != j and math.hypot(cx - ox, cy - oy) < 0.18 * w:
                s += 0.5 * oa
        if prev is not None:
            d = math.hypot(cx - prev[0], cy - prev[1])
            s *= 1.0 + 0.9 * max(0.0, 1.0 - d / (0.45 * w))
        if s > best_score:
            best, best_score = coins[i], s
    return best


def coin_near_player(coin, players, margin):
    cx, cy = coin[0], coin[1]
    for (x1, y1, x2, y2) in players:
        mx = (x2 - x1) * (margin - 1) / 2
        my = (y2 - y1) * (margin - 1) / 2
        if x1 - mx <= cx <= x2 + mx and y1 - my <= cy <= y2 + my:
            return True
    return False


class PlayerDetector:
    """Optional. Only active if a trained YOLO model file exists."""

    def __init__(self, cfg: Cfg):
        self.cfg, self.model = cfg, None
        if os.path.exists(cfg.yolo_model):
            try:
                from ultralytics import YOLO
                self.model = YOLO(cfg.yolo_model)
                print(f"[i] player-avoidance model loaded: {cfg.yolo_model}")
            except ImportError:
                print("[!] found model file but ultralytics isn't installed "
                      "(pip install --user ultralytics) — avoidance disabled")
        else:
            print("[i] no mm2_players.pt found — player avoidance disabled")

    def detect(self, frame):
        if self.model is None:
            return []
        res = self.model.predict(frame, conf=self.cfg.yolo_conf, verbose=False)[0]
        return [tuple(map(float, b.xyxy[0].tolist())) for b in res.boxes]


# ========================= interactive tuner =========================

class Tuner:
    """--tune: sample real coin colors and save them for the bot.

    IMPORTANT: clicks are sampled from the PREVIEW WINDOW, not the game!
    Keys work in BOTH the terminal and the preview window:
      f = freeze frame after a countdown (switch to the game during it!)
      l = back to live view          s = save to ~/.config/mm2bot.json
      r = reset samples              q = quit
    """

    WIN = "mm2bot tune"
    MASK_WIN = "mask (white = detected color)"

    def __init__(self, cfg: Cfg, grab: Grabber):
        self.cfg, self.grab = cfg, grab
        self.samples = []
        self.scale = 0.5
        self.frame = None          # frame the mouse callback samples from
        self.live = None
        self.frozen = None
        self.state = "live"        # live | countdown | frozen
        self.freeze_at = 0.0

    def _set_bars(self):
        lo, hi = self.cfg.hsv_lo, self.cfg.hsv_hi
        for name, val in (("H lo", lo[0]), ("H hi", hi[0]), ("S lo", lo[1]),
                          ("S hi", hi[1]), ("V lo", lo[2]), ("V hi", hi[2])):
            cv2.setTrackbarPos(name, self.WIN, int(val))

    def _read_bars(self):
        g = lambda n: cv2.getTrackbarPos(n, self.WIN)
        self.cfg.hsv_lo = (g("H lo"), g("S lo"), g("V lo"))
        self.cfg.hsv_hi = (g("H hi"), g("S hi"), g("V hi"))

    def _recompute(self):
        hs = [s[0] for s in self.samples]
        ss = [s[1] for s in self.samples]
        vs = [s[2] for s in self.samples]
        self.cfg.hsv_lo = (max(0, min(hs) - 6),
                           max(25, min(ss) - 45),
                           max(35, min(vs) - 45))
        self.cfg.hsv_hi = (min(179, max(hs) + 6), 255, 255)
        self._set_bars()

    def _on_mouse(self, event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN or self.frame is None:
            return
        fx, fy = int(x / self.scale), int(y / self.scale)
        h, w = self.frame.shape[:2]
        if not (0 <= fx < w and 0 <= fy < h):
            return
        x0, x1 = max(0, fx - 3), min(w, fx + 4)
        y0, y1 = max(0, fy - 3), min(h, fy + 4)
        patch = cv2.cvtColor(self.frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        med = tuple(int(v) for v in np.median(patch.reshape(-1, 3), axis=0))
        self.samples.append(med)
        self._recompute()
        print(f"[tune] sample #{len(self.samples)} HSV={med}  ->  "
              f"range {list(self.cfg.hsv_lo)} .. {list(self.cfg.hsv_hi)}")

    def _handle_key(self, ch):
        """Returns False when the tuner should quit."""
        if ch in ("q", "\x1b"):
            return False
        if ch == "s":
            save_config(self.cfg)
        elif ch == "r":
            self.samples.clear()
            print("[tune] samples cleared (sliders keep their positions)")
        elif ch == "f":
            self.state = "countdown"
            self.freeze_at = time.time() + self.cfg.freeze_delay
            print(f"[tune] freezing in {self.cfg.freeze_delay:.0f} s — "
                  "switch to the game NOW so coins are on screen!")
        elif ch == "l":
            self.state = "live"
            self.frozen = None
            print("[tune] live view")
        return True

    def run(self):
        try:
            cv2.namedWindow(self.WIN)
        except cv2.error:
            sys.exit("[x] --tune needs a desktop GUI (cv2.imshow unavailable — "
                     "is Xwayland installed?)")
        prop = getattr(cv2, "WND_PROP_TOPMOST", None)
        if prop is not None:
            try:
                cv2.setWindowProperty(self.WIN, prop, 1)
            except Exception:
                pass
        nop = lambda v: None
        for name, val, mx in (("H lo", self.cfg.hsv_lo[0], 179),
                              ("H hi", self.cfg.hsv_hi[0], 179),
                              ("S lo", self.cfg.hsv_lo[1], 255),
                              ("S hi", self.cfg.hsv_hi[1], 255),
                              ("V lo", self.cfg.hsv_lo[2], 255),
                              ("V hi", self.cfg.hsv_hi[2], 255)):
            cv2.createTrackbar(name, self.WIN, int(val), mx, nop)
        cv2.setMouseCallback(self.WIN, self._on_mouse)

        print("""
[tune] ============================ HOW TO USE ============================
[tune] Color samples come from LEFT-CLICKS ON THE PREVIEW WINDOW.
[tune] Clicking inside the game itself does NOTHING here.
[tune]
[tune] Keys (work in this terminal AND in the preview window):
[tune]   f = freeze the frame after a 5 s countdown
[tune]   l = back to live view        s = save        r = reset samples
[tune]   q = quit
[tune]
[tune] Fullscreen / single-monitor workflow:
[tune]   1) press F, then switch to the game within 5 s (coins visible)
[tune]   2) when it freezes, alt-tab back to the 'mm2bot tune' window
[tune]   3) LEFT-CLICK 3-5 coins on the frozen image (near + far ones)
[tune]   4) good = yellow boxes on coins only  ->  press S, then Q
[tune] ====================================================================
""")

        got_first = False
        last_warn = 0.0
        with TerminalKeys() as tk:
            running = True
            while running and not STOP:
                now = time.time()

                if self.state != "frozen":
                    f = self.grab.frame(timeout_s=0.5)
                    if f is not None:
                        self.live = f
                        if not got_first:
                            got_first = True
                            fh, fw = f.shape[:2]
                            print(f"[tune] receiving frames {fw}x{fh}")
                    elif self.live is None and now - last_warn > 2.0:
                        print("[tune] no frames yet — wrong screen picked in the "
                              "KDE dialog? sharing cancelled?")
                        last_warn = now

                if self.state == "countdown" and now >= self.freeze_at:
                    if self.live is not None:
                        self.frozen = self.live.copy()
                        self.state = "frozen"
                        print("[tune] frame FROZEN — alt-tab back and click the "
                              "coins in the preview (l = live again)")
                    else:
                        self.state = "live"

                active = self.frozen if self.state == "frozen" else self.live
                self.frame = active
                self._read_bars()

                if active is None:
                    canvas = np.full((360, 640, 3), 60, np.uint8)
                    cv2.putText(canvas, "waiting for frames...", (40, 180),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
                    cv2.imshow(self.WIN, canvas)
                else:
                    coins, mask = detect_coins(active, self.cfg)
                    vis = active.copy()
                    h, w = vis.shape[:2]
                    cv2.rectangle(vis,
                                  (int(w * self.cfg.roi_side), int(h * self.cfg.roi_top)),
                                  (w - int(w * self.cfg.roi_side),
                                   h - int(h * self.cfg.roi_bottom)),
                                  (80, 80, 80), 1)
                    for (cx, cy, a, (x, y, cw, ch)) in coins:
                        cv2.rectangle(vis, (x, y), (x + cw, y + ch), (0, 255, 255), 2)
                    if self.state == "countdown":
                        st = f"FREEZING IN {max(0.0, self.freeze_at - now):.1f}s - GO TO THE GAME!"
                        col = (0, 165, 255)
                    elif self.state == "frozen":
                        st = "FROZEN - click coins here!  (l = live)"
                        col = (0, 0, 255)
                    else:
                        st = "LIVE  (f = freeze in 5s)"
                        col = (0, 255, 0)
                    cv2.putText(vis, st, (10, int(h * 0.09)),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.1, col, 3)
                    cv2.putText(vis, f"detected: {len(coins)}  samples: "
                                     f"{len(self.samples)}  keys: F L S R Q "
                                     "(terminal or window)",
                                (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                                (0, 255, 0), 2)
                    cv2.imshow(self.WIN, cv2.resize(vis, (int(w * self.scale),
                                                          int(h * self.scale))))
                    cv2.imshow(self.MASK_WIN, cv2.resize(mask, (w // 3, h // 3)))

                keys = tk.poll()
                k = cv2.waitKey(30) & 0xFF
                if k not in (255, 0):
                    try:
                        keys.append(chr(k).lower())
                    except ValueError:
                        pass
                for ch in keys:
                    if not self._handle_key(ch):
                        running = False
                        break
        cv2.destroyAllWindows()


# ========================= the bot =========================

class Bot:
    def __init__(self, cfg: Cfg, portal: Portal, grab: Grabber,
                 debug=False, save_dir=None):
        self.cfg, self.portal, self.grab = cfg, portal, grab
        self.debug, self.save_dir = debug, save_dir
        self.players = PlayerDetector(cfg)
        self.size = None
        self.frame_i = 0
        self.last_click = 0.0
        self.last_nudge = 0.0
        self.last_evade = 0.0
        self.last_save = 0.0
        self.last_status = 0.0
        self.coin_streak = 0
        self.dragging = False
        self.turn_dir = 1                       # keep sweeping the same way
        self.cached_players = []
        self.prev_small = None
        self.diff_hist = deque(maxlen=64)
        self.moving_since = None
        self.status = "start"
        # --- target lock ---
        self.lock = None                        # dict(cx, cy, area, bw, bh)
        self.lock_miss = 0
        self.last_lock_pos = None
        self.collected = 0
        # --- search state machine ---
        self.scan_count = 0
        self.forward_until = 0.0
        self.next_search = time.time() + cfg.no_coin_turn_sec
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    # ---- movement primitives ----
    def hold(self, code, on):
        held = code in self.portal._held_keys
        if on and not held:
            self.portal.key(code, True)
        elif not on and held:
            self.portal.key(code, False)

    def release_movement(self):
        for k in ("w", "a", "s", "d"):
            self.hold(KEY[k], False)

    def stop_drag(self):
        if self.dragging:
            self.portal.button(BTN_RIGHT, False)
            self.dragging = False

    def click_btn(self):
        return BTN_RIGHT if self.cfg.click_button == "right" else BTN_LEFT

    def turn45(self, direction):
        """Rotate the camera ~45° (fast).
        keys  -> tap/hold LEFT or RIGHT ARROW (Roblox camera rotate;
                 snaps 45° in Classic camera mode). No emote conflict.
        comma -> tap ','  (rotate left only — also emote-safe)
        drag  -> hold RMB and sweep the mouse sideways (4 fast steps)"""
        self.stop_drag()
        self.release_movement()
        hold = max(0.05, self.cfg.turn_hold)
        m = self.cfg.turn_method
        if m == "comma":
            self.portal.tap(KEY["comma"], hold=hold)
        elif m == "drag":
            w, h = self.size
            self.portal.move_abs(w * 0.5, h * 0.5)
            time.sleep(0.02)
            self.portal.button(BTN_RIGHT, True)
            steps = 4
            px = self.cfg.pixels_per_45deg * direction / steps
            for _ in range(steps):
                self.portal.move_rel(px, 0)
                time.sleep(0.012)
            self.portal.button(BTN_RIGHT, False)
        else:                                   # "keys" (default)
            code = KEY["right"] if direction > 0 else KEY["left"]
            self.portal.tap(code, hold=hold)
        time.sleep(self.cfg.turn_settle_sec)

    def forward_click(self):
        """Click-to-move: click a point ahead to walk into the new heading."""
        w, h = self.size
        self.portal.click(w * 0.5, h * 0.40, self.click_btn())

    # ---- target lock ----
    def update_lock(self, coins, w, h):
        cfg = self.cfg
        if self.lock is not None:
            # re-associate the lock with the nearest current detection
            r = cfg.lock_match_frac * w
            best, bd = None, 1e18
            for c in coins:
                d = math.hypot(c[0] - self.lock["cx"], c[1] - self.lock["cy"])
                if d < bd:
                    bd, best = d, c
            if best is not None and bd <= r:
                self.lock.update(cx=best[0], cy=best[1], area=best[2],
                                 bw=best[3][2], bh=best[3][3])
                self.lock_miss = 0
            else:
                self.lock_miss += 1
                if self.lock_miss > cfg.lock_miss_frames:
                    near_char = (abs(self.lock["cx"] - w / 2) < w * 0.22
                                 and self.lock["cy"] > h * 0.48)
                    self.last_lock_pos = (self.lock["cx"], self.lock["cy"])
                    self.lock = None
                    if near_char:
                        self.collected += 1
                        print(f"[+] coin collected! (session total: {self.collected})")
                    else:
                        print("[*] lost the target — choosing the next coin")
        if (self.lock is None and coins
                and self.coin_streak >= cfg.confirm_frames):
            c = pick_target(coins, w, prev=self.last_lock_pos)
            self.lock = dict(cx=c[0], cy=c[1], area=c[2],
                             bw=c[3][2], bh=c[3][3])
            self.lock_miss = 0
            print(f"[*] target locked at ({int(c[0])},{int(c[1])}) — "
                  f"{len(coins)} coin(s) visible")

    # ---- steering ----
    def steer_wasd(self, t, now):
        """Hold W (or S if the coin is below/behind us), strafe with A/D,
        and snap the camera a notch when the coin is far to the side."""
        w, h = self.size
        dx = (t["cx"] - w / 2) / (w / 2)        # -1 .. 1
        below = t["cy"] > h * self.cfg.back_y_frac
        dz = self.cfg.strafe_deadzone
        self.hold(KEY["d"], dx > dz)
        self.hold(KEY["a"], dx < -dz)
        self.hold(KEY["w"], not below)
        self.hold(KEY["s"], below)              # coin between us and camera
        if (not below and abs(dx) > self.cfg.nudge_dx
                and now - self.last_nudge > self.cfg.nudge_cooldown):
            self.portal.tap(KEY["right"] if dx > 0 else KEY["left"],
                            hold=max(0.05, self.cfg.turn_hold))
            self.last_nudge = now

    def steer_click(self, t, now):
        """Click-to-move: aim at the floor just below the coin — pathfinds
        better than clicking the coin sprite itself."""
        if now - self.last_click < self.cfg.click_interval:
            return
        w, h = self.size
        y = min(t["cy"] + t["bh"] * 0.8, h * (1 - self.cfg.roi_bottom) - 4)
        self.portal.click(t["cx"], y, self.click_btn())
        self.last_click = now

    def evade(self, box):
        x1, y1, x2, y2 = box
        w, h = self.size
        direction = 1 if (x1 + x2) / 2 < w / 2 else -1   # player left -> go right
        print("[*] player too close — backing off + evading")
        self.status = "evade"
        self.release_movement()
        self.stop_drag()
        self.hold(KEY["s"], True)               # back away first
        time.sleep(0.35)
        self.hold(KEY["s"], False)
        self.turn45(direction)
        self.turn45(direction)                  # ~90° away
        self.forward_until = time.time() + 1.5
        if self.cfg.mode == "click":
            self.forward_click()
        self.lock = None
        self.last_evade = time.time()

    # ---- stuck detection ----
    def mark_moving(self, active, now):
        if active and self.moving_since is None:
            self.moving_since = now
        elif not active:
            self.moving_since = None

    def check_stuck(self, frame, now):
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        if self.prev_small is not None:
            self.diff_hist.append(
                (now, float(np.mean(cv2.absdiff(small, self.prev_small)))))
        self.prev_small = small
        if self.moving_since and now - self.moving_since > self.cfg.stuck_sec:
            recent = [d for t, d in self.diff_hist if now - t <= self.cfg.stuck_sec]
            if recent and max(recent) < self.cfg.stuck_diff:
                print("[*] looks stuck — backing up, jumping, turning")
                self.status = "stuck"
                self.release_movement()
                self.hold(KEY["s"], True)        # back out of the wall
                time.sleep(0.25)
                self.portal.tap(KEY["space"])
                time.sleep(0.30)
                self.hold(KEY["s"], False)
                self.turn45(self.turn_dir)
                self.forward_until = time.time() + 1.2
                if self.cfg.mode == "click":
                    self.forward_click()
                self.lock = None
                self.lock_miss = 0
                self.moving_since = time.time()

    # ---- debug/dataset helpers ----
    def draw(self, frame, mask, coins, players):
        vis = frame.copy()
        h, w = vis.shape[:2]
        cv2.rectangle(vis, (int(w * self.cfg.roi_side), int(h * self.cfg.roi_top)),
                      (w - int(w * self.cfg.roi_side),
                       h - int(h * self.cfg.roi_bottom)),
                      (80, 80, 80), 1)
        for (cx, cy, a, (x, y, cw, ch)) in coins:
            cv2.rectangle(vis, (x, y), (x + cw, y + ch), (0, 255, 255), 2)
        if self.lock is not None:
            cv2.circle(vis, (int(self.lock["cx"]), int(self.lock["cy"])),
                       20, (255, 0, 255), 3)
            cv2.putText(vis, "LOCK", (int(self.lock["cx"]) - 24,
                                      int(self.lock["cy"]) - 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)
        for (x1, y1, x2, y2) in players:
            cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)),
                          (255, 0, 0), 2)
        cv2.putText(vis, f"{self.status} | coins:{len(coins)} | "
                         f"got:{self.collected} | dry:{int(not self.portal.act)} "
                         "| Q quits",
                    (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        try:
            cv2.imshow("mm2bot", cv2.resize(vis, (w // 2, h // 2)))
            cv2.imshow("mask", cv2.resize(mask, (w // 3, h // 3)))
            k = cv2.waitKey(1) & 0xFF
            if k == 27 or (k not in (255, 0) and chr(k).lower() == "q"):
                request_stop("Q pressed in debug window")
        except cv2.error:
            print("[!] cv2.imshow unavailable — running headless, disabling --debug")
            self.debug = False

    def maybe_save(self, frame, now):
        if self.save_dir and now - self.last_save > 1.5:
            cv2.imwrite(os.path.join(self.save_dir, f"f{int(now * 1000)}.jpg"),
                        frame)
            self.last_save = now

    def print_status(self, coins, now):
        if now - self.last_status < self.cfg.status_every:
            return
        if self.lock is not None:
            extra = (f"locked ({int(self.lock['cx'])},{int(self.lock['cy'])}) "
                     f"area={int(self.lock['area'])}")
        elif now < self.forward_until:
            extra = f"exploring forward {self.forward_until - now:.1f}s"
        else:
            extra = (f"scan {self.scan_count}/{self.cfg.scan_full_circle}, "
                     f"next turn in {max(0.0, self.next_search - now):.1f}s")
        print(f"[{self.status:>7s}] coins: {len(coins):2d} | "
              f"got: {self.collected:3d} | {extra}")
        self.last_status = now

    # ---- one iteration ----
    def tick(self):
        frame = self.grab.frame()
        if frame is None:
            print("[!] no frame from PipeWire (stream paused?)")
            time.sleep(0.5)
            return
        h, w = frame.shape[:2]
        self.size = (w, h)
        self.frame_i += 1
        now = time.time()

        coins, mask = detect_coins(frame, self.cfg)
        if self.players.model and self.frame_i % self.cfg.yolo_every == 0:
            self.cached_players = self.players.detect(frame)
        players = self.cached_players
        coins = [c for c in coins
                 if not coin_near_player(c, players, self.cfg.player_coin_margin)]

        # 1) danger: a player fills too much of the screen -> run away
        danger = None
        for (x1, y1, x2, y2) in players:
            if (x2 - x1) * (y2 - y1) > self.cfg.player_danger_frac * w * h:
                danger = (x1, y1, x2, y2)
                break
        if danger and now - self.last_evade > self.cfg.evade_cooldown:
            self.evade(danger)
            return

        # 2) confirmation streak + one-coin-at-a-time target lock
        self.coin_streak = (self.coin_streak + 1
                            if len(coins) >= self.cfg.min_coins else 0)
        self.update_lock(coins, w, h)

        if self.lock is not None:
            # 3) chase the ONE locked coin
            self.status = "collect"
            self.scan_count = 0
            self.forward_until = 0.0
            self.next_search = now + self.cfg.no_coin_turn_sec
            if self.cfg.mode == "click":
                self.steer_click(self.lock, now)
            else:
                self.steer_wasd(self.lock, now)
            self.mark_moving(True, now)
        else:
            # 4) nothing locked: explore/scan (non-blocking — keeps detecting)
            self.status = "search"
            if self.cfg.mode == "wasd":
                self.hold(KEY["a"], False)
                self.hold(KEY["d"], False)
                self.hold(KEY["s"], False)
                self.hold(KEY["w"], now < self.forward_until)
            self.mark_moving(now < self.forward_until, now)

            if now >= self.next_search and now >= self.forward_until:
                if self.scan_count < self.cfg.scan_full_circle:
                    self.scan_count += 1
                    print(f"[*] no coins — scanning 45° "
                          f"({self.scan_count}/{self.cfg.scan_full_circle})")
                    self.turn45(self.turn_dir)
                    self.next_search = time.time() + self.cfg.scan_step_sec
                else:
                    self.scan_count = 0
                    print("[*] full circle scanned — exploring forward")
                    self.forward_until = now + self.cfg.explore_sec
                    if self.cfg.mode == "click":
                        self.forward_click()
                    self.next_search = self.forward_until + self.cfg.scan_step_sec

        self.check_stuck(frame, now)
        self.maybe_save(frame, now)
        self.print_status(coins, now)
        if self.debug:
            self.draw(frame, mask, coins, players)


# ========================= main =========================

def main():
    ap = argparse.ArgumentParser(
        description="External CV coin bot for MM2 (KDE Wayland + Sober). "
                    "Captures via ScreenCast portal, acts via RemoteDesktop "
                    "portal. Run --tune first!")
    ap.add_argument("--mode", choices=["click", "wasd"], default="click",
                    help="click = right-click-to-move (default), wasd = hold W")
    ap.add_argument("--turn", choices=["auto", "keys", "comma", "drag"],
                    default="auto",
                    help="45° turn method: keys = LEFT/RIGHT ARROW (default), "
                         "comma = ',' (left only), drag = RMB camera sweep")
    ap.add_argument("--turn-hold", type=float, default=0.0, metavar="SEC",
                    help="hold the turn key SEC seconds instead of tapping "
                         "(use ~0.35 if the camera rotates smoothly instead "
                         "of snapping 45°)")
    ap.add_argument("--button", choices=["right", "left"], default="right",
                    help="which mouse button click-to-move uses (yours: right)")
    ap.add_argument("--tune", action="store_true",
                    help="interactive HSV tuner: F freezes after 5s, click "
                         "coins ON THE PREVIEW, S saves, Q quits. Keys work "
                         "in the terminal too. Sends NO game input")
    ap.add_argument("--debug", action="store_true",
                    help="show detection overlay + HSV mask windows (Q quits)")
    ap.add_argument("--no-act", action="store_true",
                    help="dry run: capture + detect only, never send input")
    ap.add_argument("--hz", type=float, default=15.0,
                    help="loop rate (default 15)")
    ap.add_argument("--save-frames", metavar="DIR", default=None,
                    help="save a screenshot every 1.5s (to build a YOLO dataset)")
    args = ap.parse_args()

    cfg = Cfg(mode=args.mode, click_button=args.button, loop_hz=args.hz,
              turn_hold=args.turn_hold)
    load_config(cfg)                       # ~/.config/mm2bot.json overrides
    cfg.turn_method = "keys" if args.turn == "auto" else args.turn

    portal = Portal(cfg)

    # ---------- tune mode: capture only, no input, no bot ----------
    if args.tune:
        portal.act = False
        print("[i] requesting portal session — pick the monitor Sober is on")
        fd, node = portal.open(want_input=False)
        grab = Grabber(fd, node)
        try:
            Tuner(cfg, grab).run()
        finally:
            grab.close()
            portal.close()
            print("[i] tuner closed")
        return

    # ---------- normal bot ----------
    portal.act = not args.no_act
    print("[i] requesting portal session — in the KDE dialog, pick the monitor "
          "Sober is on" + (" (capture only)" if args.no_act else " and allow input"))
    fd, node = portal.open(want_input=not args.no_act)
    grab = Grabber(fd, node)
    bot = Bot(cfg, portal, grab, debug=args.debug, save_dir=args.save_frames)

    print(f"[i] mode={cfg.mode} turn={cfg.turn_method} turn_hold={cfg.turn_hold} "
          f"button={cfg.click_button} act={portal.act} hz={cfg.loop_hz}")
    print(f"[i] hsv range: {list(cfg.hsv_lo)} .. {list(cfg.hsv_hi)} "
          f"(run --tune if detection is off)")
    print("[i] starting in 4 s — CLICK THE SOBER WINDOW NOW so it has keyboard focus")
    print(f"[i] stop: Ctrl+C once (twice = force), Q in debug window, "
          f"or touch {cfg.stop_file}")
    for _ in range(40):                        # 4 s, but abortable
        if STOP:
            break
        time.sleep(0.1)

    if portal.act and not STOP:
        print("[i] sending a TEST JUMP — your character should hop right now. "
              "If not, input isn't reaching the game (window not focused?)")
        portal.tap(KEY["space"])
        time.sleep(1.0)

    period = 1.0 / cfg.loop_hz
    try:
        while not STOP:
            if os.path.exists(cfg.stop_file):
                os.remove(cfg.stop_file)
                print("[i] stop file detected — exiting")
                break
            t0 = time.time()
            bot.tick()
            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        pass
    finally:
        print("[i] shutting down (releasing keys, closing stream)...")
        portal.release_all()
        grab.close()
        portal.close()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        print("[i] clean exit")


if __name__ == "__main__":
    main()
