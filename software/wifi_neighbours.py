"""Utils -> WiFi neighbours: badges heard over ESP-NOW, plus the link log.

The screen beacons its own ID and name once a second on the same broadcast
channel tic-tac-toe uses, and lists every sender it hears: other badges on
this screen, and badges in tic-tac-toe (their hellos carry the device ID).
SELECT switches to the debug console, which shows the shared link log,
including anything tic-tac-toe logged earlier in this boot.
"""

import time
import uasyncio as asyncio

import bsides
import espnow_link
import linklog


TICK_MS = 50
REDRAW_MS = 200           # a full OLED push is ~1 KB of I2C traffic
BEACON_MS = 1000
STALE_MS = 10000          # a neighbour silent this long is logged as gone
COLS = 16                 # 8x8 framebuffer font on a 128 px display
ROWS = 8
LIST_ROWS = ROWS - 2      # header and footer
LOG_ROWS = ROWS - 1       # header

BEACON_TAG = b"BN"


def beacon(device_id, name):
    return BEACON_TAG + ("%s,%s" % (device_id, name[:16])).encode()


def describe(msg):
    """Return (device_id, name, kind) for a received frame; unknowns are None."""
    try:
        if msg.startswith(BEACON_TAG):
            parts = msg[2:].decode().split(",", 1)
            return parts[0], parts[1] if len(parts) > 1 else "", "beacon"
        if msg.startswith(b"TH"):
            # tic-tac-toe hello: TH<id>,<seeking>,<game>*<checksum>
            return msg[2:].split(b",", 1)[0].decode(), None, "ttt hello"
        if msg.startswith(b"T") and len(msg) > 1:
            return None, None, "ttt " + chr(msg[1])
    except UnicodeError:
        pass
    return None, None, "unknown"


def wrap(lines, width):
    out = []
    for line in lines:
        while len(line) > width:
            out.append(line[:width])
            line = " " + line[width:]
        out.append(line)
    return out


