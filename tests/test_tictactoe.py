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
    bsides.wri6 = _Writer()
    bsides.device_id = "AAAAAAAAAAAA"
    bsides.GamesScreen = lambda oled: "games"
    sys.modules["bsides"] = bsides

    uasyncio = types.ModuleType("uasyncio")
    uasyncio.create_task = lambda coro: (coro.close(), _Task())[1]
    uasyncio.sleep_ms = _sleep_ms
    uasyncio.CancelledError = Exception
    sys.modules["uasyncio"] = uasyncio

    link_fakes.install()


_install_stubs()
SPEC = importlib.util.spec_from_file_location(
    "tictactoe_test", ROOT / "software" / "games" / "tictactoe.py")
ttt = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ttt)
link_fakes.restore()
badge_link = ttt.badge_link

BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4
HIGH_ID, LOW_ID = "FFFF00000001", "0000AAAA0002"


def make(device_id):
    ttt.bsides.device_id = device_id
    return ttt.TicTacToeScreen(_FakeOled())


def wire(a, b):
    """Plug a cable between two badges."""
    link_fakes.wire(a.link.uart, b.link.uart)


def wire_espnow(a, b):
    """Put two badges' radios in range of each other (and nobody else)."""
    Air().join(a.link.esp, b.link.esp)


