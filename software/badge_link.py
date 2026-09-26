"""Pairing and transport for two-player badge games.

Until two badges agree to play, a PeerLink runs a lobby: it advertises this
badge to the same game on other badges, over the radio and the cable, lists
the badges it hears, and handles invitations. After pairing, only frames from
that partner reach the game. They carry the pair's session code and, on the
radio, must come from the partner's MAC address, so any number of pairs can
play within range of each other. A cable joins exactly two badges, so badges
that hear each other over it pair without asking.

Frames are <prefix><body>*<xor checksum>, one per ESP-NOW datagram or one per
cable line. The game's prefix keeps different games apart. Bodies:
  A<id>,<name>          advert, while not paired
  I<from>,<to>,<code>   invitation, repeated until answered
  K<from>,<to>,<code>   accepted
  N<from>,<to>          declined, busy, or invitation withdrawn
  G<code>,<message>     game message between partners
  X<code>               partner left
"""

import time
import urandom
import machine

import espnow_link
import linklog


# UART1 on the UART pads (GPIO20/GPIO21): TX to RX both ways, plus GND.
UART_ID = 1
UART_RX = 20
UART_TX = 21
UART_BAUD = 115200

# Same button ids as bsides.
BTN_NEXT = 1
BTN_PREV = 2
BTN_SELECT = 3
BTN_BACK = 4

ADVERT_MS = 1000
PLAYER_TTL_MS = 3500      # a badge not heard for this long leaves the list
INVITE_MS = 300
INVITE_TIMEOUT_MS = 15000
INVITE_STALE_MS = 2000    # an invitation not repeated for this long is gone
NOTE_MS = 4000
LIST_ROWS = 5

# False makes new links ignore the cable (the hardware tests use it to force
# the radio).
CABLE = True


def checksum(data):
    c = 0
    for b in data:
        c ^= b
    return c


def encode(prefix, body):
    return prefix + body + ("*%02X" % checksum(body)).encode()


def decode(prefix, line):
    """The body of a frame with this prefix, or None for anything else:
    other games, cable noise, truncated or corrupted frames."""
    if len(line) < 5 or line[0:1] != prefix or line[-3:-2] != b"*":
        return None
    body = line[1:-3]
    try:
        want = int(line[-2:].decode(), 16)
    except (ValueError, UnicodeError):
        return None
    return body if checksum(body) == want else None


def clean_name(name):
    return "".join(c for c in name if " " <= c <= "~")[:16]


class Player:
    def __init__(self, device_id):
        self.id = device_id
        self.name = ""
        self.mac = None
        self.cable = False
        self.seen = 0

    def label(self):
        return (self.name or self.id[-8:])[:15]


