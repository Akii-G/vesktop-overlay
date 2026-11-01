#!/usr/bin/env python3
"""Vesktop Overlay: a Mac-style voice chat + webcam overlay for games.

Receives data from the OverlayBridge Vencord plugin over 127.0.0.1 and draws a
transparent, click-through, always-on-top window.

Usage:
  vesktop-overlay            run the overlay
  vesktop-overlay --demo     show fake people and cams to preview the look
Toggle visibility:  pkill -USR1 -f vesktop-overlay
"""

import base64
import hashlib
import json
import math
import os
import signal
import socketserver
import sys
import threading
import time
import urllib.request

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "vesktop-overlay")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "vesktop-overlay", "avatars")
DEFAULT_CONFIG = {"port": 6889, "monitor": 0, "margin": 24, "font": "Inter"}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(CONFIG_DIR, "config.json")
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    except (OSError, ValueError) as e:
        print(f"[overlay] bad config ({e}), using defaults", file=sys.stderr)
    return cfg


def pick_backend():
    """GNOME has no layer-shell, but it keeps XWayland popup windows on top.
    Other Wayland desktops (COSMIC, KDE, wlroots) get a real overlay layer."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
    if wayland and "GNOME" not in desktop and os.environ.get("VO_FORCE_X11") != "1":
        try:
            import gi
            gi.require_version("GtkLayerShell", "0.1")
            return "layershell"
        except (ImportError, ValueError):
            pass
    os.environ["GDK_BACKEND"] = "x11"
    return "x11"


BACKEND = pick_backend()

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo  # noqa: E402
import cairo  # noqa: E402

if BACKEND == "layershell":
    from gi.repository import GtkLayerShell  # noqa: E402


# ---------------------------------------------------------------- helpers

def hex_rgb(value, fallback=(0.19, 0.82, 0.35)):
    try:
        v = value.lstrip("#")
        return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (AttributeError, ValueError, IndexError):
        return fallback


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def approach(current, target, dt, speed=10.0):
    return current + (target - current) * min(1.0, dt * speed)


def initials(name):
    parts = [p for p in name.split() if p]
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper() if parts else "?"


def name_color(name):
    h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16)
    hue = (h % 360) / 360
    import colorsys
    return colorsys.hsv_to_rgb(hue, 0.45, 0.75)


# ---------------------------------------------------------------- avatars

class AvatarCache:
    def __init__(self, on_ready):
        self.on_ready = on_ready
        self.pixbufs = {}
        self.pending = set()
        os.makedirs(CACHE_DIR, exist_ok=True)

    def get(self, url):
        if not url:
            return None
        if url in self.pixbufs:
            return self.pixbufs[url]
        if url not in self.pending:
            self.pending.add(url)
            threading.Thread(target=self._fetch, args=(url,), daemon=True).start()
        return None

    def _fetch(self, url):
        path = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".png")
        try:
            if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 86400:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 VesktopOverlay"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                with open(path, "wb") as f:
                    f.write(data)
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 128, 128, True)
        except Exception as e:  # network or decode problems just mean "no avatar"
            print(f"[overlay] avatar failed: {e}", file=sys.stderr)
            pix = None
        GLib.idle_add(self._done, url, pix)

    def _done(self, url, pix):
        self.pending.discard(url)
        self.pixbufs[url] = pix
        self.on_ready()
        return False


# ---------------------------------------------------------------- network

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        app = self.server.app
        for raw in self.rfile:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("t") == "frame":
                pix = decode_frame(msg.get("data", ""))
                if pix is not None:
                    GLib.idle_add(app.on_frame, msg.get("id"), pix)
            else:
                GLib.idle_add(app.on_message, msg)
        GLib.idle_add(app.on_disconnect)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def decode_frame(b64):
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
        loader.write(base64.b64decode(b64))
        loader.close()
        return loader.get_pixbuf()
    except Exception:
        return None


# ---------------------------------------------------------------- the overlay

class Person:
    def __init__(self, data):
        self.glow = 0.0          # 0..1 speaking glow, eased
        self.alpha = 0.0         # fade in/out
        self.last_spoke = 0.0
        self.update(data)

    def update(self, data):
        self.data = data
        self.id = data["id"]
        if data.get("speaking"):
            self.last_spoke = time.monotonic()


class Overlay(Gtk.Window):
    def __init__(self, cfg, demo=False):
        super().__init__(type=Gtk.WindowType.TOPLEVEL if BACKEND == "layershell" else Gtk.WindowType.POPUP)
        self.cfg = cfg
        self.demo = demo
        self.visible_toggle = True
        self.channel = None
        self.people = {}            # id -> Person
        self.order = []
        self.frames = {}            # id -> (pixbuf, time)
        self.toasts = []            # dicts: text, avatar, kind, born
        self.ui = {"position": "top-left", "scale": 1.0, "opacity": 0.85, "accent": "#30d158",
                   "onlySpeaking": False, "showWebcams": True, "webcamWidth": 280,
                   "style": "discord", "showNames": "always", "avatarSize": "large"}
        self.avatars = AvatarCache(self.queue_draw)
        self.last_tick = time.monotonic()
        self.font = cfg.get("font", "Inter")

        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_title("Vesktop Overlay")
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is None or not screen.is_composited():
            print("[overlay] no compositor with transparency found; the overlay may look black", file=sys.stderr)
        else:
            self.set_visual(visual)

        display = Gdk.Display.get_default()
        monitor = display.get_monitor(min(cfg.get("monitor", 0), display.get_n_monitors() - 1)) \
            or display.get_primary_monitor()
        geo = monitor.get_geometry()
        self.geo = geo

        if BACKEND == "layershell":
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_monitor(self, monitor)
            for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                         GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
                GtkLayerShell.set_anchor(self, edge, True)
            GtkLayerShell.set_exclusive_zone(self, -1)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
            GtkLayerShell.set_namespace(self, "vesktop-overlay")
        else:
            self.move(geo.x, geo.y)
            self.set_keep_above(True)
            self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.set_default_size(geo.width, geo.height)
        self.resize(geo.width, geo.height)

        self.connect("draw", self.on_draw)
        self.connect("realize", self.make_click_through)
        self.connect("size-allocate", lambda *_: self.make_click_through())
        self.show_all()
        self.make_click_through()

        GLib.timeout_add(16, self.tick)
        GLib.timeout_add_seconds(1, self.cleanup)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.toggle)

        # Fullscreen games: a game going fullscreen (or changing resolution) is
        # raised above us, so we keep re-raising and re-fitting the screen.
        if BACKEND == "x11":
            GLib.timeout_add(500, self.keep_on_top)
            screen.connect("monitors-changed", lambda *_: self.refit())
            screen.connect("size-changed", lambda *_: self.refit())

        if demo:
            self.start_demo()

    # ---- window behaviour

    def make_click_through(self, *_):
        win = self.get_window()
        if win is not None:
            win.input_shape_combine_region(cairo.Region(), 0, 0)
            if BACKEND == "x11":
                win.set_override_redirect(True)
                win.raise_()

    def keep_on_top(self):
        win = self.get_window()
        if win is not None and self.channel and self.visible_toggle:
            win.raise_()
        return True

    def refit(self):
        display = Gdk.Display.get_default()
        monitor = display.get_monitor(min(self.cfg.get("monitor", 0), display.get_n_monitors() - 1)) \
            or display.get_primary_monitor()
        if monitor is None:
            return
        geo = monitor.get_geometry()
        if (geo.x, geo.y, geo.width, geo.height) != (self.geo.x, self.geo.y, self.geo.width, self.geo.height):
            self.geo = geo
            self.move(geo.x, geo.y)
            self.resize(geo.width, geo.height)
            self.make_click_through()
            self.queue_draw()

    def toggle(self):
        self.visible_toggle = not self.visible_toggle
        self.queue_draw()
        return True

    # ---- data in

    def on_message(self, msg):
        t = msg.get("t")
        if t == "state":
            self.ui.update(msg.get("config") or {})
            self.channel = msg.get("channel")
            users = msg.get("users") or []
            seen = set()
            for u in users:
                seen.add(u["id"])
                if u["id"] in self.people:
                    self.people[u["id"]].update(u)
                else:
                    self.people[u["id"]] = Person(u)
            for gone in set(self.people) - seen:
                del self.people[gone]
            self.order = [u["id"] for u in users]
            if not self.channel:
                self.frames.clear()
        elif t == "speaking":
            p = self.people.get(msg.get("id"))
            if p:
                p.data["speaking"] = bool(msg.get("on"))
                if p.data["speaking"]:
                    p.last_spoke = time.monotonic()
        elif t == "toast":
            self.toasts.append({"text": msg.get("text", ""), "avatar": msg.get("avatar"),
                                "kind": msg.get("kind"), "born": time.monotonic()})
            self.toasts = self.toasts[-3:]
        self.queue_draw()
        return False

    def on_frame(self, uid, pix):
        self.frames[uid] = (pix, time.monotonic())
        self.queue_draw()
        return False

    def on_disconnect(self):
        if not self.demo:
            self.channel = None
            self.people.clear()
            self.frames.clear()
            self.queue_draw()
        return False

    def cleanup(self):
        now = time.monotonic()
        for uid in [k for k, (_, ts) in self.frames.items() if now - ts > 4]:
            del self.frames[uid]
        self.toasts = [t for t in self.toasts if now - t["born"] < 4.5]
        return True

    # ---- animation

    def tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last_tick)
        self.last_tick = now
        animating = False
        for p in self.people.values():
            target_glow = 1.0 if p.data.get("speaking") else 0.0
            target_alpha = 1.0 if self.wants_visible(p, now) else 0.0
            p.glow = approach(p.glow, target_glow, dt, 14 if target_glow else 5)
            p.alpha = approach(p.alpha, target_alpha, dt, 8)
            if abs(p.glow - target_glow) > 0.01 or abs(p.alpha - target_alpha) > 0.01:
                animating = True
        if self.toasts:
            animating = True
        if animating:
            self.queue_draw()
        return True

    def wants_visible(self, p, now):
        if not self.ui.get("onlySpeaking"):
            return True
        return p.data.get("speaking") or now - p.last_spoke < 1.5 or p.data.get("video")

    # ---- drawing

    def layout_text(self, cr, text, size, weight=Pango.Weight.SEMIBOLD):
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription(f"{self.font}, Sans")
        desc.set_absolute_size(size * Pango.SCALE)
        desc.set_weight(weight)
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        return layout

    def glass(self, cr, x, y, w, h, r, alpha_mul=1.0):
        op = self.ui.get("opacity", 0.85) * alpha_mul
        # soft shadow
        for i in range(6, 0, -1):
            rounded(cr, x - i, y - i + 3, w + 2 * i, h + 2 * i, r + i)
            cr.set_source_rgba(0, 0, 0, 0.025 * alpha_mul)
            cr.fill()
        rounded(cr, x, y, w, h, r)
        grad = cairo.LinearGradient(0, y, 0, y + h)
        grad.add_color_stop_rgba(0, 0.20, 0.20, 0.23, op)
        grad.add_color_stop_rgba(1, 0.11, 0.11, 0.13, op)
        cr.set_source(grad)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.13 * alpha_mul)
        cr.set_line_width(1)
        cr.stroke()
        # top highlight line, like macOS materials
        rounded(cr, x + 1, y + 1, w - 2, h - 2, r - 1)
        hl = cairo.LinearGradient(0, y, 0, y + h * 0.5)
        hl.add_color_stop_rgba(0, 1, 1, 1, 0.07 * alpha_mul)
        hl.add_color_stop_rgba(1, 1, 1, 1, 0)
        cr.set_source(hl)
        cr.fill()

    def draw_avatar(self, cr, p, cx, cy, radius, alpha):
        accent = hex_rgb(self.ui.get("accent"))
        if p.glow > 0.01 and self.ui.get("style") == "discord":
            cr.arc(cx, cy, radius + 1.5, 0, 2 * math.pi)
            cr.set_source_rgba(*accent, p.glow * alpha)
            cr.set_line_width(max(2.0, radius * 0.14))
            cr.stroke()
        elif p.glow > 0.01:
            for i in range(4, 0, -1):
                cr.arc(cx, cy, radius + 1.5 + i * 1.6 * p.glow, 0, 2 * math.pi)
                cr.set_source_rgba(*accent, 0.12 * p.glow * alpha)
                cr.fill()
            cr.arc(cx, cy, radius + 2, 0, 2 * math.pi)
            cr.set_source_rgba(*accent, p.glow * alpha)
            cr.set_line_width(2.2)
            cr.stroke()
        pix = self.avatars.get(p.data.get("avatar"))
        cr.save()
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.clip()
        if pix:
            s = (radius * 2) / pix.get_width()
            cr.translate(cx - radius, cy - radius)
            cr.scale(s, s)
            Gdk.cairo_set_source_pixbuf(cr, pix, 0, 0)
            cr.paint_with_alpha(alpha)
        else:
            cr.set_source_rgba(*name_color(p.data.get("name", "?")), alpha)
            cr.paint()
            lay = self.layout_text(cr, initials(p.data.get("name", "?")), radius * 0.8, Pango.Weight.BOLD)
            w, h = lay.get_pixel_size()
            cr.move_to(cx - w / 2, cy - h / 2)
            cr.set_source_rgba(1, 1, 1, alpha)
            PangoCairo.show_layout(cr, lay)
        cr.restore()

    def icon_mic_off(self, cr, x, y, s, rgba):
        cr.new_path()
        cr.set_source_rgba(*rgba)
        cr.set_line_width(s * 0.12)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        rounded(cr, x + s * 0.35, y + s * 0.08, s * 0.3, s * 0.52, s * 0.15)
        cr.fill()
        cr.arc_negative(x + s * 0.5, y + s * 0.45, s * 0.28, math.pi, 0)
        cr.stroke()
        cr.move_to(x + s * 0.5, y + s * 0.73)
        cr.line_to(x + s * 0.5, y + s * 0.9)
        cr.stroke()
        self.slash(cr, x, y, s)

    def icon_deaf(self, cr, x, y, s, rgba):
        cr.new_path()
        cr.set_source_rgba(*rgba)
        cr.set_line_width(s * 0.12)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.arc(x + s * 0.5, y + s * 0.55, s * 0.36, math.pi, 2 * math.pi)
        cr.stroke()
        rounded(cr, x + s * 0.1, y + s * 0.5, s * 0.2, s * 0.35, s * 0.06)
        cr.fill()
        rounded(cr, x + s * 0.7, y + s * 0.5, s * 0.2, s * 0.35, s * 0.06)
        cr.fill()
        self.slash(cr, x, y, s)

    def slash(self, cr, x, y, s):
        cr.set_line_width(s * 0.13)
        cr.move_to(x + s * 0.12, y + s * 0.08)
        cr.line_to(x + s * 0.88, y + s * 0.92)
        cr.stroke()

    def icon_cam(self, cr, x, y, s, rgba):
        cr.new_path()
        cr.set_source_rgba(*rgba)
        rounded(cr, x + s * 0.05, y + s * 0.25, s * 0.62, s * 0.5, s * 0.1)
        cr.fill()
        cr.move_to(x + s * 0.7, y + s * 0.5)
        cr.line_to(x + s * 0.95, y + s * 0.3)
        cr.line_to(x + s * 0.95, y + s * 0.7)
        cr.close_path()
        cr.fill()

    def badge(self, cr, x, y, text, s, rgba, alpha):
        lay = self.layout_text(cr, text, s * 0.62, Pango.Weight.BOLD)
        w, h = lay.get_pixel_size()
        bw = w + s * 0.6
        rounded(cr, x - bw, y, bw, s, s / 2)
        cr.set_source_rgba(*rgba[:3], alpha)
        cr.fill()
        cr.move_to(x - bw + (bw - w) / 2, y + (s - h) / 2)
        cr.set_source_rgba(1, 1, 1, alpha)
        PangoCairo.show_layout(cr, lay)
        return bw

    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        if not self.visible_toggle or not self.channel:
            return False
        if self.ui.get("style", "discord") == "discord":
            return self.draw_discord(cr)

        sc = float(self.ui.get("scale", 1.0))
        now = time.monotonic()
        margin = self.cfg.get("margin", 24)
        listw = 250 * sc
        row_h = 42 * sc
        gap = 6 * sc
        header_h = 34 * sc
        cam_w = float(self.ui.get("webcamWidth", 280)) * sc

        people = [self.people[i] for i in self.order if i in self.people and self.people[i].alpha > 0.01]
        cams = [self.people[i] for i in self.order if i in self.people and self.people[i].data.get("video")
                and self.ui.get("showWebcams", True)]

        # Measure first so the whole block can sit in any corner.
        h_list = header_h + gap + sum((row_h + gap) * p.alpha for p in people)
        cam_heights = []
        for p in cams:
            frame = self.frames.get(p.id)
            ratio = (frame[0].get_height() / frame[0].get_width()) if frame else 9 / 16
            cam_heights.append(cam_w * ratio)
        h_cams = sum(h + gap for h in cam_heights)
        h_toasts = len(self.toasts) * (34 * sc + gap)
        total_h = h_toasts + h_list + (gap * 2 + h_cams if cams else 0)
        total_w = max(listw, cam_w if cams else 0)

        pos = self.ui.get("position", "top-left")
        W, H = self.geo.width, self.geo.height
        x0 = margin if "left" in pos else W - margin - total_w
        if pos.startswith("top"):
            y = margin
        elif pos.startswith("bottom"):
            y = H - margin - total_h
        else:
            y = (H - total_h) / 2
        y = max(margin, y)
        right = "right" in pos

        # toasts
        for t in self.toasts:
            age = now - t["born"]
            a = min(1, age * 5) * min(1, max(0, (4.5 - age) * 2))
            tw = listw
            tx = x0 + (total_w - tw if right else 0) + (1 - min(1, age * 5)) * (-20 if not right else 20)
            th = 34 * sc
            self.glass(cr, tx, y, tw, th, th / 2, a)
            dot = (0.19, 0.82, 0.35) if t["kind"] == "join" else (1, 0.27, 0.23)
            cr.arc(tx + 14 * sc, y + th / 2, 4 * sc, 0, 2 * math.pi)
            cr.set_source_rgba(*dot, a)
            cr.fill()
            lay = self.layout_text(cr, t["text"], 13 * sc)
            lay.set_width(int((tw - 34 * sc) * Pango.SCALE))
            _, lh = lay.get_pixel_size()
            cr.move_to(tx + 26 * sc, y + (th - lh) / 2)
            cr.set_source_rgba(1, 1, 1, a)
            PangoCairo.show_layout(cr, lay)
            y += th + gap

        lx = x0 + (total_w - listw if right else 0)

        # header pill
        self.glass(cr, lx, y, listw, header_h, header_h / 2)
        accent = hex_rgb(self.ui.get("accent"))
        anyone = any(p.data.get("speaking") for p in self.people.values())
        pulse = 0.55 + 0.45 * math.sin(now * 4) if anyone else 1
        cr.arc(lx + 16 * sc, y + header_h / 2, 4.5 * sc, 0, 2 * math.pi)
        cr.set_source_rgba(*accent, pulse)
        cr.fill()
        title = self.channel.get("name") or "Voice"
        lay = self.layout_text(cr, title, 13.5 * sc, Pango.Weight.BOLD)
        count = self.layout_text(cr, str(len(self.people)), 12 * sc, Pango.Weight.SEMIBOLD)
        cw, ch = count.get_pixel_size()
        lay.set_width(int((listw - 52 * sc - cw) * Pango.SCALE))
        _, lh = lay.get_pixel_size()
        cr.move_to(lx + 28 * sc, y + (header_h - lh) / 2)
        cr.set_source_rgba(1, 1, 1, 0.95)
        PangoCairo.show_layout(cr, lay)
        cr.move_to(lx + listw - 14 * sc - cw, y + (header_h - ch) / 2)
        cr.set_source_rgba(1, 1, 1, 0.55)
        PangoCairo.show_layout(cr, count)
        y += header_h + gap

        # people
        for p in people:
            a = p.alpha
            rh = row_h
            self.glass(cr, lx, y, listw, rh, 13 * sc, a)
            if p.glow > 0.01:
                rounded(cr, lx, y, listw, rh, 13 * sc)
                cr.set_source_rgba(*accent, 0.55 * p.glow * a)
                cr.set_line_width(1.6)
                cr.stroke()
            rad = 14 * sc
            self.draw_avatar(cr, p, lx + 10 * sc + rad, y + rh / 2, rad, a)

            # status icons from the right edge
            ix = lx + listw - 10 * sc
            isz = 16 * sc
            red = (1, 0.27, 0.23, a)
            grey = (1, 1, 1, 0.75 * a)
            if p.data.get("deaf"):
                ix -= isz
                self.icon_deaf(cr, ix, y + (rh - isz) / 2, isz, red)
                ix -= 5 * sc
            elif p.data.get("mute"):
                ix -= isz
                self.icon_mic_off(cr, ix, y + (rh - isz) / 2, isz, red)
                ix -= 5 * sc
            if p.data.get("video"):
                ix -= isz
                self.icon_cam(cr, ix, y + (rh - isz) / 2, isz, grey)
                ix -= 5 * sc
            if p.data.get("live"):
                bw = self.badge(cr, ix, y + (rh - 16 * sc) / 2, "LIVE", 16 * sc, (1, 0.23, 0.19), a)
                ix -= bw + 5 * sc

            name_x = lx + 18 * sc + rad * 2
            lay = self.layout_text(cr, p.data.get("name", "?"), 13.5 * sc,
                                   Pango.Weight.BOLD if p.data.get("speaking") else Pango.Weight.MEDIUM)
            lay.set_width(int(max(10, ix - name_x - 4 * sc) * Pango.SCALE))
            _, lh = lay.get_pixel_size()
            cr.move_to(name_x, y + (rh - lh) / 2)
            dim = 0.55 if p.data.get("deaf") else 1
            cr.set_source_rgba(1, 1, 1, a * dim)
            PangoCairo.show_layout(cr, lay)
            y += (rh + gap) * a

        # webcams
        if cams:
            y += gap
            cx0 = x0 + (total_w - cam_w if right else 0)
            for p, ch_ in zip(cams, cam_heights):
                self.draw_cam(cr, p, cx0, y, cam_w, ch_, sc, now)
                y += ch_ + gap
        return False

    def draw_discord(self, cr):
        """Looks like Discord's own in-game overlay: round avatars with a green
        speaking ring and names on small dark labels, no background panel."""
        sc = float(self.ui.get("scale", 1.0))
        now = time.monotonic()
        margin = self.cfg.get("margin", 24)
        av = (32 if self.ui.get("avatarSize", "large") == "large" else 24) * sc
        gap = 8 * sc
        names = self.ui.get("showNames", "always")
        cam_w = float(self.ui.get("webcamWidth", 280)) * sc
        red = (0.95, 0.25, 0.26)
        op = self.ui.get("opacity", 0.85)

        people = [self.people[i] for i in self.order if i in self.people and self.people[i].alpha > 0.01]
        cams = [self.people[i] for i in self.order if i in self.people and self.people[i].data.get("video")
                and self.ui.get("showWebcams", True)]

        def label_alpha(p):
            if names == "never":
                return 0.0
            if names == "speaking":
                return p.glow
            return 1.0

        # measure
        rows = []
        width = 0
        for p in people:
            lay = self.layout_text(cr, p.data.get("name", "?"), 14 * sc, Pango.Weight.SEMIBOLD)
            lay.set_width(int(200 * sc * Pango.SCALE))
            tw, th = lay.get_pixel_size()
            icon = 14 * sc if (p.data.get("mute") or p.data.get("deaf")) else 0
            lw = tw + 12 * sc + (icon + 5 * sc if icon else 0)
            if p.data.get("live"):
                lw += 40 * sc
            rows.append((p, lay, tw, th, icon, lw))
            width = max(width, av + 8 * sc + (lw if label_alpha(p) > 0 or names != "never" else 0))
        cam_heights = []
        for p in cams:
            frame = self.frames.get(p.id)
            ratio = (frame[0].get_height() / frame[0].get_width()) if frame else 9 / 16
            cam_heights.append(cam_w * ratio)
        list_h = sum((av + gap) * p.alpha for p in people)
        cams_h = sum(h + gap for h in cam_heights)
        total_h = list_h + (gap + cams_h if cams else 0)
        total_w = max(width, cam_w if cams else 0)

        pos = self.ui.get("position", "top-left")
        W, H = self.geo.width, self.geo.height
        right = "right" in pos
        x0 = margin if not right else W - margin - total_w
        if pos.startswith("top"):
            y = margin
        elif pos.startswith("bottom"):
            y = H - margin - total_h
        else:
            y = (H - total_h) / 2
        y = max(margin, y)

        for p, lay, tw, th, icon, lw in rows:
            a = p.alpha
            # mirrored on the right side, like Discord
            ax = x0 + total_w - av if right else x0
            self.draw_avatar(cr, p, ax + av / 2, y + av / 2, av / 2, a)
            if names == "never" and icon:
                # small red badge on the avatar instead of a label
                bx, by, br = ax + av - 5 * sc, y + av - 5 * sc, 7 * sc
                cr.arc(bx, by, br, 0, 2 * math.pi)
                cr.set_source_rgba(0.12, 0.12, 0.13, a)
                cr.fill()
                (self.icon_deaf if p.data.get("deaf") else self.icon_mic_off)(
                    cr, bx - br * 0.7, by - br * 0.7, br * 1.4, (*red, a))
            la = label_alpha(p) * a
            if la > 0.01:
                lh = th + 6 * sc
                lx = ax - 8 * sc - lw if right else ax + av + 8 * sc
                ly = y + (av - lh) / 2
                rounded(cr, lx, ly, lw, lh, 4 * sc)
                cr.set_source_rgba(0.12, 0.12, 0.14, op * la)
                cr.fill()
                tx = lx + 6 * sc
                cr.move_to(tx, ly + 3 * sc)
                bright = 1.0 if p.data.get("speaking") else 0.82
                cr.set_source_rgba(bright, bright, bright, la * (0.6 if p.data.get("deaf") else 1))
                PangoCairo.show_layout(cr, lay)
                tx += tw + 5 * sc
                if icon:
                    (self.icon_deaf if p.data.get("deaf") else self.icon_mic_off)(
                        cr, tx, ly + (lh - icon) / 2, icon, (*red, la))
                    tx += icon + 5 * sc
                if p.data.get("live"):
                    self.badge(cr, tx + 34 * sc, ly + (lh - 15 * sc) / 2, "LIVE", 15 * sc, (0.95, 0.25, 0.26), la)
            y += (av + gap) * a

        if cams:
            y += gap
            cx0 = x0 + (total_w - cam_w if right else 0)
            for p, ch_ in zip(cams, cam_heights):
                self.draw_cam(cr, p, cx0, y, cam_w, ch_, sc, now)
                y += ch_ + gap

        self.draw_discord_toasts(cr, sc, now)
        return False

    def draw_discord_toasts(self, cr, sc, now):
        # Discord shows its popups in the bottom-right corner of the screen.
        margin = self.cfg.get("margin", 24)
        w, h = 300 * sc, 56 * sc
        y = self.geo.height - margin - h
        for t in reversed(self.toasts):
            age = now - t["born"]
            slide = min(1, age * 5)
            a = slide * min(1, max(0, (4.5 - age) * 2))
            x = self.geo.width - margin - w + (1 - slide) * 40 * sc
            for i in range(6, 0, -1):
                rounded(cr, x - i, y - i + 3, w + 2 * i, h + 2 * i, 8 * sc + i)
                cr.set_source_rgba(0, 0, 0, 0.03 * a)
                cr.fill()
            rounded(cr, x, y, w, h, 8 * sc)
            cr.set_source_rgba(0.17, 0.18, 0.19, 0.96 * a)
            cr.fill()
            fake = Person({"id": "toast", "name": t["text"].split(" ")[0], "avatar": t.get("avatar")})
            self.draw_avatar(cr, fake, x + 12 * sc + 16 * sc, y + h / 2, 16 * sc, a)
            title = self.layout_text(cr, t["text"], 14 * sc, Pango.Weight.SEMIBOLD)
            title.set_width(int((w - 70 * sc) * Pango.SCALE))
            sub = self.layout_text(cr, "Voice channel" if t["kind"] in ("join", "leave") else "", 12 * sc,
                                   Pango.Weight.NORMAL)
            _, th = title.get_pixel_size()
            _, sh = sub.get_pixel_size()
            ty = y + (h - th - sh) / 2
            cr.move_to(x + 56 * sc, ty)
            cr.set_source_rgba(1, 1, 1, a)
            PangoCairo.show_layout(cr, title)
            cr.move_to(x + 56 * sc, ty + th)
            cr.set_source_rgba(0.71, 0.73, 0.76, a)
            PangoCairo.show_layout(cr, sub)
            y -= h + 8 * sc

    def draw_cam(self, cr, p, x, y, w, h, sc, now):
        discord = self.ui.get("style") == "discord"
        r = (8 if discord else 16) * sc
        accent = hex_rgb(self.ui.get("accent"))
        for i in range(8, 0, -1):
            rounded(cr, x - i, y - i + 4, w + 2 * i, h + 2 * i, r + i)
            cr.set_source_rgba(0, 0, 0, 0.03)
            cr.fill()
        cr.save()
        rounded(cr, x, y, w, h, r)
        cr.clip()
        frame = self.frames.get(p.id)
        if frame:
            pix = frame[0]
            s = max(w / pix.get_width(), h / pix.get_height())
            cr.save()
            cr.translate(x + (w - pix.get_width() * s) / 2, y + (h - pix.get_height() * s) / 2)
            cr.scale(s, s)
            Gdk.cairo_set_source_pixbuf(cr, pix, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
            cr.restore()
        else:
            cr.set_source_rgba(0.12, 0.12, 0.14, self.ui.get("opacity", 0.85))
            cr.paint()
            rad = min(w, h) * 0.16
            self.draw_avatar(cr, p, x + w / 2, y + h * 0.36, rad, 1)
            hint = self.layout_text(cr, "Open the call view in Vesktop to see this cam", 11 * sc, Pango.Weight.NORMAL)
            hint.set_width(int((w - 20 * sc) * Pango.SCALE))
            hint.set_alignment(Pango.Alignment.CENTER)
            hw, hh = hint.get_pixel_size()
            cr.move_to(x + 10 * sc, y + h * 0.36 + rad + 6 * sc)
            cr.set_source_rgba(1, 1, 1, 0.55)
            PangoCairo.show_layout(cr, hint)
        # bottom fade so the name is readable on any video
        grad = cairo.LinearGradient(0, y + h * 0.6, 0, y + h)
        grad.add_color_stop_rgba(0, 0, 0, 0, 0)
        grad.add_color_stop_rgba(1, 0, 0, 0, 0.55)
        cr.rectangle(x, y, w, h)
        cr.set_source(grad)
        cr.fill()
        cr.restore()

        # name pill
        lay = self.layout_text(cr, p.data.get("name", "?"), 12 * sc)
        lay.set_width(int((w * 0.7) * Pango.SCALE))
        tw, th = lay.get_pixel_size()
        icon = 14 * sc if (p.data.get("mute") or p.data.get("deaf")) else 0
        pw = tw + 20 * sc + (icon + 4 * sc if icon else 0)
        ph = th + 10 * sc
        px, py = x + 10 * sc, y + h - ph - 10 * sc
        rounded(cr, px, py, pw, ph, 4 * sc if discord else ph / 2)
        cr.set_source_rgba(0, 0, 0, 0.6 if discord else 0.45)
        cr.fill()
        tx = px + 10 * sc
        if icon:
            red = (1, 0.27, 0.23, 1)
            (self.icon_deaf if p.data.get("deaf") else self.icon_mic_off)(cr, tx, py + (ph - icon) / 2, icon, red)
            tx += icon + 4 * sc
        cr.move_to(tx, py + 5 * sc)
        cr.set_source_rgba(1, 1, 1, 1)
        PangoCairo.show_layout(cr, lay)

        # border + speaking glow
        rounded(cr, x, y, w, h, r)
        if p.glow > 0.01:
            cr.set_source_rgba(*accent, p.glow)
            cr.set_line_width((2.5 if discord else 3) * sc)
        elif discord:
            cr.set_source_rgba(0, 0, 0, 0.3)
            cr.set_line_width(1)
        else:
            cr.set_source_rgba(1, 1, 1, 0.14)
            cr.set_line_width(1)
        cr.stroke()

    # ---- demo

    def start_demo(self):
        names = ["User1", "User2", "User3", "User4"]
        self.ui["accent"] = "#23a55a"
        self.on_message({"t": "state", "channel": {"name": "Lounge", "guild": "Chilling"},
                         "users": [{"id": str(i), "name": n, "avatar": None, "me": i == 0,
                                    "mute": i == 2, "deaf": i == 3, "video": i in (0, 1), "live": i == 1}
                                   for i, n in enumerate(names)]})
        self.on_message({"t": "toast", "kind": "join", "text": "User2 joined"})
        self._demo_t = 0.0

        def step():
            self._demo_t += 0.1
            for i in ("0", "1"):
                on = math.sin(self._demo_t * (1.3 if i == "0" else 0.8) + int(i)) > 0.4
                self.on_message({"t": "speaking", "id": i, "on": on})
            for i in ("0", "1"):
                self.on_frame(i, self.fake_frame(int(i)))
            return True
        GLib.timeout_add(100, step)

    def fake_frame(self, seed):
        w, h = 320, 180
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
        cr = cairo.Context(surf)
        t = self._demo_t
        g = cairo.LinearGradient(0, 0, w, h)
        g.add_color_stop_rgb(0, 0.2 + 0.2 * math.sin(t + seed), 0.25, 0.55)
        g.add_color_stop_rgb(1, 0.7, 0.3 + 0.2 * math.cos(t), 0.4)
        cr.set_source(g)
        cr.paint()
        cr.arc(w / 2 + 40 * math.sin(t * 0.7 + seed), h / 2, 40, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.fill()
        return Gdk.pixbuf_get_from_surface(surf, 0, 0, w, h)


def main():
    demo = "--demo" in sys.argv
    cfg = load_config()
    win = Overlay(cfg, demo=demo)
    if not demo:
        try:
            server = Server(("127.0.0.1", int(cfg.get("port", 6889))), Handler)
        except OSError as e:
            print(f"[overlay] cannot listen on port {cfg.get('port')}: {e} (already running?)", file=sys.stderr)
            sys.exit(1)
        server.app = win
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[overlay] listening on 127.0.0.1:{cfg.get('port')} ({BACKEND})")
    win.connect("destroy", Gtk.main_quit)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Gtk.main()


if __name__ == "__main__":
    main()
