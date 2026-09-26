import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4
MY_ID, OTHER_ID = "A1B2C3D4E5F6", "0011223344AA"
OTHER_MAC = b"\x02\x11\x22\x33\x44\x55"


class _Clock:
    now = 100000

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_add(a, b):
        return a + b


class _Task:
    def cancel(self):
        pass


class RecordingOled:
    width = 128
    height = 64

    def __init__(self):
        self.rows = {}
        self.frames = 0

    def fill(self, _color):
        self.rows = {}

    def text(self, text, x, y, _color):
        assert len(text) <= 16 and x == 0, (text, x)
        self.rows[y // 8] = text

    def show(self):
        self.frames += 1

    def screen(self):
        return [self.rows.get(i, "") for i in range(8)]


class Air:
    """Broadcast medium shared by every FakeEspNow in a test."""

    def __init__(self):
        self.radios = []


class FakeEspNow:
    air = None
    fail_init = False

    def __init__(self):
        if FakeEspNow.fail_init:
            raise OSError("ESP_ERR_WIFI_NOT_INIT")
        self.active_ = False
        self.inbox = []
        self.sent = []
        self.peers_table = {}
        self.mac = b"\x02\x00\x00\x00\x00\x01"
        FakeEspNow.air.radios.append(self)

    def active(self, value=None):
        if value is None:
            return self.active_
        self.active_ = value

    def add_peer(self, _mac):
        pass

    def send(self, _mac, msg):
        self.sent.append(bytes(msg))
        for radio in FakeEspNow.air.radios:
            if radio is not self and radio.active_ and self.active_:
                radio.deliver(self.mac, bytes(msg), rssi=-55)

    def deliver(self, mac, msg, rssi):
        self.inbox.append((mac, msg))
        self.peers_table[mac] = [rssi, _Clock.now]

    def recv(self, _timeout_ms=0):
        if self.inbox:
            return self.inbox.pop(0)
        return (None, None)


class FakeWLAN:
    def __init__(self, *_args):
        pass

    def active(self, _value=None):
        return True

    def disconnect(self):
        pass

    def config(self, *args, **kwargs):
        if args == ("mac",):
            return b"\x02\x00\x00\x00\x00\x01"
        return None


async def _sleep_ms(_ms):
    return None


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "software" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bsides = types.ModuleType("bsides")
bsides.BTN_NEXT, bsides.BTN_PREV = BTN_NEXT, BTN_PREV
bsides.BTN_SELECT, bsides.BTN_BACK = BTN_SELECT, BTN_BACK
bsides.device_id = MY_ID
bsides.USERNAME = "Ada, Lovelace"
bsides.UtilsScreen = lambda oled: "utils"
uasyncio = types.SimpleNamespace(
    create_task=lambda coro: (coro.close(), _Task())[1],
    sleep_ms=_sleep_ms, CancelledError=Exception)
time_stub = types.SimpleNamespace(ticks_ms=_Clock.ticks_ms,
                                  ticks_diff=_Clock.ticks_diff,
                                  ticks_add=_Clock.ticks_add)
network = types.SimpleNamespace(WLAN=FakeWLAN, STA_IF=0)
espnow = types.SimpleNamespace(ESPNow=FakeEspNow)

with patch.dict(sys.modules, {"bsides": bsides, "uasyncio": uasyncio,
                              "time": time_stub, "network": network,
                              "espnow": espnow}):
    linklog = _load("linklog", "linklog.py")
    espnow_link = _load("espnow_link", "espnow_link.py")
    nbr = _load("wifi_neighbours_test", "wifi_neighbours.py")

LOG_DIR = tempfile.mkdtemp(prefix="nbr-linklog-")
linklog.LOG_PATH = str(Path(LOG_DIR) / "linklog.txt")
linklog.OLD_PATH = str(Path(LOG_DIR) / "linklog.old.txt")
linklog.print = lambda *args, **kwargs: None
espnow_link.gc = types.SimpleNamespace(collect=lambda: None,
                                       mem_free=lambda: 123456)


def press(screen, btn):
    return asyncio.run(screen.handle_button(btn))


def run(screens, ms):
    for _ in range(ms // nbr.TICK_MS):
        _Clock.now += nbr.TICK_MS
        for s in screens:
            s._tick(_Clock.now)


class HelperTests(unittest.TestCase):
    def test_beacon_roundtrip_keeps_commas_in_the_name(self):
        msg = nbr.beacon(MY_ID, "Ada, Lovelace")
        self.assertEqual(nbr.describe(msg), (MY_ID, "Ada, Lovelace", "beacon"))

    def test_describes_tictactoe_traffic(self):
        self.assertEqual(nbr.describe(b"THFFFF00000001,1,0*3A"),
                         ("FFFF00000001", None, "ttt hello"))
        self.assertEqual(nbr.describe(b"TS12,-X-------,O,4*00"),
                         (None, None, "ttt S"))
        self.assertEqual(nbr.describe(b"\xff\x00junk"), (None, None, "unknown"))
        self.assertEqual(nbr.describe(b"BN\xff\xfe"), (None, None, "unknown"))

    def test_wrap_fits_the_display_and_marks_continuations(self):
        rows = nbr.wrap(["0123456789abcdefXYZ", "short"], 16)
        self.assertEqual(rows, ["0123456789abcdef", " XYZ", "short"])
        long_rows = nbr.wrap(["x" * 50], 16)
        self.assertTrue(all(len(r) <= 16 for r in long_rows))
        self.assertEqual("".join(r.lstrip() for r in long_rows), "x" * 50)

    def test_age_text(self):
        self.assertEqual(nbr.age_text(4999), "4s")
        self.assertEqual(nbr.age_text(150000), "2m")
        self.assertEqual(nbr.age_text(7200000), ">1h")


class ReserveDriverTests(unittest.TestCase):
    def test_starts_and_stops_the_driver_once(self):
        calls = []

        class RecordingWLAN(FakeWLAN):
            def active(self, value=None):
                calls.append(value)

        net = types.SimpleNamespace(WLAN=RecordingWLAN, STA_IF=0)
        with patch.object(espnow_link, "network", net):
            espnow_link.reserve_driver()
        self.assertEqual(calls, [True, False])

    def test_failure_is_logged_not_raised(self):
        class FailingWLAN(FakeWLAN):
            def active(self, value=None):
                raise OSError("WiFi Out of Memory")

        net = types.SimpleNamespace(WLAN=FailingWLAN, STA_IF=0)
        with patch.object(espnow_link, "network", net):
            espnow_link.reserve_driver()
        self.assertIn("boot wifi reserve failed: OSError('WiFi Out of Memory'",
                      linklog.lines[-1])


class NeighbourScreenTests(unittest.TestCase):
    def setUp(self):
        _Clock.now = 100000
        FakeEspNow.air = Air()
        FakeEspNow.fail_init = False
        self.oled = RecordingOled()
        self.screen = nbr.WifiNeighboursScreen(self.oled)

    def other_badge(self, msg, rssi=-67):
        self.screen.esp.deliver(OTHER_MAC, msg, rssi)

    def test_beacons_once_a_second_with_id_and_name(self):
        run([self.screen], 3000)
        sent = self.screen.esp.sent
        self.assertIn(len(sent), (3, 4))
        self.assertEqual(nbr.describe(sent[0]), (MY_ID, "Ada, Lovelace", "beacon"))

    def test_lists_a_badge_heard_in_tictactoe_with_rssi_and_age(self):
        self.other_badge(b"TH" + OTHER_ID.encode() + b",1,0*00")
        run([self.screen], 3000)       # heard at +50 ms, redrawn each second
        self.assertEqual(self.oled.screen()[0], "Nbrs 1 ch1")
        self.assertEqual(self.oled.screen()[1], "223344AA -67  2s")
        n = self.screen.neighbours[OTHER_MAC]
        self.assertEqual((n.device_id, n.kind, n.count), (OTHER_ID, "ttt hello", 1))

    def test_unknown_sender_is_labelled_by_mac(self):
        self.other_badge(b"hello?")
        run([self.screen], nbr.REDRAW_MS + 100)
        self.assertEqual(self.oled.screen()[1][:8], "m334455 ")

    def test_two_badges_on_this_screen_find_each_other(self):
        other_oled = RecordingOled()
        bsides.device_id = OTHER_ID
        try:
            other = nbr.WifiNeighboursScreen(other_oled)
        finally:
            bsides.device_id = MY_ID
        other.esp.mac = OTHER_MAC
        run([self.screen, other], 1500)
        self.assertEqual(self.screen.neighbours[OTHER_MAC].device_id, OTHER_ID)
        self.assertEqual(self.screen.neighbours[OTHER_MAC].name, "Ada, Lovelace")
        mine = other.neighbours[b"\x02\x00\x00\x00\x00\x01"]
        self.assertEqual(mine.device_id, MY_ID)
        self.assertEqual(mine.rssi, -55)

    def test_silent_neighbour_is_logged_gone_and_back(self):
        self.other_badge(nbr.beacon(OTHER_ID, "Bob"))
        run([self.screen], nbr.STALE_MS + 500)
        self.assertTrue(self.screen.neighbours[OTHER_MAC].stale)
        self.assertEqual(self.oled.screen()[0], "Nbrs 0 ch1")
        self.assertTrue(any("nbr gone 223344AA" in l for l in linklog.lines))
        self.other_badge(nbr.beacon(OTHER_ID, "Bob"))
        run([self.screen], 100)
        self.assertFalse(self.screen.neighbours[OTHER_MAC].stale)
        self.assertTrue(any("nbr back 223344AA" in l for l in linklog.lines))

    def test_console_shows_the_newest_link_log_lines(self):
        self.other_badge(nbr.beacon(OTHER_ID, "Bob"))
        run([self.screen], 100)
        press(self.screen, BTN_SELECT)
        rows = self.oled.screen()
        self.assertTrue(rows[0].startswith("Log "))
        shown = "".join(r.lstrip() for r in rows[1:])
        self.assertIn("BN" + OTHER_ID + ",Bob", shown)

    def test_console_scrolls_back_and_resumes_following(self):
        for i in range(30):
            linklog.log("test", "line %02d" % i)
        press(self.screen, BTN_SELECT)
        follow_rows = self.oled.screen()
        for _ in range(3):
            press(self.screen, BTN_PREV)
        self.assertIsNotNone(self.screen.log_offset)
        self.assertNotEqual(self.oled.screen(), follow_rows)
        linklog.log("test", "arrives while scrolled back")
        run([self.screen], 100)
        shown = lambda: "".join(self.oled.screen()).replace(" ", "")
        self.assertNotIn("arrives", shown())
        for _ in range(10):
            press(self.screen, BTN_NEXT)
        self.assertIsNone(self.screen.log_offset)
        self.assertIn("arriveswhilescrolledback", shown())

    def test_select_toggles_back_to_the_list(self):
        press(self.screen, BTN_SELECT)
        press(self.screen, BTN_SELECT)
        self.assertEqual(self.screen.view, "list")
        self.assertTrue(self.oled.screen()[0].startswith("Nbrs"))

    def test_back_shuts_the_radio_and_flushes_the_log(self):
        esp = self.screen.esp
        self.assertEqual(press(self.screen, BTN_BACK), "utils")
        self.assertFalse(self.screen.running)
        self.assertFalse(esp.active_)
        text = Path(linklog.LOG_PATH).read_text()
        self.assertIn("nbr espnow up ch 1 mac 02:00:00:00:00:01", text)
        self.assertIn("nbr espnow down", text)

    def test_radio_failure_is_shown_and_explained_in_the_log(self):
        FakeEspNow.fail_init = True
        screen = nbr.WifiNeighboursScreen(self.oled)
        self.assertIsNone(screen.esp)
        self.assertEqual(self.oled.screen()[0], "Radio failed")
        run([screen], 1000)
        press(screen, BTN_SELECT)
        shown = "".join(r.lstrip() for r in self.oled.screen()[1:])
        self.assertIn("ESP_ERR_WIFI_NOT_INIT", shown)
        self.assertEqual(press(screen, BTN_BACK), "utils")

    def test_start_and_failure_lines_report_heap_and_wifi_memory(self):
        heap = types.SimpleNamespace(
            HEAP_DATA=4,
            idf_heap_info=lambda _caps: [(90000, 30000, 12000, 1),
                                         (40000, 8000, 7000, 1)])
        FakeEspNow.fail_init = True
        with patch.object(espnow_link, "esp32", heap):
            nbr.WifiNeighboursScreen(self.oled)
        failure = [l for l in linklog.lines if "init failed" in l][-1]
        self.assertIn("mp free 123456 idf free 38000 largest 12000", failure)
        starting = [l for l in linklog.lines if "espnow starting" in l][-1]
        self.assertIn("mp free 123456 idf free 38000", starting)


if __name__ == "__main__":
    unittest.main()
