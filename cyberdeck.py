#!/usr/bin/env python3
"""cyberdeck - neon terminal clock, weather and system monitor over matrix rain.

Python standard library only. System stats come from /proc, /sys, statvfs (what
`df` uses) and, as a fallback for temperature, `sensors`. Weather comes from the
Open-Meteo API (no API key needed).

Keys:  q / Esc  quit   s  settings (city, units, air quality, theme, FPS, rain, scanlines, glitch, boot intro)

Config lives at ~/.config/cyberdeck/config.json. With no city configured, the app
asks for one at launch and saves it there.
"""
import argparse
import copy
import curses
import json
import locale
import os
import random
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

FPS_MIN, FPS_MAX, FPS_STEP = 5, 60, 5
GLITCH_MIN, GLITCH_MAX, GLITCH_STEP = 0.05, 2.0, 0.05  # bursts per second

DEFAULTS = {
    "location": {"city": None, "lat": None, "lon": None},  # asked for on first launch
    "units": "metric",  # metric | imperial
    "fps": 30,
    "clock_24h": True,
    "show_seconds": False,
    "weather_refresh_minutes": 15,
    "air_quality": True,
    "theme": "cyan-magenta",  # cyan-magenta | amber | matrix | mono | synthwave
    "boot_sequence": True,  # ~3s intro at launch; any key skips it
    "disk_path": "/",
    "glitch": {"enabled": True, "rate": 0.35},  # rate: clock glitch bursts per second, on average
    "rain": {"enabled": True, "scanlines": True, "charset": "katakana", "speed": 1.0},  # katakana | ascii
}

# ---------------------------------------------------------------- config ----


def config_path(override):
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "cyberdeck" / "config.json"


def deep_merge(base, over):
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            out[key] = deep_merge(base[key], val)
        else:
            out[key] = val
    return out


def load_config(path):
    """Config with defaults filled in. Nothing is written until a city is chosen."""
    if path.exists():
        try:
            return deep_merge(copy.deepcopy(DEFAULTS), json.loads(path.read_text()))
        except (OSError, ValueError) as err:
            raise SystemExit(f"cyberdeck: cannot read {path}: {err}")
    return copy.deepcopy(DEFAULTS)


def clamp_fps(value):
    try:
        return max(FPS_MIN, min(FPS_MAX, int(value)))
    except (TypeError, ValueError):
        return DEFAULTS["fps"]


def save_config(path, updates):
    """Persist changed settings, leaving the rest of the file (and CLI overrides) alone."""
    try:
        data = json.loads(path.read_text()) if path.exists() else copy.deepcopy(DEFAULTS)
        data = deep_merge(data, updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError):
        pass


