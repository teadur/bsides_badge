import time
import urandom
import uasyncio as asyncio

import badge_link
import bsides
import linklog

from writer.writer import Writer
import writer.font6 as font6
import writer.freesans20 as freesans20

GAME_NAME = "Pong"

# Players pick each other in badge_link's lobby (over the radio, or the same
# cable as Tic-tac-toe: TX to RX both ways plus GND); after that every
# message goes to the partner over whichever of the two is up.
#
# Debug flag: log phase changes and raw traffic on both the cable and the
# radio to linklog (serial console, Utils -> WiFi neighbours, and
# /linklog.txt). Pairing and ESP-NOW errors are logged either way.
DEBUG_LINK = False

BTN_NEXT = bsides.BTN_NEXT
BTN_PREV = bsides.BTN_PREV
BTN_SELECT = bsides.BTN_SELECT
BTN_BACK = bsides.BTN_BACK

GAME_SECONDS = 60
TICK_MS = 20          # physics tick
RENDER_MS = 60        # about 16 fps on the 2025 badge
STATE_MS = 100        # host state heartbeat, independent of render timing
CLIENT_MS = 200        # guest heartbeat, independent of loop iterations
CLIENT_CHANGE_MS = 50  # cap traffic while a paddle button is held
COUNTDOWN_MS = 3000
LOST_TIMEOUT_MS = 3500

PADDLE_W = 2
PADDLE_H = 12
PADDLE_STEP = 1
BALL = 3
BALL_SPEED0 = 1.0
BALL_ACCEL = 0.15
BALL_SPEED_MAX = 2.4

# S carries the host's phase as one of these letters; C is sent by the guest.
PHASE_LETTER = {"count": "C", "play": "P", "over": "O"}
LETTER_PHASE = {v: k for k, v in PHASE_LETTER.items()}


