import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import link_fakes  # noqa: E402
from link_fakes import Air, Clock, FakeUart, wire  # noqa: E402

linklog, espnow_link, badge_link = link_fakes.install()
link_fakes.restore()

BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4
TICK_MS = 50


class RecordingOled:
    def __init__(self):
        self.rows = {}

    def text(self, text, x, y, _color):
        assert len(text) <= 16 and x == 0, (text, x)
        self.rows[y // 8] = text

    def screen(self):
        return [self.rows.get(i, "") for i in range(8)]


class Badge:
    """A PeerLink plus a record of what it told its game."""

    def __init__(self, device_id, name=""):
        self.id = device_id
        self.paired = 0
        self.unpaired = []
        self.messages = []
        self.link = badge_link.PeerLink(
            b"T", "test", device_id, name, on_paired=self._paired,
            on_unpaired=self.unpaired.append, on_message=self.messages.append)

    def _paired(self):
        self.paired += 1

    @property
    def state(self):
        return self.link.state

    def press(self, btn):
        return self.link.handle_button(btn)

    def choose(self, other):
        """Move the lobby cursor onto other and invite it."""
        ids = [p.id for p in self.link.listed()]
        self.link.index = ids.index(other.id)
        self.press(BTN_SELECT)

    def screen(self):
        oled = RecordingOled()
        self.link.render(oled, "GAME")
        return oled.screen()


def run(badges, ms=500):
    for _ in range(ms // TICK_MS):
        Clock.now += TICK_MS
        for b in badges:
            b.link.poll(Clock.now)
            b.link.tick(Clock.now)


def run_until(badges, done, ms=5000):
    for _ in range(ms // TICK_MS):
        if done():
            return True
        run(badges, TICK_MS)
    return done()


def pair(a, b, others=()):
    """a invites b from its list, b accepts."""
    everyone = [a, b] + list(others)
    assert run_until(everyone, lambda: b.id in [p.id for p in a.link.listed()])
    a.choose(b)
    assert run_until(everyone, lambda: b.state == "invited"), b.state
    b.press(BTN_SELECT)
    assert run_until(everyone, lambda: a.state == b.state == "paired"), \
        (a.state, b.state)


class RadioTest(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        Clock.now = 100000
        self.air = Air.default = Air()

    def tearDown(self):
        Air.default = None


class FrameTests(unittest.TestCase):
    def test_frames_roundtrip_and_reject_garbage(self):
        line = badge_link.encode(b"T", b"G12AB,C12,4,7")
        self.assertEqual(badge_link.decode(b"T", line), b"G12AB,C12,4,7")
        self.assertIsNone(badge_link.decode(b"P", line))    # another game
        self.assertIsNone(badge_link.decode(b"T", line[:-1]))
        self.assertIsNone(badge_link.decode(b"T", line.replace(b",7", b",8")))
        self.assertIsNone(badge_link.decode(b"T", b""))
        self.assertIsNone(badge_link.decode(b"T", b"T*00"))
        self.assertIsNone(badge_link.decode(b"T", b"TC1,1,1*ZZ"))
        # Pong traffic on the same cable must never parse
        for pong in (b"HFFFF00000001", b"G", b"P23", b"S60,30,20,1,2,45", b"R"):
            self.assertIsNone(badge_link.decode(b"T", pong))

    def test_names_are_printable_and_short(self):
        self.assertEqual(badge_link.clean_name("Ann\n\x00é Lee-Smith-Jones"),
                         "Ann Lee-Smith-Jo")


class LobbyTests(RadioTest):
    def test_lists_badges_in_range_by_name(self):
        a, b, c = Badge("A0", "ann"), Badge("B0", "bob"), Badge("C0")
        run([a, b, c], 1500)
        self.assertEqual([p.label() for p in a.link.listed()], ["C0", "bob"])
        self.assertEqual(a.screen()[1:3], [">C0", " bob"])
        self.assertEqual(a.screen()[7], "SEL=ask BK=exit")

    def test_a_badge_that_goes_quiet_leaves_the_list(self):
        a, b = Badge("A0"), Badge("B0")
        run([a, b], 1500)
        self.assertEqual(len(a.link.listed()), 1)
        b.link.close()
        run([a], badge_link.PLAYER_TTL_MS + 1000)
        self.assertEqual(a.link.listed(), [])
        self.assertEqual(a.screen()[2:4], ["Looking for", "players..."])

    def test_back_in_the_list_is_left_to_the_game(self):
        a = Badge("A0")
        self.assertFalse(a.press(BTN_BACK))
        self.assertTrue(a.press(BTN_SELECT))    # nobody to invite: no-op
        self.assertEqual(a.state, "lobby")

    def test_invite_and_accept_pairs_both_with_one_session(self):
        a, b = Badge("A0", "ann"), Badge("B0", "bob")
        run([a, b], 1500)
        a.choose(b)
        self.assertEqual(a.screen()[2:5], ["Asking", "bob", "to play..."])
        run([a, b], 400)
        self.assertEqual(b.state, "invited")
        self.assertEqual(b.screen()[2:4], ["Invite from", "ann"])
        b.press(BTN_SELECT)
        run([a, b], 400)
        self.assertEqual((a.state, b.state), ("paired", "paired"))
        self.assertEqual((a.paired, b.paired), (1, 1))
        self.assertEqual(a.link.session, b.link.session)
        self.assertEqual((a.link.peer.id, b.link.peer.id), ("B0", "A0"))
        # game messages now go straight to the partner's MAC
        self.assertIn(b.link.esp.mac, a.link.esp.peers)
        a.link.send(b"hello")
        run([a, b], 100)
        self.assertEqual(b.messages, [b"hello"])
        self.assertEqual(a.link.esp.sent[-1], (b.link.esp.mac, a.link.esp.sent[-1][1]))

    def test_decline_returns_both_to_the_lobby(self):
        a, b = Badge("A0"), Badge("B0", "bob")
        run([a, b], 1500)
        a.choose(b)
        run([a, b], 400)
        b.press(BTN_NEXT)
        run([a, b], 400)
        self.assertEqual((a.state, b.state), ("lobby", "lobby"))
        self.assertEqual(a.screen()[7], "bob said no")
        run([a, b], badge_link.NOTE_MS)
        self.assertEqual(a.screen()[7], "SEL=ask BK=exit")
        self.assertEqual((a.paired, b.paired), (0, 0))

    def test_cancelled_invitation_is_withdrawn(self):
        a, b = Badge("A0", "ann"), Badge("B0")
        run([a, b], 1500)
        a.choose(b)
        run([a, b], 400)
        a.press(BTN_BACK)
        run([a, b], 200)
        self.assertEqual((a.state, b.state), ("lobby", "lobby"))
        self.assertEqual(b.screen()[7], "ann gave up")

    def test_unanswered_invitation_times_out(self):
        a, b = Badge("A0"), Badge("B0", "bob")
        run([a, b], 1500)
        a.choose(b)
        run([a, b], badge_link.INVITE_TIMEOUT_MS + 500)
        self.assertEqual((a.state, b.state), ("lobby", "lobby"))
        self.assertEqual(a.link.note, "No answer from bob")

    def test_invitation_from_a_badge_that_vanished_goes_stale(self):
        a, b = Badge("A0"), Badge("B0")
        run([a, b], 1500)
        a.choose(b)
        run([a, b], 400)
        a.link.esp.active(False)            # walked out of range
        run([b], badge_link.INVITE_STALE_MS + 200)
        self.assertEqual(b.state, "lobby")

    def test_invitations_to_each_other_pair_once(self):
        a, b = Badge("A0"), Badge("B0")
        run([a, b], 1500)
        a.choose(b)
        b.choose(a)
        run([a, b], 600)
        self.assertEqual((a.state, b.state), ("paired", "paired"))
        self.assertEqual(a.link.session, b.link.session)
        self.assertEqual((a.paired, b.paired), (1, 1))

    def test_lost_accept_is_repeated(self):
        a, b = Badge("A0"), Badge("B0")
        run([a, b], 1500)
        a.choose(b)
        run([a, b], 400)
        b.press(BTN_SELECT)
        a.link.esp.inbox.clear()            # the accept never arrived
        self.assertEqual((a.state, b.state), ("inviting", "paired"))
        run([a, b], 600)
        self.assertEqual((a.state, b.state), ("paired", "paired"))
        self.assertEqual(a.link.session, b.link.session)

    def test_inviter_giving_up_after_the_accept_unpairs_the_invitee(self):
        a, b = Badge("A0", "ann"), Badge("B0")
        run([a, b], 1500)
        a.choose(b)
        run([a, b], 400)
        b.press(BTN_SELECT)
        a.link.esp.inbox.clear()
        a.press(BTN_BACK)
        run([a, b], 200)
        self.assertEqual((a.state, b.state), ("lobby", "lobby"))
        self.assertEqual(b.unpaired, ["ann left"])

    def test_leaving_returns_the_partner_to_the_lobby(self):
        a, b = Badge("A0", "ann"), Badge("B0")
        pair(a, b)
        mac = b.link.esp.mac
        a.link.leave()
        run([a, b], 200)
        self.assertEqual((a.state, b.state), ("lobby", "lobby"))
        self.assertEqual(b.unpaired, ["ann left"])
        self.assertNotIn(mac, a.link.esp.peers)
        self.assertFalse(a.link.send(b"late"))
        # and they can play again
        pair(b, a)
        self.assertEqual((a.paired, b.paired), (2, 2))

    def test_starting_over_with_the_same_partner_replaces_the_session(self):
        a, b = Badge("A0"), Badge("B0")
        pair(a, b)
        old = a.link.session
        b.link._unpair("")                  # b lost track and asks again
        pair(b, a)
        self.assertNotEqual(a.link.session, old)
        self.assertEqual(a.link.session, b.link.session)


class CableTests(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        Clock.now = 100000

    def test_badges_on_a_cable_pair_without_asking(self):
        a, b = Badge("A0"), Badge("B0")
        wire(a.link.uart, b.link.uart)
        run([a, b], 1500)
        self.assertEqual((a.state, b.state), ("paired", "paired"))
        self.assertTrue(a.link.peer.cable)
        a.link.send(b"move")
        run([a, b], 100)
        self.assertEqual(b.messages, [b"move"])

    def test_cable_can_be_ignored(self):
        a, b = Badge("A0"), Badge("B0")
        a.link.cable = b.link.cable = False
        wire(a.link.uart, b.link.uart)
        run([a, b], 3000)
        self.assertEqual((a.state, b.state), ("lobby", "lobby"))
        self.assertEqual(a.link.listed(), [])
        self.assertEqual(a.link.uart.sent, [])

    def test_radio_off_still_pairs_over_the_cable(self):
        a, b = Badge("A0"), Badge("B0")
        a.link.esp = a.link.radio = None
        wire(a.link.uart, b.link.uart)
        run([a, b], 1500)
        self.assertEqual((a.state, b.state), ("paired", "paired"))

    def test_other_games_on_the_cable_are_ignored(self):
        a = Badge("A0")
        other = FakeUart()
        wire(a.link.uart, other)
        for _ in range(20):
            other.write(b"H0000AAAA0002\n")
            other.write(badge_link.encode(b"P", b"AB0,pong") + b"\n")
            run([a], 300)
        self.assertEqual(a.state, "lobby")
        self.assertEqual(a.link.listed(), [])


class CrowdTests(RadioTest):
    """Many badges in one room, on one lossy channel."""

    def setUp(self):
        super().setUp()
        self.air.loss = 0.2
        self.badges = [Badge("%02X0000000000" % i, "p%d" % i)
                       for i in range(6)]

    def test_two_pairs_play_side_by_side_among_bystanders(self):
        p0, p1, p2, p3, b4, b5 = self.badges
        pair(p0, p1, self.badges[2:])
        pair(p2, p3, [p0, p1, b4, b5])
        # Playing badges stop advertising, so the lobby soon drops them
        run(self.badges, badge_link.PLAYER_TTL_MS + 500)
        self.assertEqual([p.id for p in b4.link.listed()], [b5.id])
        # but an invitation sent from an old list is turned down.
        b4.link.invite(p0.id)
        b5.link.invite(p2.id)
        run(self.badges, 2000)
        self.assertEqual((b4.state, b5.state), ("lobby", "lobby"))
        self.assertIn("said no", b4.link.note)
        for badge in self.badges[:4]:
            self.assertEqual(badge.state, "paired")
            self.assertEqual(badge.paired, 1)
            self.assertEqual(badge.unpaired, [])

        for n in range(40):
            for badge in self.badges[:4]:
                badge.link.send(("%s:%d" % (badge.id[:2], n)).encode())
            run(self.badges, 100)
        for badge, partner in ((p0, p1), (p1, p0), (p2, p3), (p3, p2)):
            senders = {m.split(b":")[0].decode() for m in badge.messages}
            self.assertEqual(senders, {partner.id[:2]})
            self.assertGreater(len(badge.messages), 20)     # lossy air
        self.assertNotEqual(p0.link.session, p2.link.session)
        self.assertEqual((b4.messages, b5.messages), ([], []))

    def test_forged_frames_from_another_badge_are_ignored(self):
        p0, p1, rogue = self.badges[:3]
        self.air.loss = 0.0
        pair(p0, p1, [rogue])
        session = p0.link.session.encode()
        # A frame copied off the air, re-sent from the rogue's own MAC.
        rogue.link.radio.send(
            badge_link.encode(b"T", b"G" + session + b",evil"))
        # And one that tries to end their game.
        rogue.link.radio.send(badge_link.encode(b"T", b"X" + session))
        run([p0, p1, rogue], 200)
        self.assertEqual(p0.messages, [])
        self.assertEqual((p0.state, p1.state), ("paired", "paired"))

    def test_invite_meant_for_someone_else_is_ignored(self):
        p0, p1, p2 = self.badges[:3]
        self.air.loss = 0.0
        run([p0, p1, p2], 1500)
        p0.choose(p1)
        run([p0, p1, p2], 400)
        self.assertEqual((p1.state, p2.state), ("invited", "lobby"))


if __name__ == "__main__":
    unittest.main()