class PeerLink:
    """The lobby, then the connection to one partner.

    The game calls poll() and tick() regularly, send() for its messages, and
    gets on_paired(), on_unpaired(reason) and on_message(message) back."""

    def __init__(self, prefix, owner, device_id, name, on_paired, on_unpaired,
                 on_message, debug=False):
        self.prefix = prefix
        self.owner = owner
        self.my_id = device_id
        self.name = clean_name(name)
        self.on_paired = on_paired
        self.on_unpaired = on_unpaired
        self.on_message = on_message
        self.debug = debug
        self.cable = CABLE              # False ignores the cable entirely

        self.uart = machine.UART(UART_ID, baudrate=UART_BAUD,
                                 tx=machine.Pin(UART_TX),
                                 rx=machine.Pin(UART_RX), timeout=0)
        self._buf = b""
        # None means cable only; the reason is in the link log.
        self.esp, _mac = espnow_link.open_link(owner, device_id)
        self.radio = (espnow_link.Broadcaster(self.esp, owner)
                      if self.esp is not None else None)

        self.players = {}
        self.index = 0
        self.state = "lobby"            # lobby/inviting/invited/paired
        self.target = None              # who we invite, or who invites us
        self.session = None
        self.peer = None                # the Player we are paired with
        self._unicast = None            # the partner's MAC once registered
        self.note = ""
        self.dirty = True
        now = time.ticks_ms()
        self.note_until = now
        self.last_advert = time.ticks_add(now, -ADVERT_MS)
        self.last_invite = now
        self.invite_started = now
        self.invite_seen = now

    # ---------- lobby state ----------
    def listed(self):
        return sorted(self.players.values(), key=lambda p: (p.label(), p.id))

    def label(self, device_id):
        p = self.players.get(device_id)
        return p.label() if p else device_id[-8:]

    def _log(self, *parts):
        linklog.log(self.owner, *parts)

    def _set_note(self, text):
        self.note = text
        self.note_until = time.ticks_add(time.ticks_ms(), NOTE_MS)
        self.dirty = True

    def _to_lobby(self, note):
        self.state = "lobby"
        self.target = None
        self.session = None
        if note:
            self._set_note(note)
        self.dirty = True

    def invite(self, device_id):
        self.state = "inviting"
        self.target = device_id
        self.session = "%04X" % (urandom.getrandbits(16) or 1)
        now = time.ticks_ms()
        self.invite_started = now
        self.last_invite = time.ticks_add(now, -INVITE_MS)
        self._log("inviting", device_id, repr(self.label(device_id)))
        self.dirty = True

    def _accept(self):
        self._write(("K%s,%s,%s" % (self.my_id, self.target,
                                    self.session)).encode())
        self._pair(self.target)

    def _decline(self, device_id):
        self._write(("N%s,%s" % (self.my_id, device_id)).encode())

    def _pair(self, device_id):
        peer = self.players.get(device_id) or Player(device_id)
        self.peer = peer
        self.state = "paired"
        self.target = None
        self._unicast = None
        if peer.mac is not None and self.esp is not None:
            try:
                self.esp.add_peer(peer.mac)
                self._unicast = peer.mac
            except OSError as exc:
                # Broadcast still works: the partner filters by session.
                self._log("add_peer:", repr(exc))
        self._log("paired with", peer.id, repr(peer.name),
                  "over cable" if peer.cable else "over radio",
                  "session", self.session)
        self.note = ""
        self.dirty = True
        self.on_paired()

    def _unpair(self, reason):
        if self._unicast is not None:
            try:
                self.esp.del_peer(self._unicast)
            except OSError:
                pass
        self._log("unpaired:", reason or "left")
        self.peer = None
        self._unicast = None
        self._to_lobby(reason)
        self.on_unpaired(reason)

    def leave(self, note=""):
        """Tell the partner (or the badge we are inviting) we are going."""
        if self.state == "paired":
            body = ("X" + self.session).encode()
            self._write(body, self._unicast)
            if self._unicast is not None:
                self._write(body)           # and broadcast, in case
            self._unpair(note)
        elif self.state in ("inviting", "invited"):
            self._decline(self.target)
            self._to_lobby(note)

    def close(self):
        self.leave()
        try:
            self.uart.deinit()
        except Exception:
            pass
        espnow_link.close_link(self.esp, self.owner)

    # ---------- transport ----------
    def _write(self, body, mac=None):
        frame = encode(self.prefix, body)
        if self.cable:
            self.uart.write(frame + b"\n")
        sent = self.radio is not None and \
            self.radio.send(frame, mac or espnow_link.BROADCAST)
        if self.debug and body[0:1] != b"A":    # adverts would flood the log
            via = ("cable+" if self.cable else "") + ("radio" if sent else "")
            self._log("tx", via or "nowhere", body)

    def send(self, message):
        """Send a game message to the partner; False when not paired."""
        if self.state != "paired":
            return False
        self._write(("G%s," % self.session).encode() + message, self._unicast)
        return True

    def poll(self, now):
        n = self.uart.any()
        if n:
            data = self.uart.read(n)
            if self.cable:
                self._buf += data
        i = self._buf.find(b"\n")
        while i >= 0:
            line, self._buf = self._buf[:i], self._buf[i + 1:]
            self._on_frame(line, "cable", None, now)
            i = self._buf.find(b"\n")
        if len(self._buf) > 300:
            self._buf = self._buf[-100:]
        if self.esp is None:
            return
        while True:
            try:
                mac, msg = self.esp.recv(0)
            except OSError as exc:
                self._log("espnow recv failed:", repr(exc))
                return
            if msg is None:
                return
            self._on_frame(bytes(msg), "radio", bytes(mac), now)

    def _heard(self, device_id, via, mac, now, name=None):
        p = self.players.get(device_id)
        if p is None:
            p = self.players[device_id] = Player(device_id)
            self._log("lobby: heard", device_id, "over", via,
                      espnow_link.mac_hex(mac) if mac else "")
            self.dirty = True
        if name is not None and name != p.name:
            p.name = name
            self.dirty = True
        if via == "cable":
            p.cable = True
        else:
            p.mac = mac
        p.seen = now
        return p

    def _on_frame(self, line, via, mac, now):
        body = decode(self.prefix, line)
        if body is None:
            if self.debug and line:
                self._log("rx", via, "dropped:", line)
            return
        kind = body[0:1]
        if self.debug and kind != b"A":
            self._log("rx", via, body if mac is None
                      else body + b" from " + espnow_link.mac_hex(mac).encode())
        try:
            if kind == b"G":
                comma = body.find(b",")
                self._on_game(body[1:comma].decode(), body[comma + 1:], via,
                              mac, now)
                return
            fields = body[1:].decode().split(",", 2 if kind != b"A" else 1)
            if kind == b"A":
                self._heard(fields[0], via, mac, now, clean_name(fields[1]))
                if via == "cable" and self.state == "lobby" and self.cable:
                    self.invite(fields[0])
            elif kind == b"I":
                self._on_invite(fields[0], fields[1], fields[2], via, mac, now)
            elif kind == b"K":
                self._on_accept(fields[0], fields[1], fields[2], via, mac, now)
            elif kind == b"N":
                self._on_decline(fields[0], fields[1])
            elif kind == b"X":
                if self._from_peer(fields[0], via, mac):
                    self._unpair(self.peer.label() + " left")
        except (IndexError, ValueError, UnicodeError) as exc:
            if self.debug:
                self._log("rx", via, "bad frame:", repr(exc))

    def _from_peer(self, session, via, mac):
        """True for a frame of our pairing from our partner: the session
        code must match and, over the radio, the sender's MAC too."""
        if self.state != "paired" or session != self.session:
            return False
        return via == "cable" or self.peer.mac is None or mac == self.peer.mac

    def _on_game(self, session, message, via, mac, now):
        if self._from_peer(session, via, mac):
            self.on_message(message)

    def _on_invite(self, frm, to, session, via, mac, now):
        if to != self.my_id:
            return
        self._heard(frm, via, mac, now)
        if self.state == "paired":
            if frm != self.peer.id:
                self._decline(frm)                  # busy
                return
            if session == self.session:
                self._write(("K%s,%s,%s" % (self.my_id, frm,
                                            session)).encode())
                return                              # our accept was lost
            self._unpair(self.peer.label() + " left")   # it started over
        if self.state == "inviting":
            if frm != self.target:
                self._decline(frm)
            elif frm > self.my_id or (frm == self.my_id and
                                      session > self.session):
                # We invited each other: the higher ID's invitation wins.
                self.session = session
                self._accept()
            return
        if self.state == "invited":
            if frm == self.target:
                self.invite_seen = now
                self.session = session
            else:
                self._decline(frm)
            return
        self.target = frm
        self.session = session
        if via == "cable" and self.cable:
            self._accept()
            return
        self.state = "invited"
        self.invite_seen = now
        self._log("invited by", frm, repr(self.label(frm)))
        self.dirty = True

    def _on_accept(self, frm, to, session, via, mac, now):
        if to != self.my_id:
            return
        self._heard(frm, via, mac, now)
        if self.state == "inviting" and frm == self.target and \
                session == self.session:
            self._pair(frm)

    def _on_decline(self, frm, to):
        if to != self.my_id:
            return
        if self.state == "paired" and frm == self.peer.id:
            # It gave up inviting us before our accept got through.
            self._unpair(self.peer.label() + " left")
            return
        if frm != self.target:
            return
        if self.state == "inviting":
            self._log(frm, "declined")
            self._to_lobby(self.label(frm) + " said no")
        elif self.state == "invited":
            self._to_lobby(self.label(frm) + " gave up")

    def tick(self, now):
        if self.state != "paired":
            if time.ticks_diff(now, self.last_advert) >= ADVERT_MS:
                self.last_advert = now
                self._write(("A%s,%s" % (self.my_id, self.name)).encode())
            for p in list(self.players.values()):
                if p.id != self.target and \
                        time.ticks_diff(now, p.seen) >= PLAYER_TTL_MS:
                    del self.players[p.id]
                    self.dirty = True
            self.index = min(self.index, max(0, len(self.players) - 1))
        if self.state == "inviting":
            if time.ticks_diff(now, self.invite_started) >= INVITE_TIMEOUT_MS:
                self._log("no answer from", self.target)
                self._decline(self.target)
                self._to_lobby("No answer from " + self.label(self.target))
            elif time.ticks_diff(now, self.last_invite) >= INVITE_MS:
                self.last_invite = now
                self._write(("I%s,%s,%s" % (self.my_id, self.target,
                                            self.session)).encode())
        elif self.state == "invited" and \
                time.ticks_diff(now, self.invite_seen) >= INVITE_STALE_MS:
            self._to_lobby(self.label(self.target) + " gave up")
        if self.note and time.ticks_diff(now, self.note_until) >= 0:
            self.note = ""
            self.dirty = True

    # ---------- lobby screen ----------
    def handle_button(self, btn):
        """Lobby controls. False when the game should handle btn: BACK in
        the player list (leave the game), and everything once paired."""
        if self.state == "lobby":
            players = self.listed()
            if btn == BTN_BACK:
                return False
            if players and btn == BTN_NEXT:
                self.index = (self.index + 1) % len(players)
            elif players and btn == BTN_PREV:
                self.index = (self.index - 1) % len(players)
            elif players and btn == BTN_SELECT:
                self.invite(players[min(self.index, len(players) - 1)].id)
            self.dirty = True
            return True
        if self.state == "inviting":
            if btn == BTN_BACK:
                self.leave()
            return True
        if self.state == "invited":
            if btn == BTN_SELECT:
                self._accept()
            else:
                self.leave()
            return True
        return False

    def render(self, oled, title):
        """Draw the lobby with the 8-pixel font (16 characters a row)."""
        oled.text(title[:16], 0, 0, 1)
        if self.state == "inviting":
            oled.text("Asking", 0, 16, 1)
            oled.text(self.label(self.target), 0, 26, 1)
            oled.text("to play...", 0, 36, 1)
            oled.text("BACK=cancel", 0, 56, 1)
            return
        if self.state == "invited":
            oled.text("Invite from", 0, 16, 1)
            oled.text(self.label(self.target), 0, 26, 1)
            oled.text("SELECT=play", 0, 40, 1)
            oled.text("NEXT=no", 0, 48, 1)
            return
        players = self.listed()
        if not players:
            oled.text("Looking for", 0, 16, 1)
            oled.text("players...", 0, 26, 1)
        top = max(0, min(self.index - LIST_ROWS + 1,
                         len(players) - LIST_ROWS))
        for row, p in enumerate(players[top:top + LIST_ROWS]):
            mark = ">" if top + row == self.index else " "
            oled.text(mark + p.label(), 0, 12 + row * 8, 1)
        footer = self.note or ("SEL=ask BK=exit" if players else "BACK=exit")
        oled.text(footer[:16], 0, 56, 1)
