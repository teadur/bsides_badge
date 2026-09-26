"""Broadcast ESP-NOW bring-up shared by the badge-to-badge Wi-Fi features."""

import gc
import time

import linklog

try:
    import network
    import espnow
except ImportError:
    network = None
    espnow = None

try:
    import esp32
except ImportError:
    esp32 = None


BROADCAST = b"\xff\xff\xff\xff\xff\xff"

# ESP-NOW only reaches badges on the same Wi-Fi channel, and no badge joins an
# access point here, so nothing else would make the radios agree on one.
CHANNEL = 1

# MicroPython retries a send for up to 2 s while the Wi-Fi driver has no
# memory for it, and nothing else on the badge runs meanwhile (a starved badge
# stopped answering on USB). After a failed send, skip sends for this long.
SEND_BACKOFF_MS = 5000


def mac_hex(mac):
    if not mac:
        return "?"
    return ":".join("%02X" % b for b in mac)


def memory():
    """Python heap free, plus the Wi-Fi driver's (IDF) free and largest block."""
    text = "mp free %d" % gc.mem_free()
    if esp32 is not None:
        regions = esp32.idf_heap_info(esp32.HEAP_DATA)
        text += " idf free %d largest %d" % (
            sum(r[1] for r in regions), max(r[2] for r in regions))
    return text


def reserve_driver():
    """Start and stop the Wi-Fi driver once, before the UI loads.

    The driver allocates its buffers outside the Python heap and keeps them
    after active(False), while the Python heap grows into that same memory
    as the UI and games load. A badge that first starts Wi-Fi in a game can
    find too little left (seen: 35 KB free, 18 KB largest block)."""
    if network is None:
        return
    try:
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.active(False)
    except Exception as exc:
        linklog.log("boot", "wifi reserve failed:", repr(exc), memory())
        linklog.flush()
        return
    gc.collect()
    linklog.log("boot", "wifi reserved,", memory())


def open_link(owner, device_id):
    """Return (ESPNow, own MAC) on success, or (None, None).

    A failure is logged rather than raised: callers fall back to whatever
    else they have (the cable) or show the reason from the log."""
    if not linklog.identity:
        linklog.identity = device_id
    if espnow is None:
        linklog.log(owner, "espnow module unavailable")
        return None, None
    # The Wi-Fi driver allocates outside the Python heap; collecting first
    # lets the heap hand back empty areas before the driver needs them.
    gc.collect()
    linklog.log(owner, "espnow starting,", memory())
    wlan = None
    try:
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        try:
            wlan.disconnect()
        except OSError:
            pass
        wlan.config(channel=CHANNEL)
        esp = espnow.ESPNow()
        esp.active(True)
        try:
            esp.add_peer(BROADCAST)
        except OSError as exc:
            # Already registered when a previous user did not shut down.
            linklog.log(owner, "add_peer:", repr(exc))
        mac = wlan.config("mac")
    except Exception as exc:
        linklog.log(owner, "espnow init failed:", repr(exc), memory())
        if wlan is not None:
            try:
                wlan.active(False)    # release a half-started driver
            except Exception:
                pass
        linklog.flush()
        return None, None
    linklog.log(owner, "espnow up ch", CHANNEL, "mac", mac_hex(mac))
    linklog.log(owner, "after start,", memory())
    return esp, mac


class Broadcaster:
    """Broadcasts over ESP-NOW, pausing after a failed send."""

    def __init__(self, esp, owner):
        self.esp = esp
        self.owner = owner
        self.failures = 0
        self.paused_until = None

    def send(self, msg):
        """Broadcast msg; False if it was skipped or failed."""
        if self.paused_until is not None:
            if time.ticks_diff(self.paused_until, time.ticks_ms()) > 0:
                return False
            self.paused_until = None
        try:
            self.esp.send(BROADCAST, msg, False)
        except OSError as exc:
            self.failures += 1
            self.paused_until = time.ticks_add(time.ticks_ms(), SEND_BACKOFF_MS)
            linklog.log(self.owner, "espnow send failed:", repr(exc),
                        "- pausing sends for", SEND_BACKOFF_MS, "ms,", memory())
            return False
        if self.failures:
            linklog.log(self.owner, "espnow sending again after",
                        self.failures, "failed sends")
            self.failures = 0
        return True


def close_link(esp, owner):
    if esp is not None:
        try:
            esp.active(False)
            network.WLAN(network.STA_IF).active(False)
        except Exception as exc:
            linklog.log(owner, "espnow close failed:", repr(exc))
        linklog.log(owner, "espnow down")
    linklog.flush()
