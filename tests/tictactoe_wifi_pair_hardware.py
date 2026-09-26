"""Run concurrently on two badges to test tic-tac-toe over ESP-NOW only.

    mpremote connect /dev/ttyACM0 run tests/tictactoe_wifi_pair_hardware.py
    mpremote connect /dev/ttyACM1 run tests/tictactoe_wifi_pair_hardware.py

No cable is needed: the cable is ignored, so every frame has to come over
Wi-Fi. Both badges join the lobby under the name TEST_NAME and only pair
with each other: the one with the higher ID invites, the other accepts, as
players would with the buttons. Then they play one scripted game in which
each side takes the first empty cell on its turn. X starts, so both must end on the
board XOXOXOX-- with X winning. Each side prints PASS or FAIL; the link log
(python scripts/badge.py logs) has the full traffic.
"""

import sys
import time
import uasyncio as asyncio
from machine import I2C, Pin

import ssd1306
from badge_config import hardware_for, load_badge_config
from writer.writer import Writer
import writer.font6 as font6

config = load_badge_config()
oled = ssd1306.SSD1306_I2C(
    128, 64, I2C(0, scl=Pin(1), sda=Pin(0)),
    addr=hardware_for(config["badge_version"])["oled_address"])


class BadgeStub:
    device_id = config["device_id"]
    BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4
    wri6 = Writer(oled, font6, verbose=False)
    GamesScreen = None
    USERNAME = "ttt-wifi-test"


TEST_NAME = BadgeStub.USERNAME


sys.modules["bsides"] = BadgeStub()
import badge_link
import espnow_link
from games import tictactoe

badge_link.CABLE = False                # Wi-Fi only, even if a cable is on

LINK_TIMEOUT_MS = 45000                 # room for one unanswered invitation
GAME_TIMEOUT_MS = 60000
EXPECTED = list("XOXOXOX--")


def say(text):
    print("TTT-WIFI %s: %s" % (BadgeStub.device_id, text))


async def pair_in_lobby(game, started):
    """Invite or accept the other test badge, pressing buttons as a player."""
    link = game.link
    while game.phase != "play" or not game.game_no:
        if time.ticks_diff(time.ticks_ms(), started) > LINK_TIMEOUT_MS:
            raise RuntimeError("no link within %d ms (phase %s, lobby %s)" % (
                LINK_TIMEOUT_MS, game.phase, link.state))
        if link.state == "lobby":
            players = link.listed()
            for i, p in enumerate(players):
                if p.name == TEST_NAME and p.id < link.my_id:
                    say("inviting %s" % p.id)
                    link.index = i
                    await game.handle_button(BadgeStub.BTN_SELECT)
                    break
        elif link.state == "invited":
            inviter = link.players.get(link.target)
            if inviter is not None and inviter.name == TEST_NAME:
                say("accepting %s" % inviter.id)
                await game.handle_button(BadgeStub.BTN_SELECT)
            else:
                await game.handle_button(BadgeStub.BTN_NEXT)    # decline
        await asyncio.sleep_ms(100)


async def main():
    say("before start, " + espnow_link.memory())
    game = tictactoe.TicTacToeScreen(oled)
    started = time.ticks_ms()
    try:
        if game.link.esp is None:
            raise RuntimeError("ESP-NOW did not start; see the link log")
        await pair_in_lobby(game, started)
        say("linked in %d ms as %s (%s), peer %s" % (
            time.ticks_diff(time.ticks_ms(), started), game.my_mark(),
            "host" if game.is_host else "guest", game.peer_id))

        played = time.ticks_ms()
        while tictactoe.winner(game.board)[0] == tictactoe.EMPTY:
            if time.ticks_diff(time.ticks_ms(), played) > GAME_TIMEOUT_MS:
                raise RuntimeError("game stuck at %s" % "".join(game.board))
            if game.phase != "play":
                raise RuntimeError("link dropped: phase %s" % game.phase)
            mine = game.turn == game.my_mark()
            if mine and (game.is_host or game.pending is None):
                game.cursor = game.board.index(tictactoe.EMPTY)
                say("placing %s on cell %d" % (game.my_mark(), game.cursor))
                await game.handle_button(BadgeStub.BTN_SELECT)
            await asyncio.sleep_ms(100)
        # Keep the heartbeat running so the other badge also sees the end.
        await asyncio.sleep_ms(2000)

        board = "".join(game.board)
        result = tictactoe.winner(game.board)[0]
        say("final board %s, winner %s, %d ms" % (
            board, result, time.ticks_diff(time.ticks_ms(), played)))
        if game.board != EXPECTED or result != "X":
            raise RuntimeError("expected XOXOXOX-- won by X")
        say("PASS")
    except Exception as exc:
        say("FAIL: %s" % exc)
    finally:
        await game._stop()
        say("after stop, " + espnow_link.memory())


asyncio.run(main())