def age_text(ms):
    s = ms // 1000
    if s < 100:
        return "%ds" % s
    if s < 6000:
        return "%dm" % (s // 60)
    return ">1h"


class Neighbour:
    def __init__(self, mac):
        self.mac = mac
        self.device_id = None
        self.name = None
        self.rssi = None
        self.last_seen = 0
        self.count = 0
        self.kind = ""
        self.stale = False

    def label(self):
        if self.device_id:
            return self.device_id[-8:]
        return "m" + "".join("%02X" % b for b in self.mac[-3:])


class WifiNeighboursScreen:
    """
    Controls:
      SELECT    -> switch between neighbour list and debug console
      NEXT/PREV -> scroll; the console follows new lines when at the bottom
      BACK      -> stop the radio and return to Utils
    """
    manages_own_render = True

    def __init__(self, oled):
        self.oled = oled
        self.my_id = bsides.device_id
        self.name = bsides.USERNAME or ""
        self.view = "list"
        self.neighbours = {}
        self.list_offset = 0
        self.log_offset = None       # None follows the newest line
        self.sent = 0
        self._drawn = None
        self._last_render = 0

        self.esp, self.mac = espnow_link.open_link("nbr", self.my_id)
        self.radio = (espnow_link.Broadcaster(self.esp, "nbr")
                      if self.esp is not None else None)
        if self.esp is not None:
            linklog.log("nbr", "beaconing as", self.my_id, repr(self.name),
                        "every", BEACON_MS, "ms")
        now = time.ticks_ms()
        self.last_beacon = time.ticks_add(now, -BEACON_MS)
        self.running = True
        self._task = asyncio.create_task(self._loop())
        self.render()

    # ---------- radio ----------
    def _poll(self, now):
        while True:
            try:
                mac, msg = self.esp.recv(0)
            except OSError as exc:
                linklog.log("nbr", "recv failed:", repr(exc))
                return
            if msg is None:
                return
            self._on_frame(bytes(mac), bytes(msg), now)

    def _rssi(self, mac):
        table = getattr(self.esp, "peers_table", None)
        if not table or mac not in table:
            return None
        return table[mac][0]

    def _on_frame(self, mac, msg, now):
        device_id, name, kind = describe(msg)
        n = self.neighbours.get(mac)
        if n is None:
            n = self.neighbours[mac] = Neighbour(mac)
            linklog.log("nbr", "new", espnow_link.mac_hex(mac),
                        device_id or "?", kind)
        elif n.stale:
            linklog.log("nbr", "back", n.label())
        n.stale = False
        n.last_seen = now
        n.count += 1
        n.kind = kind
        n.rssi = self._rssi(mac)
        if device_id:
            n.device_id = device_id
        if name is not None:
            n.name = name
        linklog.log("nbr", "rx", n.label(), n.rssi, msg)

    def _expire(self, now):
        for n in self.neighbours.values():
            if not n.stale and time.ticks_diff(now, n.last_seen) >= STALE_MS:
                n.stale = True
                linklog.log("nbr", "gone", n.label(), "after", n.count, "frames")

    def _beacon(self, now):
        if time.ticks_diff(now, self.last_beacon) < BEACON_MS:
            return
        self.last_beacon = now
        if self.radio.send(beacon(self.my_id, self.name)):
            self.sent += 1

    def _tick(self, now):
        if self.esp is not None:
            self._poll(now)
            self._beacon(now)
            self._expire(now)
        if time.ticks_diff(now, self._last_render) < REDRAW_MS:
            return
        # Ages tick every second even when nothing new arrives.
        key = (self.view, self.list_offset, self.log_offset, linklog.revision,
               len(self.neighbours), now // 1000)
        if key != self._drawn:
            self.render()

    async def _loop(self):
        try:
            while self.running:
                self._tick(time.ticks_ms())
                await asyncio.sleep_ms(TICK_MS)
        except asyncio.CancelledError:
            return

    # ---------- drawing ----------
    def _sorted(self):
        return sorted(self.neighbours.values(),
                      key=lambda n: (n.stale, -n.last_seen))

    def _log_rows(self):
        # Whole seconds on screen; the log file keeps milliseconds.
        return wrap([line.split(".", 1)[0] + line[line.find(" "):]
                     for line in linklog.lines], COLS)

    def _render_list(self, now):
        o = self.oled
        live = sum(1 for n in self.neighbours.values() if not n.stale)
        if self.esp is None:
            o.text("Radio failed", 0, 0, 1)
            o.text("SELECT: see log", 0, 16, 1)
            return
        o.text("Nbrs %d ch%d" % (live, espnow_link.CHANNEL), 0, 0, 1)
        rows = self._sorted()
        if not rows:
            o.text("Listening...", 0, 16, 1)
            o.text("tx %d beacons" % self.sent, 0, 32, 1)
        for i, n in enumerate(rows[self.list_offset:self.list_offset + LIST_ROWS]):
            rssi = "%d" % n.rssi if n.rssi is not None else "?"
            age = age_text(time.ticks_diff(now, n.last_seen))
            o.text("%-8s%4s%4s" % (n.label(), rssi, age), 0, 8 + i * 8, 1)
        o.text("SEL=log BK=exit", 0, 56, 1)

    def _render_log(self):
        o = self.oled
        rows = self._log_rows()
        top = self._log_top(rows)
        o.text("Log %d/%d" % (min(top + LOG_ROWS, len(rows)), len(rows)),
               0, 0, 1)
        for i, line in enumerate(rows[top:top + LOG_ROWS]):
            o.text(line, 0, 8 + i * 8, 1)

    def _log_top(self, rows):
        last = max(0, len(rows) - LOG_ROWS)
        if self.log_offset is None:
            return last
        return min(self.log_offset, last)

    def render(self):
        now = time.ticks_ms()
        self._last_render = now
        self._drawn = (self.view, self.list_offset, self.log_offset,
                       linklog.revision, len(self.neighbours), now // 1000)
        self.oled.fill(0)
        if self.view == "list":
            self._render_list(now)
        else:
            self._render_log()
        self.oled.show()

    # ---------- input ----------
    def _scroll(self, step):
        if self.view == "list":
            last = max(0, len(self.neighbours) - LIST_ROWS)
            self.list_offset = max(0, min(last, self.list_offset + step))
            return
        rows = self._log_rows()
        last = max(0, len(rows) - LOG_ROWS)
        top = max(0, min(last, self._log_top(rows) + step))
        self.log_offset = None if top == last else top

    async def handle_button(self, btn):
        if btn == bsides.BTN_BACK:
            self.running = False
            self._task.cancel()
            await asyncio.sleep_ms(0)
            linklog.log("nbr", "closing,", len(self.neighbours), "seen,",
                        self.sent, "beacons sent")
            espnow_link.close_link(self.esp, "nbr")
            return bsides.UtilsScreen(self.oled)
        if btn == bsides.BTN_SELECT:
            self.view = "log" if self.view == "list" else "list"
        elif btn == bsides.BTN_NEXT:
            self._scroll(1)
        elif btn == bsides.BTN_PREV:
            self._scroll(-1)
        self.render()
        return self
