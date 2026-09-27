"""Pong timing and link tests, run against the fakes in link_fakes.py."""

import asyncio
import importlib.util
import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import link_fakes  # noqa: E402
from link_fakes import Air, Clock as _Clock  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class _Font:
    @staticmethod
    def height():
        return 14


class _Writer:
    font = _Font()

    def __init__(self, *_args, **_kwargs):
        pass

    @staticmethod
    def stringlen(text):
        return 6 * len(text)

    @staticmethod
    def set_textpos(*_args):
        pass

    @staticmethod
    def printstring(_text):
        pass


class _FakeOled:
    width = 128
    height = 64

    def __init__(self):
        self.shown = 0

    def show(self):
        self.shown += 1

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Task:
    def cancel(self):
        pass


async def _sleep_ms(_ms):
    return None


def _install_stubs():
    bsides = types.ModuleType("bsides")
    bsides.BTN_NEXT, bsides.BTN_PREV = 1, 2
    bsides.BTN_SELECT, bsides.BTN_BACK = 3, 4
    bsides.GamesScreen = lambda oled: "games"
    bsides.device_id = "AAAAAAAAAAAA"
    sys.modules["bsides"] = bsides

    uasyncio = types.ModuleType("uasyncio")
    uasyncio.create_task = lambda coro: (coro.close(), _Task())[1]
    uasyncio.sleep_ms = _sleep_ms
    uasyncio.CancelledError = Exception
    sys.modules["uasyncio"] = uasyncio

    writer_package = types.ModuleType("writer")
    writer_package.__path__ = []
    writer = types.ModuleType("writer.writer")
    writer.Writer = _Writer
    sys.modules["writer"] = writer_package
    sys.modules["writer.writer"] = writer
    sys.modules["writer.font6"] = types.ModuleType("writer.font6")
    sys.modules["writer.freesans20"] = types.ModuleType("writer.freesans20")

    link_fakes.install()


_install_stubs()
SPEC = importlib.util.spec_from_file_location(
    "pong_test", ROOT / "software" / "games" / "pong.py")
pong = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pong)
link_fakes.restore()
badge_link = pong.badge_link

BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4
HIGH_ID, LOW_ID = "FFFF00000001", "0000AAAA0002"


def make(device_id):
    pong.bsides.device_id = device_id
    return pong.PongScreen(_FakeOled())


def wire(a, b):
    link_fakes.wire(a.link.uart, b.link.uart)


def wire_espnow(a, b):
    Air().join(a.link.esp, b.link.esp)