class PongScreen:
    """
    Two-player Pong. Players pair in badge_link's lobby, then play over the
    cable and the radio at once. The badge with the higher device ID becomes
    host: it owns the ball, scores and match timer, and plays on the left.
    The other badge plays on the right. Every message the host sends carries
    the whole state (ball, both paddles, score, time left, and whose match
    this is), and every message the guest sends carries its paddle and
    whether it wants a rematch, so a lost or duplicated message never
    desyncs the game - it just shows up again on the next heartbeat, a few
    tens of milliseconds later. A dropped link pauses the match in place
    (the ball freezes; nothing is lost) and resumes it once messages flow
    again, rather than forcing a new match.
    Controls:
      lobby      -> NEXT/PREV pick a player, SELECT invites; an invitation
                    is accepted with SELECT, declined with NEXT or BACK
      NEXT/SELECT -> move your paddle up/down (hold either to keep moving)
      SELECT     -> rematch, once the match is over; back to the lobby
                    when the link is lost
      BACK       -> exit to menu

    The main UI sees manages_own_render and leaves rendering to the game loop.
    """
    manages_own_render = True
    repeat_select = True

    def __init__(self, oled):
        self.oled = oled
        self.wri6 = Writer(oled, font6, verbose=False)
        self.wri20 = Writer(oled, freesans20, verbose=False)

        self.hud_h = self.wri6.font.height()
        self.pf_top = self.hud_h
        self.pf_bot = oled.height - 2
        self.pl_x = 2
        self.pr_x = oled.width - 2 - PADDLE_W

        self.my_id = bsides.device_id
        self.link = badge_link.PeerLink(
            b"Q", "pong", self.my_id, getattr(bsides, "USERNAME", None) or "",
            on_paired=self._start_link, on_unpaired=self._on_unpaired,
            on_message=self._handle_message, debug=DEBUG_LINK)

        self.running = True
        self.peer_id = None
        self.is_host = None
        self.match_no = 0
        self._pre_lost_phase = "count"

        self._clear_scoreboard()
        self._set_phase("lobby")

        self.last_rx = time.ticks_ms()
        self.last_state_tx = self.last_rx
        self.last_client_tx = self.last_rx
        self.last_sent_paddle = -1
        self.last_render = time.ticks_add(self.last_rx, -RENDER_MS)
        self._dirty = True

        self._tasks = [asyncio.create_task(self._loop()),
                       asyncio.create_task(self._rx())]
        self.render()

    def _dbg(self, *parts):
        if DEBUG_LINK:
            linklog.log("pong", *parts)

    def _set_phase(self, phase):
        if phase != getattr(self, "phase", None):
            self._dbg("phase", getattr(self, "phase", None), "->", phase)
        self.phase = phase

    # ---------- state helpers ----------
    def _clear_scoreboard(self):
        self.score_l = 0
        self.score_r = 0
        self.paddle_y = self._clamp_paddle((self.pf_bot - PADDLE_H) // 2)
        self.op_y = self.paddle_y
        self.bx = (self.oled.width - BALL) // 2
        self.by = (self.pf_top + self.pf_bot - BALL) // 2
        self.vx = 0.0
        self.vy = 0.0
        self.time_left = GAME_SECONDS
        self.want_rematch = False

    def _clamp_paddle(self, y):
        return max(self.pf_top, min(self.pf_bot - PADDLE_H + 1, y))

    def _scores(self):
        if self.is_host:
            return self.score_l, self.score_r
        return self.score_r, self.score_l

    def _serve(self, direction=None):
        self.bx = (self.oled.width - BALL) // 2
        self.by = (self.pf_top + self.pf_bot - BALL) // 2
        if direction is None:
            direction = 1 if urandom.getrandbits(1) else -1
        self.vx = BALL_SPEED0 * direction
        self.vy = (urandom.getrandbits(8) - 128) / 160.0

    def _new_match(self):
        """Host only: a fresh match, freshly numbered."""
        self.match_no += 1
        self._clear_scoreboard()
        self._set_phase("count")
        self.count_start = time.ticks_ms()
        self._dirty = True

    # ---------- link ----------
    def _start_link(self):
        """Paired: the higher device ID hosts and starts the match; the
        guest assumes a match is starting too, and the host's first state
        heartbeat corrects it within about STATE_MS if it guessed wrong."""
        peer = self.link.peer
        now = time.ticks_ms()
        self.last_rx = now
        self.last_state_tx = time.ticks_add(now, -STATE_MS)
        self.last_client_tx = time.ticks_add(now, -CLIENT_MS)
        if peer.id == self.my_id:
            self._set_phase("clash")
            self._dirty = True
            return
        self.peer_id = peer.id
        self.is_host = self.my_id > peer.id
        if self.is_host:
            self._new_match()
        else:
            self._clear_scoreboard()
            self._set_phase("count")
            self.count_start = now
        self._dirty = True

    def _on_unpaired(self, reason):
        self.is_host = None
        self.peer_id = None
        self.match_no = 0
        self._set_phase("lobby")
        self._dirty = True

    def _send(self, payload):
        self.link.send(payload)

    def _send_state(self):
        self._send(("S%d,%s,%d,%d,%d,%d,%d,%d" % (
            self.match_no, PHASE_LETTER[self.phase], int(self.bx), int(self.by),
            self.paddle_y, self.score_l, self.score_r, self.time_left)).encode())
        self.last_state_tx = time.ticks_ms()

    def _send_client(self):
        self._send(("C%d,%d,%d" % (
            self.match_no, self.paddle_y, 1 if self.want_rematch else 0)).encode())
        self.last_client_tx = time.ticks_ms()
        self.last_sent_paddle = self.paddle_y

    # ---------- host physics ----------
    def _overlaps(self, py):
        return self.by + BALL - 1 >= py and self.by <= py + PADDLE_H - 1

    def _bounce(self, py, direction):
        speed = min(BALL_SPEED_MAX, abs(self.vx) + BALL_ACCEL)
        self.vx = speed * direction
        off = (self.by + BALL / 2 - (py + PADDLE_H / 2)) / (PADDLE_H / 2)
        self.vy = off * speed * 0.75

    def _physics(self, now):
        self.time_left = GAME_SECONDS - time.ticks_diff(now, self.play_start) // 1000
        if self.time_left <= 0:
            self.time_left = 0
            self._set_phase("over")
            self._dirty = True
            return

        self.bx += self.vx
        self.by += self.vy

        if self.by <= self.pf_top:
            self.by = self.pf_top
            self.vy = abs(self.vy)
        elif self.by + BALL - 1 >= self.pf_bot:
            self.by = self.pf_bot - BALL + 1
            self.vy = -abs(self.vy)

        if self.vx < 0 and self.bx <= self.pl_x + PADDLE_W and self.bx + BALL >= self.pl_x:
            if self._overlaps(self.paddle_y):
                self.bx = self.pl_x + PADDLE_W
                self._bounce(self.paddle_y, 1)
        if self.vx > 0 and self.bx + BALL >= self.pr_x and self.bx <= self.pr_x + PADDLE_W:
            if self._overlaps(self.op_y):
                self.bx = self.pr_x - BALL
                self._bounce(self.op_y, -1)

        if self.bx + BALL < 0:
            self.score_r += 1
            self._serve(-1)
        elif self.bx > self.oled.width:
            self.score_l += 1
            self._serve(1)

    # ---------- receive ----------
    def _handle_message(self, payload):
        tag = payload[0:1]
        try:
            parts = payload[1:].decode().split(",")
            if tag == b"S":
                self._on_state(int(parts[0]), parts[1], int(parts[2]),
                               int(parts[3]), int(parts[4]), int(parts[5]),
                               int(parts[6]), int(parts[7]))
            elif tag == b"C":
                self._on_client(int(parts[0]), int(parts[1]), parts[2] == "1")
        except (ValueError, IndexError, UnicodeError) as exc:
            self._dbg("dropped message:", payload, repr(exc))

    def _on_state(self, match_no, phase_letter, bx, by, hy, sl, sr, t):
        if self.is_host is not False:
            return
        new_phase = LETTER_PHASE.get(phase_letter)
        if new_phase is None:
            return
        self.last_rx = time.ticks_ms()
        if match_no != self.match_no:
            self.match_no = match_no
            self._clear_scoreboard()
        self.op_y = self._clamp_paddle(hy)
        self.bx = self.oled.width - bx - BALL
        self.by = by
        self.score_l, self.score_r = sl, sr
        self.time_left = t
        if new_phase != self.phase:
            self._set_phase(new_phase)
        self._dirty = True

    def _on_client(self, match_no, paddle_y, rematch):
        if self.is_host is not True:
            return
        self.last_rx = time.ticks_ms()
        self.op_y = self._clamp_paddle(paddle_y)
        if rematch and self.phase == "over" and match_no == self.match_no:
            self._new_match()
        self._dirty = True

    # ---------- loops ----------
    def _tick(self, now):
        self.link.tick(now)
        if self.link.dirty:
            self.link.dirty = False
            self._dirty = True
        if self.phase in ("count", "play", "over", "lost"):
            self._tick_match(now)
        if self._dirty and (self.phase not in ("count", "play") or
                            time.ticks_diff(now, self.last_render) >= RENDER_MS):
            self.render()
            self._dirty = False
            self.last_render = now

    def _tick_match(self, now):
        lost = time.ticks_diff(now, self.last_rx) >= LOST_TIMEOUT_MS
        if lost and self.phase != "lost":
            self._pre_lost_phase = self.phase
            self._set_phase("lost")
            self._dirty = True
        elif not lost and self.phase == "lost":
            self._set_phase(self._pre_lost_phase)
            self._dirty = True
        if self.is_host:
            if lost:
                return                       # frozen: nothing to advance
            if self.phase == "count":
                if time.ticks_diff(now, self.count_start) >= COUNTDOWN_MS:
                    self._set_phase("play")
                    self.play_start = now
                    self._serve()
                    self._dirty = True
            elif self.phase == "play":
                self._physics(now)
            if self.phase in ("count", "play", "over") and \
                    time.ticks_diff(now, self.last_state_tx) >= STATE_MS:
                self._send_state()
        else:
            due = time.ticks_diff(now, self.last_client_tx) >= CLIENT_MS
            changed = self.paddle_y != self.last_sent_paddle and \
                time.ticks_diff(now, self.last_client_tx) >= CLIENT_CHANGE_MS
            if due or changed:
                self._send_client()
        if self.phase in ("count", "play") and \
                time.ticks_diff(now, self.last_render) >= RENDER_MS:
            self._dirty = True

    async def _loop(self):
        try:
            while self.running:
                self._tick(time.ticks_ms())
                await asyncio.sleep_ms(TICK_MS)
        except asyncio.CancelledError:
            return

    async def _rx(self):
        try:
            while self.running:
                self.link.poll(time.ticks_ms())
                await asyncio.sleep_ms(10)
        except asyncio.CancelledError:
            return

    async def _stop(self):
        self.running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.sleep_ms(0)
        self.link.close()

    # ---------- drawing ----------
    def _center6(self, text, y):
        w = self.wri6.stringlen(text)
        self.wri6.set_textpos(self.oled, y, max(0, (self.oled.width - w) // 2))
        self.wri6.printstring(text)

    def render(self):
        oled = self.oled
        oled.fill(0)
        if self.phase == "lobby":
            self.link.render(oled, "PONG")
        elif self.phase == "clash":
            self._center6("Same badge ID", 16)
            self._center6("on both badges", 30)
            self._center6("BACK=Exit", 48)
        elif self.phase == "lost":
            self._center6("Link lost", 16)
            self._center6("Reconnecting...", 32)
            self._center6("SELECT=Lobby  BACK=Exit", 50)
        elif self.phase == "count":
            self._center6("PONG", 0)
            n = 3 - time.ticks_diff(time.ticks_ms(), self.count_start) // 1000
            self.wri20.set_textpos(oled, 24, 56)
            self.wri20.printstring(str(max(1, n)))
        elif self.phase in ("play", "over"):
            self._draw_field()
            if self.phase == "over":
                self._overlay_over()
        oled.show()

    def _draw_field(self):
        oled = self.oled
        me, op = self._scores()
        self.wri6.set_textpos(oled, 0, 0)
        self.wri6.printstring("ME {:d} {:02d}s OP {:d}".format(me, self.time_left, op))
        oled.hline(0, self.hud_h - 1, oled.width, 1)
        oled.hline(0, self.pf_bot, oled.width, 1)
        for y in range(self.pf_top, self.pf_bot - 1, 4):
            oled.vline(oled.width // 2, y, 2, 1)
        oled.fill_rect(self.pl_x, self.paddle_y, PADDLE_W, PADDLE_H, 1)
        oled.fill_rect(self.pr_x, self.op_y, PADDLE_W, PADDLE_H, 1)
        oled.fill_rect(int(self.bx), int(self.by), BALL, BALL, 1)

    def _overlay_over(self):
        oled = self.oled
        me, op = self._scores()
        if me > op:
            title = "YOU WIN"
        elif me < op:
            title = "YOU LOSE"
        else:
            title = "DRAW"
        lines = [title, "{:d}:{:d}".format(me, op), "SELECT=Rematch", "BACK=Exit"]
        pad, gap = 2, 1
        fh = self.wri6.font.height()
        maxw = max(self.wri6.stringlen(s) for s in lines)
        box_w = min(oled.width, maxw + 2 * pad)
        box_h = 4 * fh + 3 * gap + 2 * pad
        x = (oled.width - box_w) // 2
        y = max(0, (oled.height - box_h) // 2)
        oled.fill_rect(x, y, box_w, box_h, 0)
        oled.rect(x, y, box_w, box_h, 1)
        ty = y + pad
        for s in lines:
            tw = self.wri6.stringlen(s)
            self.wri6.set_textpos(oled, ty, max(0, x + (box_w - tw) // 2))
            self.wri6.printstring(s)
            ty += fh + gap

    # ---------- input ----------
    async def handle_button(self, btn):
        if self.phase == "lobby" and self.link.handle_button(btn):
            return self
        if btn == BTN_BACK:
            await self._stop()
            return bsides.GamesScreen(self.oled)

        if self.phase == "lost":
            if btn == BTN_SELECT:
                self.link.leave()
            return self
        if self.phase in ("count", "play"):
            if btn == BTN_NEXT:
                self.paddle_y = self._clamp_paddle(self.paddle_y - PADDLE_STEP)
                self._dirty = True
            elif btn == BTN_SELECT:
                self.paddle_y = self._clamp_paddle(self.paddle_y + PADDLE_STEP)
                self._dirty = True
        elif self.phase == "over" and btn == BTN_SELECT:
            if self.is_host:
                self._new_match()
            else:
                self.want_rematch = True
                self._send_client()
        return self


# Contract used by game_loader (GAME_NAME is read as a string literal).
GameScreen = PongScreen