def pump(games, ms=600):
    for _ in range(ms // ttt.TICK_MS):
        _Clock.now += ttt.TICK_MS
        for g in games:
            g.link.poll(_Clock.now)
            g._tick(_Clock.now)


def press(game, btn):
    _Clock.now += ttt.MOVE_GUARD_MS
    return asyncio.run(game.handle_button(btn))


def pair_radio(a, b):
    """a invites b from the lobby, b accepts; then they start a game."""
    pump([a, b], 1500)
    a.link.index = [p.id for p in a.link.listed()].index(b.my_id)
    press(a, BTN_SELECT)
    pump([a, b], 600)
    assert b.link.state == "invited", b.link.state
    press(b, BTN_SELECT)
    pump([a, b], 1000)


def play_cell(game, cell, others):
    """Move this badge's cursor onto an empty cell and press SELECT."""
    for _ in range(9):
        if game.cursor == cell:
            break
        press(game, BTN_NEXT)
    assert game.cursor == cell, (game.cursor, cell, game.board)
    press(game, BTN_SELECT)
    pump([game] + others)


def game_messages(uart):
    """Game messages (after the session code) a badge wrote to its cable."""
    bodies = [badge_link.decode(b"T", line.rstrip(b"\n")) for line in uart.sent]
    return [b[b.index(b",") + 1:] for b in bodies if b and b[0:1] == b"G"]


class HelperTests(unittest.TestCase):
    def test_winner(self):
        self.assertEqual(ttt.winner(list("---------")), ("-", None))
        self.assertEqual(ttt.winner(list("XXX-OO---")), ("X", (0, 1, 2)))
        self.assertEqual(ttt.winner(list("O-XO-XO--")), ("O", (0, 3, 6)))
        self.assertEqual(ttt.winner(list("X-O-XO--X")), ("X", (0, 4, 8)))
        self.assertEqual(ttt.winner(list("X-O-O-O-X")), ("O", (2, 4, 6)))
        self.assertEqual(ttt.winner(list("XOXXOOOXX")), ("D", None))
        # a full board with a winning line is a win, not a draw
        self.assertEqual(ttt.winner(list("XXXOOXXOO"))[0], "X")


class LinkedGameTests(unittest.TestCase):
    def setUp(self):
        random.seed(3)
        _Clock.now = 100000
        self.host = make(HIGH_ID)
        self.guest = make(LOW_ID)
        wire(self.host, self.guest)
        self.both = [self.host, self.guest]
        pump(self.both, 2000)           # the cable pairs them by itself

    def assert_in_sync(self):
        self.assertEqual(self.host.board, self.guest.board)
        self.assertEqual(self.host.turn, self.guest.turn)
        self.assertEqual(self.host.game_no, self.guest.game_no)

    def test_higher_id_hosts_and_plays_x(self):
        self.assertTrue(self.host.is_host)
        self.assertFalse(self.guest.is_host)
        self.assertEqual((self.host.my_mark(), self.guest.my_mark()), ("X", "O"))
        self.assertEqual((self.host.phase, self.guest.phase), ("play", "play"))
        self.assertNotEqual(self.guest.game_no, 0)
        self.assertEqual(self.host.turn, "X")
        self.assert_in_sync()

    def test_handshake_does_not_echo_forever(self):
        for g in self.both:
            g.link.uart.sent.clear()
        pump(self.both, 3000)
        for g in self.both:
            messages = game_messages(g.link.uart)
            self.assertTrue(messages)
            self.assertEqual([m for m in messages if m.startswith(b"H")], [])

    def test_full_game_host_wins(self):
        h, g = self.host, self.guest
        play_cell(h, 0, [g])
        play_cell(g, 4, [h])
        play_cell(h, 1, [g])
        play_cell(g, 8, [h])
        play_cell(h, 2, [g])
        self.assert_in_sync()
        self.assertEqual(ttt.winner(g.board), ("X", (0, 1, 2)))
        self.assertEqual((h.wins, h.losses, g.wins, g.losses), (1, 0, 0, 1))

    def test_moves_out_of_turn_or_on_taken_cells_are_ignored(self):
        h, g = self.host, self.guest
        press(g, BTN_SELECT)                 # guest tries to move first
        pump(self.both)
        self.assertEqual(h.board, ["-"] * 9)
        play_cell(h, 4, [g])
        press(h, BTN_SELECT)                 # host tries to move twice
        pump(self.both)
        self.assertEqual(h.board.count("X"), 1)
        # a forged client move onto the taken centre cell
        h._on_client(h.game_no, 4, "4")
        self.assertEqual(h.board[4], "X")
        # a move for some other game number
        h._on_client(h.game_no + 2, 0, "0")
        self.assertEqual(h.board[0], "-")
        self.assert_in_sync()

    def test_cursor_skips_taken_cells_and_hold_is_rate_limited(self):
        h, g = self.host, self.guest
        play_cell(h, 4, [g])
        self.assertNotEqual(g.cursor, 4)
        g.cursor = 3
        press(g, BTN_NEXT)
        self.assertEqual(g.cursor, 5)
        press(g, BTN_PREV)
        self.assertEqual(g.cursor, 3)
        for _ in range(30):                  # 10 ms auto-repeat burst
            _Clock.now += 10
            asyncio.run(g.handle_button(BTN_NEXT))
        self.assertLessEqual(g.cursor, 7)
        self.assertGreaterEqual(g.cursor, 5)

    def test_rematch_from_guest_alternates_starter_and_keeps_score(self):
        h, g = self.host, self.guest
        for who, cell in ((h, 0), (g, 3), (h, 1), (g, 4), (h, 2)):
            play_cell(who, cell, [h if who is g else g])
        first = h.game_no
        press(g, BTN_SELECT)                 # guest asks for a new game
        pump(self.both)
        self.assertEqual(h.game_no, first + 1)
        self.assertEqual(h.board, ["-"] * 9)
        self.assertEqual(h.turn, "O")        # guest starts game two
        self.assertFalse(g.want_rematch)
        self.assert_in_sync()
        for who, cell in ((g, 0), (h, 3), (g, 1), (h, 4), (g, 2)):
            play_cell(who, cell, [h if who is g else g])
        self.assertEqual((h.wins, h.losses), (1, 1))
        self.assertEqual((g.wins, g.losses), (1, 1))

    def test_game_survives_a_lossy_corrupting_link(self):
        h, g = self.host, self.guest
        h.link.uart.loss = g.link.uart.loss = 0.5
        moves = ((h, 4), (g, 0), (h, 8), (g, 2), (h, 1), (g, 7), (h, 3), (g, 5), (h, 6))
        for who, cell in moves:
            mark = who.my_mark()
            press_target = cell
            for _ in range(9):
                if who.cursor == press_target:
                    break
                press(who, BTN_NEXT)
            press(who, BTN_SELECT)
            for _ in range(40):
                pump(self.both, 250)
                if h.board[cell] == mark and g.board[cell] == mark:
                    break
            self.assertEqual(h.board[cell], mark)
            self.assertEqual(g.board[cell], mark)
        self.assert_in_sync()
        self.assertEqual(ttt.winner(h.board)[0], "D")
        self.assertEqual((h.draws, g.draws), (1, 1))
        # Half the traffic is lost, so a 2 s gap (a moment of "lost") can
        # happen; the link must always come back.
        for _ in range(20):
            if (h.phase, g.phase) == ("play", "play"):
                break
            pump(self.both, 250)
        self.assertEqual((h.phase, g.phase), ("play", "play"))

    def test_unplugged_cable_is_detected_and_game_resumes(self):
        h, g = self.host, self.guest
        play_cell(h, 4, [g])
        h.link.uart.plugged = False
        pump(self.both, ttt.LOST_TIMEOUT_MS + 500)
        self.assertEqual((h.phase, g.phase), ("lost", "lost"))
        h.link.uart.plugged = True
        pump(self.both, 1500)
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        self.assertEqual(h.board[4], "X")
        self.assert_in_sync()
        play_cell(g, 0, [h])
        self.assertEqual(h.board[0], "O")

    def test_guest_restart_gets_the_board_back(self):
        h = self.host
        play_cell(h, 4, [self.guest])
        play_cell(self.guest, 0, [h])
        asyncio.run(self.guest.handle_button(BTN_BACK))
        guest2 = make(LOW_ID)
        wire(h, guest2)
        pump([h, guest2], 2500)
        self.assertEqual(guest2.phase, "play")
        self.assertEqual(guest2.board, h.board)
        self.assertEqual(guest2.board[4], "X")

    def test_host_restart_starts_fresh_and_drops_stale_guest_move(self):
        h, g = self.host, self.guest
        play_cell(h, 4, [g])
        g.pending = 0                        # move queued as the host vanishes
        asyncio.run(h.handle_button(BTN_BACK))
        host2 = make(HIGH_ID)
        wire(host2, g)
        # Worst case: the random base gives the new host the very game
        # number the guest still holds, so only the hello handshake can
        # expose the restart.
        stale_game = g.game_no
        same_base = (stale_game - 1) // 2 - 1
        with patch.object(ttt.urandom, "getrandbits", lambda bits: same_base):
            pump([host2, g], 2500)
        self.assertEqual(host2.game_no, stale_game)
        self.assertEqual((host2.phase, g.phase), ("play", "play"))
        self.assertEqual(g.game_no, host2.game_no)
        self.assertIsNone(g.pending)
        self.assertEqual(host2.board, ["-"] * 9)
        self.assertEqual(g.board, ["-"] * 9)

    def test_starved_radio_backs_off_while_the_cable_keeps_playing(self):
        h, g = self.host, self.guest
        attempts = []

        def no_memory(_mac, _msg, _sync=True):
            attempts.append(_Clock.now)
            raise OSError(-12391, "ESP_ERR_ESPNOW_NO_MEM")

        h.link.esp.send = no_memory
        play_cell(h, 4, [g])
        pump(self.both, 12000)
        # The heartbeat alone would try about 48 sends in 12 s.
        self.assertLessEqual(len(attempts), 3)
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        self.assertEqual(g.board[4], "X")
        play_cell(g, 0, [h])
        self.assertEqual(h.board[0], "O")

    def test_back_releases_the_uart(self):
        self.assertEqual(press(self.host, BTN_BACK), "games")
        self.assertFalse(self.host.running)
        self.assertTrue(self.host.link.uart.closed)
        self.assertFalse(self.host.link.esp.active_)
        # and the guest is told, rather than waiting for a timeout
        pump([self.guest], 200)
        self.assertEqual(self.guest.phase, "lobby")


class EspNowLinkTests(unittest.TestCase):
    """Same protocol, but with no cable at all: the players pair in the
    lobby over the radio."""

    def setUp(self):
        random.seed(3)
        _Clock.now = 100000
        self.host = make(HIGH_ID)
        self.guest = make(LOW_ID)
        wire_espnow(self.host, self.guest)
        self.both = [self.host, self.guest]
        pair_radio(self.guest, self.host)

    def test_links_and_plays_without_a_cable(self):
        h, g = self.host, self.guest
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        self.assertTrue(h.is_host)
        play_cell(h, 4, [g])
        play_cell(g, 0, [h])
        self.assertEqual(h.board, g.board)
        self.assertEqual(h.board[4], "X")
        self.assertEqual(h.board[0], "O")

    def test_link_events_reach_the_persisted_link_log(self):
        ttt.linklog.flush()
        log = ttt.linklog
        text = "".join(Path(path).read_text() for path in
                       (log.OLD_PATH, log.LOG_PATH) if Path(path).exists())
        mac = ttt.badge_link.espnow_link.mac_hex(
            self.host.link.esp.mac)
        self.assertIn("ttt espnow up ch 1 mac " + mac, text)
        self.assertIn("ttt inviting " + HIGH_ID, text)
        self.assertIn("ttt paired with " + LOW_ID, text)
        self.assertIn("ttt phase lobby -> link", text)
        self.assertIn("ttt phase link -> play", text)
        self.assertIn("ttt hello from " + LOW_ID, text)
        self.assertIn(",S", text)
        self.assertIn(" from " + mac, text)

    def test_falls_back_to_uart_when_espnow_is_unavailable(self):
        """No espnow module (older firmware, no Wi-Fi radio, ...) -> the
        cable-only path from before this feature still works unmodified."""
        with patch.object(badge_link.espnow_link, "espnow", None):
            h, g = make(HIGH_ID), make(LOW_ID)
        self.assertIsNone(h.link.esp)
        self.assertIsNone(g.link.esp)
        wire(h, g)
        pump([h, g], 2000)
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        play_cell(h, 4, [g])
        self.assertEqual(g.board[4], "X")

    def test_duplicate_delivery_over_both_links_does_not_desync(self):
        """A cable plugged in alongside a working Wi-Fi link delivers every
        message twice; the game number guard must absorb that without
        double-advancing a new game or double-counting a move."""
        h, g = self.host, self.guest
        wire(h, g)                          # cable now also connects them
        for who, cell in ((h, 0), (g, 3), (h, 1), (g, 4), (h, 2)):
            play_cell(who, cell, [h if who is g else g])
        first_game = h.game_no
        self.assertEqual(ttt.winner(h.board), ("X", (0, 1, 2)))
        press(g, BTN_SELECT)                # guest asks for a rematch
        pump(self.both)
        self.assertEqual(h.game_no, first_game + 1)
        self.assertEqual(h.board, ["-"] * 9)
        self.assertEqual(h.board, g.board)


class CrowdTests(unittest.TestCase):
    """Two games in one room, with a fifth badge waiting in the lobby."""

    def test_two_games_on_one_channel_stay_apart(self):
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
        pump(badges, 1000)
        self.assertEqual([b.phase for b in badges], ["play"] * 4 + ["lobby"])
        self.assertEqual((h1.peer_id, h2.peer_id), (ids[1], ids[3]))

        for who, cell, other in ((h1, 0, g1), (h2, 8, g2), (g1, 4, h1),
                                 (g2, 3, h2), (h1, 1, g1), (h2, 7, g2)):
            play_cell(who, cell, [b for b in badges if b is not who])
            pump(badges, 1000)
        self.assertEqual("".join(h1.board), "XX--O----")
        self.assertEqual("".join(h2.board), "---O---XX")
        self.assertEqual(h1.board, g1.board)
        self.assertEqual(h2.board, g2.board)
        self.assertEqual(lone.phase, "lobby")


class LonelyBadgeTests(unittest.TestCase):
    def setUp(self):
        _Clock.now = 100000

    def test_waits_in_the_lobby_advertising(self):
        g = make(HIGH_ID)
        pump([g], 5000)
        self.assertEqual((g.phase, g.link.state), ("lobby", "lobby"))
        adverts = [m for m in g.link.uart.sent if m.startswith(b"TA")]
        self.assertGreaterEqual(len(adverts), 4)
        press(g, BTN_SELECT)                # nobody listed: nothing happens
        self.assertEqual(g.phase, "lobby")

    def test_late_peer_links(self):
        a = make(HIGH_ID)
        pump([a], 12000)
        b = make(LOW_ID)
        wire(a, b)
        pump([a, b], 2500)
        self.assertEqual((a.phase, b.phase), ("play", "play"))
        self.assertEqual(a.board, b.board)

    def test_identical_ids_are_reported(self):
        a, b = make(HIGH_ID), make(HIGH_ID)
        wire(a, b)
        pump([a, b], 2500)
        self.assertEqual((a.phase, b.phase), ("clash", "clash"))

    def test_lost_partner_select_returns_to_the_lobby(self):
        a, b = make(HIGH_ID), make(LOW_ID)
        wire(a, b)
        pump([a, b], 2500)
        a.link.uart.plugged = False
        pump([a, b], ttt.LOST_TIMEOUT_MS + 500)
        self.assertEqual(a.phase, "lost")
        press(a, BTN_SELECT)
        self.assertEqual((a.phase, a.link.state), ("lobby", "lobby"))

    def test_pong_on_the_other_end_is_ignored(self):
        g = make(HIGH_ID)
        other = link_fakes.FakeUart()
        link_fakes.wire(g.link.uart, other)
        for _ in range(20):
            other.write(b"H0000AAAA0002\n")
            other.write(b"G\n")
            pump([g], 300)
        self.assertEqual(g.phase, "lobby")
        self.assertIsNone(g.is_host)


if __name__ == "__main__":
    unittest.main()