# ----------------------------------------------------------------- stats ----


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def read_cpu_times():
    parts = (_read("/proc/stat") or "cpu 0 0 0 0").splitlines()[0].split()[1:]
    vals = [int(v) for v in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return idle, sum(vals[:8])


def read_meminfo():
    info = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        if rest:
            info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    return total - info.get("MemAvailable", total), total


def read_disk(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return 0, 0
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    return used, used + st.f_bavail * st.f_frsize  # same maths as df


HWMON_PREFERRED = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")


def read_temp_sysfs():
    by_name = {}
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        temps = [int(v) / 1000 for v in map(_read, hwmon.glob("temp*_input")) if v and v.lstrip("-").isdigit()]
        if temps:
            by_name[_read(hwmon / "name") or ""] = max(temps)
    for name in HWMON_PREFERRED:
        if name in by_name:
            return by_name[name]
    if by_name:
        return max(by_name.values())
    zones = [int(v) / 1000 for v in map(_read, Path("/sys/class/thermal").glob("thermal_zone*/temp")) if v and v.lstrip("-").isdigit()]
    return max(zones) if zones else None


def read_temp_sensors():
    out = subprocess.run(["sensors", "-j"], capture_output=True, text=True, timeout=3).stdout
    temps = []

    def walk(node):
        for key, val in node.items():
            if isinstance(val, dict):
                walk(val)
            elif key.startswith("temp") and key.endswith("_input"):
                temps.append(float(val))

    walk(json.loads(out))
    return max(temps) if temps else None


def read_uptime():
    """'3d 04h 12m' / '4h 12m' / '12m', or '--' if /proc/uptime is unreadable."""
    try:
        secs = int(float((_read("/proc/uptime") or "").split()[0]))
    except (IndexError, ValueError):
        return "--"
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    mins = rest // 60
    if days:
        return f"{days}d {hours:02d}h {mins:02d}m"
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


def read_battery():
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        cap = _read(bat / "capacity")
        if cap and cap.isdigit():
            return int(cap), _read(bat / "status") or "Unknown"
    return None


class Stats:
    def __init__(self, disk_path):
        self.disk_path = disk_path
        self.cpu = 0.0
        self.mem = (0, 0)
        self.disk = (0, 0)
        self.temp = None
        self.bat = None
        self.host = socket.gethostname().split(".")[0]
        self.uptime = read_uptime()
        self._prev = read_cpu_times()
        self._ticks = 0
        self._use_sensors = True

    def sample(self):
        idle, total = read_cpu_times()
        d_idle, d_total = idle - self._prev[0], total - self._prev[1]
        self._prev = (idle, total)
        if d_total > 0:
            self.cpu = 100.0 * (1 - d_idle / d_total)
        self.mem = read_meminfo()
        self.disk = read_disk(self.disk_path)
        self.bat = read_battery()
        self.uptime = read_uptime()
        if self._ticks % 2 == 0:
            self.temp = read_temp_sysfs()
            if self.temp is None and self._use_sensors and self._ticks % 5 == 0:
                try:
                    self.temp = read_temp_sensors()
                except (OSError, ValueError, subprocess.SubprocessError):
                    self._use_sensors = False
        self._ticks += 1


# --------------------------------------------------------------- weather ----

WMO = {
    0: "CLEAR SKY", 1: "MOSTLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST",
    45: "FOG", 48: "RIME FOG",
    51: "LIGHT DRIZZLE", 53: "DRIZZLE", 55: "HEAVY DRIZZLE",
    56: "FREEZING DRIZZLE", 57: "FREEZING DRIZZLE",
    61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN",
    66: "FREEZING RAIN", 67: "FREEZING RAIN",
    71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW", 77: "SNOW GRAINS",
    80: "RAIN SHOWERS", 81: "RAIN SHOWERS", 82: "VIOLENT SHOWERS",
    85: "SNOW SHOWERS", 86: "HEAVY SNOW SHOWERS",
    95: "THUNDERSTORM", 96: "THUNDERSTORM + HAIL", 99: "THUNDERSTORM + HAIL",
}


# US AQI bands: (upper bound, label, colour)
AQI_BANDS = (
    (50, "GOOD", "ok"), (100, "MODERATE", "warn"), (150, "UNHEALTHY FOR SOME", "warn"),
    (200, "UNHEALTHY", "crit"), (300, "VERY UNHEALTHY", "crit"), (10**9, "HAZARDOUS", "crit"),
)


def aqi_band(aqi):
    return next(b for b in AQI_BANDS if aqi <= b[0])


def http_json(url, params):
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "cyberdeck/1.0"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def geocode(query):
    """'Paris' or 'Paris, FR' / 'Paris, France' -> (LABEL, lat, lon)."""
    name, _, hint = query.partition(",")
    name, hint = name.strip(), hint.strip().lower()
    if not name:
        raise RuntimeError("enter a city name")
    res = http_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": name, "count": 10, "language": "en", "format": "json"},
    ).get("results") or []
    if hint:
        res = [r for r in res if hint in {str(r.get(k, "")).lower() for k in ("country", "country_code", "admin1")}] or res
    if not res:
        raise RuntimeError(f"no match for '{query.strip()}'")
    hit = res[0]
    label = hit["name"] + (", " + hit["country_code"] if hit.get("country_code") else "")
    return label.upper(), hit["latitude"], hit["longitude"]


class Weather(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.loc = cfg["location"]
        self.refresh = max(1, cfg["weather_refresh_minutes"]) * 60
        self.imperial = cfg["units"] == "imperial"
        self.air_quality = cfg["air_quality"]
        self.data = None
        self.error = None
        self.place = None
        self.wake = threading.Event()
        self._gen = 0  # bumped when the location changes so in-flight fetches are dropped
        self._quit = False

    def has_location(self):
        loc = self.loc
        return bool(loc.get("city")) or (loc.get("lat") is not None and loc.get("lon") is not None)

    def toggle_units(self):
        self.imperial = not self.imperial
        self.wake.set()

    def set_location(self, loc, place=None):
        self.loc, self.place = loc, place
        self.data = self.error = None
        self._gen += 1
        if self.is_alive():
            self.wake.set()
        else:
            self.start()

    def _resolve(self):
        if self.place:
            return self.place
        lat, lon, city = self.loc.get("lat"), self.loc.get("lon"), self.loc.get("city")
        if lat is not None and lon is not None:
            return ((city or f"{lat:.2f},{lon:.2f}").upper(), lat, lon)
        return geocode(city)

    def _fetch(self):
        gen, imperial = self._gen, self.imperial
        place = self._resolve()
        _, lat, lon = place
        js = http_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh",
                "timezone": "auto",
                "forecast_days": 1,
            },
        )
        cur, day = js["current"], js["daily"]
        aq = {}
        if self.air_quality:
            try:  # separate API, missing in some places; never let it sink the weather
                aq = http_json(
                    "https://air-quality-api.open-meteo.com/v1/air-quality",
                    {"latitude": lat, "longitude": lon, "current": "us_aqi,pm2_5", "timezone": "auto"},
                )["current"]
            except Exception:
                pass
        if gen != self._gen:  # city changed while we were fetching
            return
        self.place = place
        self.data = {
            "temp": cur["temperature_2m"],
            "feels": cur["apparent_temperature"],
            "hum": cur["relative_humidity_2m"],
            "wind": cur["wind_speed_10m"],
            "desc": WMO.get(cur["weather_code"], "UNKNOWN"),
            "hi": day["temperature_2m_max"][0],
            "lo": day["temperature_2m_min"][0],
            "aqi": aq.get("us_aqi"),
            "pm25": aq.get("pm2_5"),
            "imperial": imperial,
        }

    def run(self):
        while not self._quit:
            gen = self._gen
            try:
                self._fetch()
                error, delay = None, self.refresh
            except Exception as err:  # network down, bad city, API change...
                error, delay = (str(err) or type(err).__name__), 60
            if gen == self._gen:
                self.error = error
            self.wake.wait(delay)
            self.wake.clear()


# ------------------------------------------------------------- big clock ----

_G = {
    "0": ("███", "█ █", "█ █", "█ █", "███"),
    "1": (" █ ", "██ ", " █ ", " █ ", "███"),
    "2": ("███", "  █", "███", "█  ", "███"),
    "3": ("███", "  █", "███", "  █", "███"),
    "4": ("█ █", "█ █", "███", "  █", "  █"),
    "5": ("███", "█  ", "███", "  █", "███"),
    "6": ("███", "█  ", "███", "█ █", "███"),
    "7": ("███", "  █", "  █", "  █", "  █"),
    "8": ("███", "█ █", "███", "█ █", "███"),
    "9": ("███", "█ █", "███", "  █", "███"),
    ":": (" ", "█", " ", "█", " "),
    " ": (" ", " ", " ", " ", " "),  # blinked-off colon
    "_": ("   ",) * 5,  # blank digit (12h leading zero)
}


def render_big(text, scale):
    rows = [""] * 5
    for ch in text:
        for i, line in enumerate(_G[ch]):
            rows[i] += "".join(("█" if c == "█" else " ") * scale for c in line) + " " * scale
    return [r[:-scale] for r in rows]


def split_rows(rows, dx, cyan, magenta, both):
    """Chromatic split: a cyan copy and a magenta copy 2*dx columns apart; white where they overlap."""
    pad = " " * (2 * dx)
    out = []
    for row in rows:
        segs = []
        for a, b in zip(row + pad, pad + row):
            if a != " " and b != " ":
                cell = (a, both)
            elif a != " ":
                cell = (a, cyan)
            elif b != " ":
                cell = (b, magenta)
            else:
                cell = (" ", 0)
            if segs and segs[-1][1] == cell[1]:
                segs[-1] = (segs[-1][0] + cell[0], cell[1])
            else:
                segs.append(cell)
        out.append(segs)
    return out