def pump(games, ms=600):
    for _ in range(ms // pong.TICK_MS):
        _Clock.now += pong.TICK_MS
        for g in games:
            g.link.poll(_Clock.now)
            g._tick(_Clock.now)


def press(game, btn):
    return asyncio.run(game.handle_button(btn))


def pair_radio(a, b):
    """a invites b from the lobby, b accepts."""
    pump([a, b], 1500)
    a.link.index = [p.id for p in a.link.listed()].index(b.my_id)
    press(a, BTN_SELECT)
    pump([a, b], 600)
    assert b.link.state == "invited", b.link.state
    press(b, BTN_SELECT)
    pump([a, b], 600)


def play_to_over(host, guest, others=()):
    """Run the clock until the match ends (60 s), keeping paddles clear so
    nobody scores early is not required: the timer alone ends it."""
    everyone = [host, guest] + list(others)
    for _ in range(pong.GAME_SECONDS + 5):
        pump(everyone, 1000)
        if host.phase == "over" and guest.phase == "over":
            return
    raise AssertionError("match never reached 'over': %s / %s" %
                         (host.phase, guest.phase))


class LinkedGameTests(unittest.TestCase):
    def setUp(self):
        random.seed(3)
        _Clock.now = 100000
        self.host = make(HIGH_ID)
        self.guest = make(LOW_ID)
        wire(self.host, self.guest)
        self.both = [self.host, self.guest]
        pump(self.both, 2000)          # the cable pairs them by itself

    def test_higher_id_hosts_and_both_enter_the_same_count(self):
        h, g = self.host, self.guest
        self.assertTrue(h.is_host)
        self.assertFalse(g.is_host)
        self.assertEqual((h.phase, g.phase), ("count", "count"))
        self.assertEqual(h.match_no, 1)

    def test_countdown_ends_in_a_synchronised_play_phase(self):
        h, g = self.host, self.guest
        pump(self.both, pong.COUNTDOWN_MS + 500)
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        self.assertEqual(g.match_no, h.match_no)

    def test_ball_position_is_mirrored_for_the_guest(self):
        h, g = self.host, self.guest
        pump(self.both, pong.COUNTDOWN_MS + 500)
        # The guest is at most one state heartbeat behind the host, except
        # right after a score, when the ball jumps back to the centre - so
        # sample repeatedly and only require most snapshots to agree.
        max_travel = pong.BALL_SPEED_MAX * pong.STATE_MS / pong.TICK_MS
        misses = 0
        for _ in range(20):
            pump(self.both, 200)
            if abs(g.bx - (h.oled.width - h.bx - pong.BALL)) > max_travel or \
                    abs(g.by - h.by) > max_travel:
                misses += 1
        self.assertLessEqual(misses, 1)

    def test_guest_paddle_reaches_the_host_as_op_y(self):
        h, g = self.host, self.guest
        pump(self.both, pong.COUNTDOWN_MS + 500)
        press(g, BTN_SELECT)                 # guest paddle down
        pump(self.both, 400)
        self.assertEqual(h.op_y, g.paddle_y)

    def test_match_ends_on_the_clock_without_any_extra_message(self):
        h, g = self.host, self.guest
        play_to_over(h, g)
        self.assertEqual((h.phase, g.phase), ("over", "over"))
        self.assertEqual(g.score_l, h.score_l)
        self.assertEqual(g.score_r, h.score_r)

    def test_guest_rematch_request_is_repeated_until_the_host_restarts(self):
        h, g = self.host, self.guest
        play_to_over(h, g)
        first_match = h.match_no
        # The first couple of heartbeats carrying the request are lost
        # (including the one sent immediately on the button press); it
        # must still get through once they stop being dropped.
        g.link.uart.loss = 1.0
        press(g, BTN_SELECT)                 # guest asks for a rematch
        self.assertTrue(g.want_rematch)
        pump(self.both, 3 * pong.CLIENT_MS)
        self.assertEqual(h.match_no, first_match)     # not through yet
        g.link.uart.loss = 0.0
        pump(self.both, pong.CLIENT_MS + 200)
        self.assertEqual(h.match_no, first_match + 1)
        self.assertEqual((h.phase, g.phase), ("count", "count"))
        self.assertFalse(g.want_rematch)

    def test_host_can_restart_without_waiting_for_the_guest(self):
        h, g = self.host, self.guest
        play_to_over(h, g)
        press(h, BTN_SELECT)
        self.assertEqual(h.phase, "count")
        pump(self.both, 500)
        self.assertEqual(g.phase, "count")
        self.assertEqual(g.match_no, h.match_no)

    def test_a_lossy_gap_pauses_and_resumes_the_same_match_no_restart(self):
        h, g = self.host, self.guest
        pump(self.both, pong.COUNTDOWN_MS + 500)
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        match_before = h.match_no
        h.link.uart.plugged = False
        pump(self.both, pong.LOST_TIMEOUT_MS + 500)
        self.assertEqual((h.phase, g.phase), ("lost", "lost"))
        # physics is frozen while lost: further waiting changes nothing
        score_before = (h.score_l, h.score_r)
        ball_before = (h.bx, h.by)
        pump(self.both, 2000)
        self.assertEqual(h.match_no, match_before)
        self.assertEqual((h.score_l, h.score_r), score_before)
        self.assertEqual((h.bx, h.by), ball_before)
        h.link.uart.plugged = True
        pump(self.both, 500)
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        self.assertEqual(g.match_no, match_before)

    def test_lost_select_returns_to_the_lobby(self):
        h, g = self.host, self.guest
        pump(self.both, pong.COUNTDOWN_MS + 500)
        h.link.uart.plugged = False
        pump(self.both, pong.LOST_TIMEOUT_MS + 500)
        self.assertEqual(h.phase, "lost")
        press(h, BTN_SELECT)
        self.assertEqual((h.phase, h.link.state), ("lobby", "lobby"))

    def test_back_releases_the_uart_and_the_partner_returns_to_the_lobby(self):
        h, g = self.host, self.guest
        self.assertEqual(press(h, BTN_BACK), "games")
        self.assertFalse(h.running)
        self.assertTrue(h.link.uart.closed)
        pump([g], 200)
        self.assertEqual(g.phase, "lobby")


class EspNowLinkTests(unittest.TestCase):
    """Same protocol, but with no cable at all: the players pair in the
    lobby over the radio."""

    def setUp(self):
        random.seed(3)
        _Clock.now = 100000
        # DEBUG_LINK defaults to off; one test below checks the debug
        # logging still works (phase changes, traffic) when it is on -
        # covering pairing too, so it has to wrap the whole setUp.
        with patch.object(pong, "DEBUG_LINK", True):
            self.host = make(HIGH_ID)
            self.guest = make(LOW_ID)
            wire_espnow(self.host, self.guest)
            self.both = [self.host, self.guest]
            pair_radio(self.guest, self.host)

    def test_links_and_plays_without_a_cable(self):
        h, g = self.host, self.guest
        self.assertEqual((h.phase, g.phase), ("count", "count"))
        self.assertTrue(h.is_host)
        pump(self.both, pong.COUNTDOWN_MS + 500)
        self.assertEqual((h.phase, g.phase), ("play", "play"))

    def test_link_events_reach_the_persisted_link_log(self):
        pong.linklog.flush()
        log = pong.linklog
        text = "".join(Path(p).read_text() for p in
                       (log.OLD_PATH, log.LOG_PATH) if Path(p).exists())
        mac = badge_link.espnow_link.mac_hex(self.host.link.esp.mac)
        self.assertIn("pong espnow up ch 1 mac " + mac, text)
        self.assertIn("pong paired with " + LOW_ID, text)
        self.assertIn("pong phase lobby -> count", text)


class LonelyBadgeTests(unittest.TestCase):
    def setUp(self):
        _Clock.now = 100000

    def test_waits_in_the_lobby_advertising(self):
        g = make(HIGH_ID)
        pump([g], 5000)
        self.assertEqual((g.phase, g.link.state), ("lobby", "lobby"))
        adverts = [m for m in g.link.uart.sent if m.startswith(b"QA")]
        self.assertGreaterEqual(len(adverts), 4)

    def test_identical_ids_are_reported(self):
        a, b = make(HIGH_ID), make(HIGH_ID)
        wire(a, b)
        pump([a, b], 2500)
        self.assertEqual((a.phase, b.phase), ("clash", "clash"))

    def test_other_games_on_the_cable_are_ignored(self):
        g = make(HIGH_ID)
        other = link_fakes.FakeUart()
        link_fakes.wire(g.link.uart, other)
        for _ in range(20):
            other.write(b"HFFFF00000001\n")
            other.write(badge_link.encode(b"T", b"AB0,ttt") + b"\n")
            pump([g], 300)
        self.assertEqual(g.phase, "lobby")
        self.assertIsNone(g.is_host)


class CrowdTests(unittest.TestCase):
    """Two matches in one room, with a fifth badge waiting in the lobby."""

    def test_two_matches_on_one_channel_stay_apart(self):
        random.seed(5)
        _Clock.now = 100000
        room = Air(loss=0.1)
        ids = ("F1" + "0" * 10, "A1" + "0" * 10, "F2" + "0" * 10,
               "A2" + "0" * 10, "C3" + "0" * 10)
        h1, g1, h2, g2, lone = badges = [make(i) for i in ids]
        room.join(*(b.link.esp for b in badges))
        room.loss = 0.0
        pair_radio(g1, h1)
        pair_radio(h2, g2)
        room.loss = 0.1
        pump(badges, pong.COUNTDOWN_MS + 2000)
        self.assertEqual([b.phase for b in badges],
                         ["play"] * 4 + ["lobby"])
        self.assertEqual((h1.peer_id, h2.peer_id), (ids[1], ids[3]))
        self.assertNotEqual(h1.match_no and id(h1.link), h2.match_no and
                           id(h2.link))
        press(g1, BTN_NEXT)                  # g1's paddle moves
        pump(badges, 1000)
        self.assertEqual(h1.op_y, g1.paddle_y)
        self.assertNotEqual(h2.op_y, g1.paddle_y)


if __name__ == "__main__":
    unittest.main()