def scan_flicker(rows, dim, flash):
    """Horizontal scan-line flicker: one or two rows drop out, dim or flash white."""
    rows = list(rows)
    mode = random.choice(("blank", "dim", "flash"))
    for i in random.sample(range(len(rows)), random.choice((1, 2))):
        text = "".join(t for t, _ in rows[i])
        rows[i] = [(" " * len(text), 0)] if mode == "blank" else [(text, dim if mode == "dim" else flash)]
    return rows


GLITCH_CHARS = "▓▒░▀▄"


def glitch_rows(rows):
    """Tear the clock: shift rows sideways, tear one band hard, corrupt a few cells."""
    band = random.randrange(len(rows))
    out = []
    for i, row in enumerate(rows):
        if i == band:
            dx = random.choice((-6, -5, 5, 6))
        else:
            dx = random.choice((-3, -2, -1, 1, 2, 3)) if random.random() < 0.35 else 0
        if dx > 0:
            row = " " * dx + row[:-dx]
        elif dx < 0:
            row = row[-dx:] + " " * -dx
        out.append("".join(
            random.choice(GLITCH_CHARS) if (ch == "█" and random.random() < 0.06)
            else "▒" if (ch == " " and random.random() < 0.004)
            else ch
            for ch in row
        ))
    return out


# -------------------------------------------------------------- the rain ----

HALO_HIDDEN = 3
BOOT_SECS = 3.0
BOOT_LINES = (  # (text, start, fade-ramp attr): typed out over BOOT_TYPE seconds, then faded out at the end
    ("INITIALISING…", 0.0, "fade_c"),
    ("LINK ESTABLISHED", 1.35, "fade_g"),
)
BOOT_TYPE, BOOT_FADE_IN, BOOT_FADE_OUT, BOOT_RAIN_FADE = 0.9, 0.45, 0.6, 2.4
SCANLINE_SHIFT = 1  # ramp steps darker on every other rain row (CRT effect)
HALO_SHIFT = (0, 2, 5, 0)  # how many ramp steps darker the rain is at each halo level
KATAKANA = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789:.=*+-<>"
ASCII = "01234567890ABCDEFXYZ:.=*+-<>|/\\#$%&"


class Rain:
    def __init__(self, h, w, chars, speed):
        self.h, self.w, self.chars, self.speed = h, w, chars, speed
        self.t, self.tick = 0.0, 0  # seconds elapsed, glyph-flicker counter
        self.drops = [self._new(initial=True) for _ in range(w)]

    def _new(self, initial=False):
        length = random.randint(5, max(6, self.h * 2 // 3))
        y = random.uniform(-self.h, self.h) if initial else -random.uniform(0, self.h)
        rows_per_sec = random.uniform(4, 16) * self.speed
        return [y, rows_per_sec, length, random.randrange(1 << 30)]

    def step(self, dt):
        self.t += dt
        self.tick = int(self.t * 8)  # trail glyphs mutate ~8x a second
        for i, d in enumerate(self.drops):
            d[0] += d[1] * dt
            if d[0] - d[2] > self.h:
                self.drops[i] = self._new()

    def draw(self, win, ramp, mask, scanlines=False, fade=0):
        """ramp: attrs from white head down to a faint tail. mask: per-cell halo level (see App.halo_mask)."""
        chars, n, w = self.chars, len(self.chars), self.w
        last = len(ramp) - 1
        for x, (pos, _, length, seed) in enumerate(self.drops):
            head = int(pos)
            for k in range(length + 1):
                y = head - k
                if y < 0 or y >= self.h:
                    continue
                level = mask[y * w + x]
                if level == HALO_HIDDEN:
                    continue
                flick = self.tick if (x + y) % 3 == 0 and k else 0  # the head glyph stays put
                ch = chars[(seed + y * 7919 + flick * 104729) % n]
                idx = 0 if k == 0 else 1 + (k * (last - 2)) // length  # 1 .. last-1
                dim = HALO_SHIFT[level] + fade + (SCANLINE_SHIFT if scanlines and y % 2 else 0)
                put(win, y, x, ch, ramp[min(last, idx + dim)])


# ------------------------------------------------------------------- ui -----


def put(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if x < 0:
        s, x = s[-x:], 0
    s = s[: w - x]
    if s:
        try:
            win.addstr(y, x, s, attr)
        except curses.error:  # writing the bottom-right cell raises after drawing
            pass


# Each theme: 256-colour codes for the UI, four 256-colour ramps (rain trail head->tail, stat-bar
# gradient low->high, two boot-text fades dark->bright), and plain names for 8-colour terminals.
THEMES = {
    "cyan-magenta": {
        "name": "CYAN/MAGENTA",
        "ui": dict(clock=51, date=201, border=201, label=51, weather=213, flash=231, dim=244, ok=46, warn=220, crit=196),
        "rain": (231, 157, 120, 84, 46, 40, 34, 28, 22, 235),
        "grad": (46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196),
        "boot1": (23, 30, 37, 44, 51),
        "boot2": (22, 28, 34, 40, 46),
        "c8": dict(primary="CYAN", accent="MAGENTA", rain="GREEN", ok="GREEN", warn="YELLOW", crit="RED"),
    },
    "amber": {
        "name": "AMBER",
        "ui": dict(clock=214, date=208, border=172, label=178, weather=220, flash=230, dim=130, ok=178, warn=208, crit=196),
        "rain": (230, 222, 220, 214, 208, 172, 136, 94, 58, 235),
        "grad": (94, 130, 136, 172, 178, 214, 220, 226, 208, 202, 196),
        "boot1": (52, 94, 130, 172, 214),
        "boot2": (58, 94, 136, 178, 220),
        "c8": dict(primary="YELLOW", accent="YELLOW", rain="YELLOW", ok="YELLOW", warn="YELLOW", crit="RED"),
    },
    "matrix": {
        "name": "MATRIX",
        "ui": dict(clock=46, date=40, border=34, label=40, weather=82, flash=231, dim=65, ok=46, warn=118, crit=231),
        "rain": (231, 157, 120, 84, 46, 40, 34, 28, 22, 235),
        "grad": (22, 28, 34, 40, 46, 82, 118, 120, 157, 194, 231),
        "boot1": (22, 28, 34, 40, 46),
        "boot2": (22, 28, 34, 40, 46),
        "c8": dict(primary="GREEN", accent="GREEN", rain="GREEN", ok="GREEN", warn="GREEN", crit="WHITE"),
    },
    "mono": {
        "name": "BLACK & WHITE",
        "ui": dict(clock=255, date=250, border=244, label=252, weather=253, flash=231, dim=242, ok=250, warn=253, crit=231),
        "rain": (231, 255, 252, 249, 246, 243, 240, 238, 236, 234),
        "grad": (238, 240, 242, 244, 246, 248, 250, 252, 254, 255, 231),
        "boot1": (236, 240, 244, 250, 255),
        "boot2": (236, 240, 244, 250, 255),
        "c8": dict(primary="WHITE", accent="WHITE", rain="WHITE", ok="WHITE", warn="WHITE", crit="WHITE"),
    },
    "synthwave": {
        "name": "SYNTHWAVE",
        "ui": dict(clock=199, date=51, border=93, label=141, weather=213, flash=231, dim=103, ok=51, warn=214, crit=197),
        "rain": (231, 219, 213, 207, 171, 135, 99, 93, 54, 234),
        "grad": (51, 45, 81, 141, 177, 213, 207, 201, 205, 203, 197),
        "boot1": (53, 90, 127, 163, 199),
        "boot2": (23, 30, 37, 44, 51),
        "c8": dict(primary="MAGENTA", accent="CYAN", rain="MAGENTA", ok="CYAN", warn="YELLOW", crit="RED"),
    },
}
THEME_ORDER = tuple(THEMES)
DEFAULT_THEME = "cyan-magenta"


def init_colors(theme=DEFAULT_THEME):
    th = THEMES.get(theme) or THEMES[DEFAULT_THEME]
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = 0
    rich = curses.COLORS >= 256
    c8 = {k: getattr(curses, "COLOR_" + v) for k, v in th["c8"].items()}
    P, A8, RN, OK, WARN, CRIT = (c8[k] for k in ("primary", "accent", "rain", "ok", "warn", "crit"))
    W, u = curses.COLOR_WHITE, th["ui"]
    spec = {
        "flash": (u["flash"], W),
        "clock": (u["clock"], P), "date": (u["date"], A8), "border": (u["border"], A8), "label": (u["label"], P),
        "ok": (u["ok"], OK), "warn": (u["warn"], WARN), "crit": (u["crit"], CRIT), "dim": (u["dim"], W),
        "weather": (u["weather"], A8),
    }
    attrs = {}
    for i, (name, (c256, c8_)) in enumerate(spec.items(), 1):
        curses.init_pair(i, c256 if rich else c8_, bg)
        attrs[name] = curses.color_pair(i)
    attrs["clock"] |= curses.A_BOLD
    attrs["flash"] |= curses.A_BOLD
    attrs["date"] |= curses.A_BOLD

    next_pair = [len(spec) + 1]

    def ramp(colours):  # [(256-colour, 8-colour, extra attr)] -> attrs
        out = []
        for c256, c8_, extra in colours:
            curses.init_pair(next_pair[0], c256 if rich else c8_, bg)
            out.append(curses.color_pair(next_pair[0]) | extra)
            next_pair[0] += 1
        return out

    B, D = curses.A_BOLD, curses.A_DIM
    if rich:
        attrs["rain"] = ramp([(c, RN, 0) for c in th["rain"]])
        attrs["grad"] = ramp([(c, OK, 0) for c in th["grad"]])
        attrs["fade_c"] = ramp([(c, P, 0) for c in th["boot1"]])
        attrs["fade_g"] = ramp([(c, OK, 0) for c in th["boot2"]])
    else:
        attrs["rain"] = ramp([(W, W, B), (RN, RN, B), (RN, RN, 0), (RN, RN, D)])
        attrs["grad"] = ramp([(OK, OK, 0), (WARN, WARN, 0), (CRIT, CRIT, 0)])
        attrs["fade_c"] = ramp([(P, P, D), (P, P, 0), (P, P, B)])
        attrs["fade_g"] = ramp([(OK, OK, D), (OK, OK, 0), (OK, OK, B)])
    return attrs


BAR_W, TEXT_W, INNER_W = 20, 11, 38
REFRESH_CHOICES = (5, 10, 15, 30, 60)  # minutes
MENU_ITEMS = ("City", "Units", "Weather refresh", "Air quality", "Theme", "FPS", "Rain", "Scanlines", "Glitch", "Glitch rate", "Boot intro")
TITLE = "▌CYBERDECK//v0.6▐"
MENU = " [Q] quit   [S] settings "


EIGHTHS = " ▏▎▍▌▋▊▉"


class App:
    def __init__(self, scr, cfg, weather, stats, config_file):
        self.scr, self.cfg, self.weather, self.stats = scr, cfg, weather, stats
        self.config_file = config_file
        self.fps = clamp_fps(cfg["fps"])
        self.theme = cfg["theme"] if cfg["theme"] in THEMES else DEFAULT_THEME
        self.modal = None
        self.glitch = {"until": 0.0}  # the current burst
        self.boot_t0 = None  # set in run() while the boot sequence is playing
        self.rain = None
        self.rain_cfg = cfg["rain"]
        self.chars = ASCII if self.rain_cfg["charset"] == "ascii" else KATAKANA

    # -- content ---------------------------------------------------------

    def level(self, pct, invert=False):
        if invert:  # battery: low is bad
            return "crit" if pct <= 15 else "warn" if pct <= 30 else "ok"
        return "crit" if pct >= 90 else "warn" if pct >= 70 else "ok"

    def clock_rows(self, now, scale, glitch, w):
        A = self.A
        fmt = "%H:%M" if self.cfg["clock_24h"] else "%I:%M"
        if self.cfg["show_seconds"]:
            fmt += ":%S"
        text = now.strftime(fmt)
        if not self.cfg["clock_24h"] and text[0] == "0":
            text = "_" + text[1:]
        if now.microsecond >= 500_000:  # blink the colons
            text = text.replace(":", " ")
        if scale:
            rows = render_big(text, scale)
            if glitch and glitch["tear"]:
                rows = glitch_rows(rows)
            dx = min(glitch["split"], (w - len(rows[0]) - 4) // 2) if glitch else 0
            if dx > 0:
                segs = split_rows(rows, dx, A["clock"], A["date"], A["flash"])
            else:
                segs = [[(row, A["clock"])] for row in rows]
            if glitch and glitch["scan"]:
                segs = scan_flicker(segs, A["dim"], A["flash"])
            return segs
        return [[(text.replace("_", "").strip(), A["clock"])]]

    def glitch_effects(self):
        """The clock's effects for this frame, or None. Bursts are timed in seconds, so FPS doesn't matter.

        A burst tears the clock; some bursts also get a brief chromatic split and/or scan-line
        flicker. Roughly one burst in four is a scan-line flicker on its own.
        """
        if not self.cfg["glitch"]["enabled"]:
            return None
        t, burst = time.monotonic(), self.glitch
        if t >= burst["until"]:
            if random.random() >= self.cfg["glitch"]["rate"] / self.fps:
                return None
            scan_only = random.random() < 0.25
            burst = self.glitch = {
                "until": t + (random.uniform(0.1, 0.25) if scan_only else random.uniform(0.2, 0.45)),
                "tear": not scan_only,
                "split_until": t + random.uniform(0.08, 0.2) if not scan_only and random.random() < 0.6 else 0.0,
                "dx": random.choice((1, 1, 2)),  # copies end up 2*dx columns apart
                "scan": scan_only or random.random() < 0.5,
            }
        return {
            "tear": burst["tear"],
            "split": burst["dx"] if t < burst["split_until"] else 0,
            "scan": burst["scan"] and random.random() < 0.4,  # flickers on and off within the burst
        }

    def boot_time(self):
        """Seconds into the boot sequence, or None if it isn't playing."""
        if self.boot_t0 is None:
            return None
        t = time.monotonic() - self.boot_t0
        if t >= BOOT_SECS:
            self.boot_t0 = None
            return None
        return t

    def boot_rows(self, t):
        """The boot text: each line types itself out, and both fade away at the end."""
        out = []
        for text, start, ramp_name in BOOT_LINES:
            ramp = self.A[ramp_name]
            typed = min(len(text), max(0, int((t - start) / BOOT_TYPE * len(text))))
            fade = min(1.0, max(0.0, (t - start) / BOOT_FADE_IN), max(0.0, (BOOT_SECS - t) / BOOT_FADE_OUT))
            attr = ramp[round(fade * (len(ramp) - 1))]
            out.append([(text[:typed].ljust(len(text)), attr)])  # padded, so the layout doesn't jump
        return [out[0], None, out[1]]

    def rule_row(self):
        A = self.A
        return [[("─── ", A["border"]), ("◆", A["clock"]), (" ───", A["border"])]]

    def date_row(self, now):
        s = now.strftime("%A  %d %B %Y").upper()
        if not self.cfg["clock_24h"]:
            s += "  " + now.strftime("%p")
        return [[(s, self.A["date"])]]

    def weather_rows(self, lines):
        A, wx = self.A, self.weather
        d = wx.data
        if not wx.has_location():
            if lines == 1:
                return [[("// NO LOCATION SET - PRESS S //", A["weather"])]]
            return [[("// NO LOCATION SET //", A["weather"])], [("press S to choose your city", A["dim"])]]
        if d is None:
            msg = "// WEATHER LINK OFFLINE //" if wx.error else "// SCANNING ATMOSPHERE //"
            rows = [[(msg, A["weather"])]]
            if wx.error and lines > 1:
                rows.append([(wx.error[:60], A["dim"])])
            return rows
        u = "F" if d["imperial"] else "C"
        speed = "mph" if d["imperial"] else "km/h"
        place = wx.place[0] if wx.place else ""
        stale = "  [OFFLINE]" if wx.error else ""
        main = f"{d['temp']:.0f}°{u}  {d['desc']}  H{d['hi']:.0f}° L{d['lo']:.0f}°"
        if lines == 1:
            return [[(f"{place}  {main}{stale}", A["weather"])]]
        rows = [
            [(f"// {place}{stale} //", A["border"])],
            [(main, A["weather"])],
            [(f"FEELS {d['feels']:.0f}°  HUM {d['hum']:.0f}%  WIND {d['wind']:.0f} {speed}", A["dim"])],
        ]
        if lines >= 4 and self.cfg["air_quality"]:
            rows.append(self.air_quality_row(d))
        return rows

    def air_quality_row(self, d):
        A = self.A
        if d.get("aqi") is None:
            return [("AIR QUALITY  N/A", A["dim"])]
        _, label, colour = aqi_band(d["aqi"])
        segs = [("AIR QUALITY ", A["dim"]), (f"{d['aqi']:.0f} {label}", A[colour])]
        if d.get("pm25") is not None:
            segs.append((f"  PM2.5 {d['pm25']:.0f}", A["dim"]))
        return segs

    def stat_values(self):
        """[(label, fraction, text, level)] for every stat."""
        s = self.stats
        imperial = self.weather.imperial
        out = [("CPU", s.cpu / 100, f"{s.cpu:.0f}%", self.level(s.cpu))]
        used, total = s.mem
        if total:
            out.append(("MEM", used / total, f"{used / 2**30:.1f}/{total / 2**30:.1f}G", self.level(100 * used / total)))
        used, total = s.disk
        if total:
            out.append(("DSK", used / total, f"{used / 2**30:.0f}/{total / 2**30:.0f}G", self.level(100 * used / total)))
        if s.temp is not None:
            shown = s.temp * 9 / 5 + 32 if imperial else s.temp
            out.append(("TMP", (s.temp - 30) / 70, f"{shown:.0f}°{'F' if imperial else 'C'}", self.level((s.temp - 30) / 70 * 100)))
        if s.bat:
            pct, status = s.bat
            tag = {"Charging": " CHG", "Full": " FULL", "Discharging": " DIS"}.get(status, "")
            out.append(("BAT", pct / 100, f"{pct}%{tag}", self.level(pct, invert=True)))
        return out

    def bar_segments(self, frac, invert=False):
        """Bar with eighth-block precision, coloured along a gradient by position (battery runs red -> green)."""
        grad = self.A["grad"][::-1] if invert else self.A["grad"]
        colour = lambda i: grad[i * len(grad) // BAR_W]
        total = max(0.0, min(1.0, frac)) * BAR_W
        whole, part = int(total), int((total % 1) * 8)
        segs = [("█", colour(i)) for i in range(whole)]
        if part and whole < BAR_W:
            segs.append((EIGHTHS[part], colour(whole)))
        used = len(segs)
        if used < BAR_W:
            segs.append(("░" * (BAR_W - used), self.A["dim"]))
        return segs

    def stats_box(self):
        A = self.A
        b = A["border"]
        title = "[ SYS//STATUS ]"
        rows = [[("╔═" + title + "═" * (INNER_W - len(title) - 1) + "╗", b)]]
        for label, frac, text, lvl in self.stat_values():
            rows.append(
                [("║ ", b), (label + " ", A["label"])]
                + self.bar_segments(frac, invert=(label == "BAT"))
                + [(" " + text.rjust(TEXT_W) + " ", A[lvl]), ("║", b)]
            )
        rows.append([("╚" + "═" * INNER_W + "╝", b)])
        return rows

    def stats_line(self):
        A = self.A
        segs = []
        for label, _, text, lvl in self.stat_values():
            segs += [(label + " ", A["label"]), (text + "   ", A[lvl])]
        if segs:
            segs[-1] = (segs[-1][0].rstrip(), segs[-1][1])
        return [segs]

    def build(self, h, w, now):
        """Pick the richest layout that fits the terminal."""
        glitch = self.glitch_effects()
        scale = 0
        for s in (2, 1):
            if len(render_big(now.strftime("%H:%M:%S" if self.cfg["show_seconds"] else "%H:%M"), s)[0]) + 4 <= w:
                scale = s
                break
        box_ok = w >= INNER_W + 4
        candidates = [(0, 1, False, False)]
        if scale:
            full = 4 if self.cfg["air_quality"] else 3  # weather lines
            candidates = [(scale, full, box_ok, True), (scale, full, False, True), (scale, 3, False, True),
                          (scale, 1, False, False)] + candidates
        for sc, wlines, box, rule in candidates:  # rule: the underline below the date
            sections = [
                self.clock_rows(now, sc, glitch, w),
                self.date_row(now) + (self.rule_row() if rule else []),
                self.weather_rows(wlines),
                self.stats_box() if box else self.stats_line(),
            ]
            gap = 1 if sc or h > 8 else 0
            rows = []
            for i, sec in enumerate(sections):
                if i:
                    rows += [None] * gap
                rows += sec
            if len(rows) <= h:
                return rows
        return rows[:h]

    # -- frame -----------------------------------------------------------

    def frame(self, dt=0.0):
        scr, A = self.scr, self.A
        scr.erase()
        h, w = scr.getmaxyx()
        boot_t = self.boot_time()
        if boot_t is not None:
            rows = self.boot_rows(boot_t)
            top = max(0, (h - len(rows)) // 2)
            menu = False
        elif self.modal:
            rows = self.modal_rows(w)
            top = max(0, (h - len(rows)) // 2)
            menu = False
        else:
            rows = self.build(h, w, datetime.now())
            top = max(0, (h - len(rows)) // 2)
            menu = top + len(rows) < h  # room for the bottom menu
        header = self.header(w) if top > 0 and boot_t is None else []  # only when row 0 is free
        if self.rain_cfg["enabled"]:
            if not self.rain or (self.rain.h, self.rain.w) != (h, w):
                self.rain = Rain(h, w, self.chars, self.rain_cfg["speed"])
            self.rain.step(dt)
            rects = self.row_rects(rows, top, w)
            if menu:
                rects.append((h - 1, 1, 1 + len(MENU)))
            rects += [(0, x, x + len(t)) for x, t in header]
            fade = 0 if boot_t is None else round(6 * max(0.0, 1 - boot_t / BOOT_RAIN_FADE))  # rain fades in
            self.rain.draw(scr, A["rain"], self.halo_mask(rects, h, w), self.rain_cfg["scanlines"], fade)
        self.draw_rows(rows, top, w)
        for (x, text), attr in zip(header, (A["date"], A["label"])):
            put(scr, 0, x, text, attr)
        if menu:
            put(scr, h - 1, 1, MENU, A["dim"])
        scr.refresh()

    def header(self, w):
        """[(x, text)] for the top-left title and top-right host//uptime, if they fit."""
        out = [(1, TITLE)]
        right = f"{self.stats.host}//{self.stats.uptime}"
        if 1 + len(TITLE) + 2 + len(right) + 1 <= w:
            out.append((w - 1 - len(right), right))
        return out

    def row_rects(self, rows, top, w):
        """[(y, x0, x1)] for every drawn row."""
        rects = []
        for i, segs in enumerate(rows):
            if segs is not None:
                width = sum(len(t) for t, _ in segs)
                x = (w - width) // 2
                rects.append((top + i, x, x + width))
        return rects

    def halo_mask(self, rects, h, w):
        """Per-cell rain level: 0 full, 1-2 progressively fainter near text, 3 hidden behind it."""
        mask = bytearray(h * w)

        def paint(y, x0, x1, level):
            x0, x1 = max(0, x0), min(w, x1)
            if 0 <= y < h and x0 < x1:
                mask[y * w + x0 : y * w + x1] = bytes([level]) * (x1 - x0)

        # Outer halos first so the inner (stronger) ones overwrite them.
        for y, x0, x1 in rects:
            for dy in (-1, 0, 1):
                paint(y + dy, x0 - 8, x1 + 8, 1)
        for y, x0, x1 in rects:
            paint(y, x0 - 5, x1 + 5, 2)
            paint(y - 1, x0 - 3, x1 + 3, 2)
            paint(y + 1, x0 - 3, x1 + 3, 2)
        for y, x0, x1 in rects:
            paint(y, x0 - 2, x1 + 2, HALO_HIDDEN)
        return mask

    def draw_rows(self, rows, top, w):
        for i, segs in enumerate(rows):
            if segs is None:
                continue
            width = sum(len(t) for t, _ in segs)
            x = (w - width) // 2
            put(self.scr, top + i, max(0, x - 2), " " * (width + 4))  # matches the hidden zone in halo_mask
            for text, attr in segs:
                put(self.scr, top + i, x, text, attr)
                x += len(text)

    # -- settings menu / city prompt ---------------------------------------

    def open_menu(self, sel=0):
        self.modal = {"kind": "menu", "sel": sel}

    def open_city_prompt(self, first_run=False, back=False):
        title = "[ INITIALISE // LOCATION ]" if first_run else "[ SETTINGS // CITY ]"
        self.modal = {
            "kind": "city", "title": title, "back": back, "error": None, "busy": False,
            "text": self.cfg["location"].get("city") or "",
        }

    def menu_values(self):
        g = self.cfg["glitch"]
        onoff = lambda on: "ON" if on else "OFF"
        return [
            (self.cfg["location"].get("city") or "NOT SET").upper(),
            "IMPERIAL" if self.weather.imperial else "METRIC",
            f"◀ {self.cfg['weather_refresh_minutes']} min ▶",
            onoff(self.cfg["air_quality"]),
            f"◀ {THEMES[self.theme]['name']} ▶",
            f"◀ {self.fps} ▶",
            onoff(self.rain_cfg["enabled"]),
            onoff(self.rain_cfg["scanlines"]),
            onoff(g["enabled"]),
            f"◀ {g['rate']:.2f}/s ▶",
            onoff(self.cfg["boot_sequence"]),
        ]

    def change_setting(self, idx, d):
        """d is -1 / +1 for left / right, 0 for Enter. Every change applies live and is saved."""
        name, g = MENU_ITEMS[idx], self.cfg["glitch"]
        if name == "City":
            if d == 0:
                self.open_city_prompt(back=True)
        elif name == "Units":
            self.weather.toggle_units()  # also re-fetches the weather in the new units
            self.cfg["units"] = "imperial" if self.weather.imperial else "metric"
            save_config(self.config_file, {"units": self.cfg["units"]})
        elif name == "Weather refresh":
            cur = self.cfg["weather_refresh_minutes"]
            if d >= 0:  # Enter cycles upward and wraps
                mins = next((m for m in REFRESH_CHOICES if m > cur), REFRESH_CHOICES[0])
            else:
                mins = next((m for m in reversed(REFRESH_CHOICES) if m < cur), REFRESH_CHOICES[-1])
            self.cfg["weather_refresh_minutes"] = mins
            self.weather.refresh = mins * 60
            self.weather.wake.set()  # re-fetch now so the new interval starts from here
            save_config(self.config_file, {"weather_refresh_minutes": mins})
        elif name == "Air quality":
            self.cfg["air_quality"] = self.weather.air_quality = not self.cfg["air_quality"]
            self.weather.wake.set()  # fetch (or stop fetching) right away
            save_config(self.config_file, {"air_quality": self.cfg["air_quality"]})
        elif name == "Theme":
            i = THEME_ORDER.index(self.theme)
            self.theme = THEME_ORDER[(i + (-1 if d < 0 else 1)) % len(THEME_ORDER)]
            self.A = init_colors(self.theme)
            self.cfg["theme"] = self.theme
            save_config(self.config_file, {"theme": self.theme})
        elif name == "FPS":
            self.fps = clamp_fps(self.fps + (d or 1) * FPS_STEP)
            self.cfg["fps"] = self.fps
            save_config(self.config_file, {"fps": self.fps})
        elif name == "Rain":
            self.rain_cfg["enabled"] = not self.rain_cfg["enabled"]
            save_config(self.config_file, {"rain": {"enabled": self.rain_cfg["enabled"]}})
        elif name == "Scanlines":
            self.rain_cfg["scanlines"] = not self.rain_cfg["scanlines"]
            save_config(self.config_file, {"rain": {"scanlines": self.rain_cfg["scanlines"]}})
        elif name == "Glitch":
            g["enabled"] = not g["enabled"]
            save_config(self.config_file, {"glitch": {"enabled": g["enabled"]}})
        elif name == "Boot intro":
            self.cfg["boot_sequence"] = not self.cfg["boot_sequence"]
            save_config(self.config_file, {"boot_sequence": self.cfg["boot_sequence"]})
        elif name == "Glitch rate":
            g["rate"] = round(max(GLITCH_MIN, min(GLITCH_MAX, g["rate"] + (d or 1) * GLITCH_STEP)), 2)
            save_config(self.config_file, {"glitch": {"rate": g["rate"]}})

    def modal_rows(self, w):
        A, m = self.A, self.modal
        b = A["border"]
        inner = max(30, min(w - 2, 46))

        def line(text, attr):
            return [("║", b), (text.ljust(inner)[:inner], attr), ("║", b)]

        def top(title):
            return [("╔═" + title + "═" * (inner - len(title) - 1) + "╗", b)]

        bottom = [("╚" + "═" * inner + "╝", b)]

        if m["kind"] == "menu":
            rows = [top("[ SETTINGS ]")]
            for i, (name, value) in enumerate(zip(MENU_ITEMS, self.menu_values())):
                sel = i == m["sel"]
                text = f" {'▶' if sel else ' '} {name:<16}" + value[: inner - 20].rjust(inner - 19)
                rows.append(line(text, A["clock"] if sel else A["label"]))
            rows.append(line(" ↑↓ select · ←→/ENTER change · ESC close", A["dim"]))
            return rows + [bottom]

        cursor = "█" if int(time.time() * 2) % 2 == 0 and not m["busy"] else " "
        typed = m["text"][-(inner - 6):]
        if m["busy"]:
            status = ("SCANNING GRID...", A["weather"])
        elif m["error"]:
            status = (m["error"][: inner - 2].upper(), A["crit"])
        else:
            status = ("ENTER confirm · ESC cancel", A["dim"])
        return [
            top(m["title"]),
            line(" ENTER CITY  (e.g. Paris, FR)", A["label"]),
            line(" > " + typed + cursor, A["clock"]),
            line(" " + status[0], status[1]),
            bottom,
        ]

    def read_key(self):
        try:
            return self.scr.get_wch()
        except curses.error:  # timeout, no input
            return None

    def modal_key(self, key):
        m = self.modal
        enter = key in ("\n", "\r") or key == curses.KEY_ENTER
        if m["kind"] == "menu":
            if key == "\x1b":
                self.modal = None
            elif key == curses.KEY_UP:
                m["sel"] = (m["sel"] - 1) % len(MENU_ITEMS)
            elif key in (curses.KEY_DOWN, "\t"):
                m["sel"] = (m["sel"] + 1) % len(MENU_ITEMS)
            elif key in (curses.KEY_LEFT, "-", "_"):
                self.change_setting(m["sel"], -1)
            elif key in (curses.KEY_RIGHT, "+", "="):
                self.change_setting(m["sel"], 1)
            elif enter:
                self.change_setting(m["sel"], 0)
        elif key == "\x1b":
            if m["back"]:
                self.open_menu()
            else:
                self.modal = None
        elif enter:
            self.submit_city()
        elif key in ("\x7f", "\b") or key == curses.KEY_BACKSPACE:
            m["text"], m["error"] = m["text"][:-1], None
        elif key == "\x15":  # ctrl-u
            m["text"], m["error"] = "", None
        elif isinstance(key, str) and key.isprintable() and len(m["text"]) < 60:
            m["text"], m["error"] = m["text"] + key, None

    def submit_city(self):
        m = self.modal
        query = m["text"].strip()
        if not query:
            m["error"] = "enter a city name"
            return
        m["busy"] = True
        self.frame()  # show SCANNING before the (blocking) lookup
        try:
            place = geocode(query)
        except Exception as err:
            m["busy"], m["error"] = False, str(err) or type(err).__name__
            return
        loc = {"city": query, "lat": None, "lon": None}
        self.cfg["location"] = loc
        save_config(self.config_file, {"location": loc})
        self.weather.set_location(loc, place)
        self.modal = None

    def run(self):
        curses.curs_set(0)
        self.A = init_colors(self.theme)
        if not self.weather.has_location():
            self.open_city_prompt(first_run=True)
        last_stats, last_frame = 0.0, time.monotonic()
        if self.cfg["boot_sequence"]:
            self.boot_t0 = last_frame
        while True:
            # Sleep only until the next frame is due; a keypress wakes us early.
            wait = last_frame + 1.0 / self.fps - time.monotonic()
            self.scr.timeout(max(0, int(wait * 1000)))
            key = self.read_key()
            if key == curses.KEY_RESIZE:
                self.rain = None
            elif self.boot_t0 is not None:
                if key is not None:  # any key skips the intro
                    self.boot_t0 = None
            elif self.modal:
                if key is not None:
                    self.modal_key(key)
            elif key in ("q", "Q", "\x1b"):
                return
            elif key in ("s", "S"):
                self.open_menu()
            now = time.monotonic()
            if key is None and now - last_frame < 0.9 / self.fps:
                continue
            if now - last_stats >= 1.0:
                self.stats.sample()
                last_stats = now
            self.frame(min(now - last_frame, 0.25))  # dt drives the rain, so speed is FPS-independent
            last_frame = now


def main():
    ap = argparse.ArgumentParser(description="Cyberpunk terminal clock, weather and system monitor.")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--city", help="weather location by name")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--fps", type=int, help=f"frames per second ({FPS_MIN}-{FPS_MAX})")
    ap.add_argument("--theme", choices=THEME_ORDER, help="colour theme")
    ap.add_argument("--units", choices=("metric", "imperial"))
    ap.add_argument("--12h", dest="h12", action="store_true", help="12-hour clock")
    ap.add_argument("--seconds", action="store_true", help="show seconds")
    ap.add_argument("--ascii", action="store_true", help="ASCII rain instead of katakana")
    ap.add_argument("--no-rain", action="store_true")
    ap.add_argument("--no-scanlines", action="store_true", help="no CRT scanlines in the rain")
    ap.add_argument("--no-air-quality", action="store_true", help="hide air quality")
    ap.add_argument("--no-boot", action="store_true", help="skip the boot sequence")
    ap.add_argument("--no-glitch", action="store_true", help="disable the clock glitch")
    args = ap.parse_args()

    config_file = config_path(args.config)
    cfg = load_config(config_file)
    if args.city:
        cfg["location"] = {"city": args.city, "lat": None, "lon": None}
    if args.lat is not None and args.lon is not None:
        cfg["location"] = {**cfg["location"], "lat": args.lat, "lon": args.lon}
    if args.fps:
        cfg["fps"] = args.fps
    if args.theme:
        cfg["theme"] = args.theme
    if args.units:
        cfg["units"] = args.units
    if args.h12:
        cfg["clock_24h"] = False
    if args.seconds:
        cfg["show_seconds"] = True
    if args.ascii:
        cfg["rain"]["charset"] = "ascii"
    if args.no_air_quality:
        cfg["air_quality"] = False
    if args.no_boot:
        cfg["boot_sequence"] = False
    if args.no_glitch:
        cfg["glitch"]["enabled"] = False
    if args.no_scanlines:
        cfg["rain"]["scanlines"] = False
    if args.no_rain:
        cfg["rain"]["enabled"] = False

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    weather = Weather(cfg)
    if weather.has_location():
        weather.start()  # otherwise it starts once the city prompt is answered
    stats = Stats(cfg["disk_path"])
    try:
        curses.wrapper(lambda scr: App(scr, cfg, weather, stats, config_file).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
