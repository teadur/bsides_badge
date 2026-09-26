"""Run concurrently on two badges to test Pong over ESP-NOW only.

    mpremote connect /dev/ttyACM0 run tests/pong_wifi_pair_hardware.py
    mpremote connect /dev/ttyACM1 run tests/pong_wifi_pair_hardware.py

No cable is needed: the cable is ignored, so every frame has to come over
Wi-Fi. Both badges join the lobby under the name TEST_NAME and only pair
with each other: the one with the higher ID invites, the other accepts, as
players would with the buttons. They then let one full match play out
(paddles left where they start) and check it reaches the final score
screen without the link ever being reported lost. Each side prints PASS or
FAIL; the link log (python scripts/badge.py logs) has the full traffic.
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
    USERNAME = "pong-wifi-test"


TEST_NAME = BadgeStub.USERNAME


sys.modules["bsides"] = BadgeStub()
import badge_link
import espnow_link
from games import pong

badge_link.CABLE = False                # Wi-Fi only, even if a cable is on

LINK_TIMEOUT_MS = 45000                 # room for one unanswered invitation
MATCH_TIMEOUT_MS = 90000                # countdown + a full match + margin


def say(text):
    print("PONG-WIFI %s: %s" % (BadgeStub.device_id, text))


async def pair_in_lobby(game, started):
    """Invite or accept the other test badge, pressing buttons as a player."""
    link = game.link
    while game.phase == "lobby":
        if time.ticks_diff(time.ticks_ms(), started) > LINK_TIMEOUT_MS:
            raise RuntimeError("no link within %d ms (lobby %s)" %
                              (LINK_TIMEOUT_MS, link.state))
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
    game = pong.PongScreen(oled)
    started = time.ticks_ms()
    previous = None
    try:
        if game.link.esp is None:
            raise RuntimeError("ESP-NOW did not start; see the link log")
        await pair_in_lobby(game, started)
        say("paired as %s, peer %s" %
            ("host" if game.is_host else "guest", game.peer_id))

        while game.phase != "over":
            now = time.ticks_ms()
            if time.ticks_diff(now, started) > MATCH_TIMEOUT_MS:
                raise RuntimeError("match stuck at phase %s" % game.phase)
            if game.phase != previous:
                say("phase -> %s at %d ms" %
                    (game.phase, time.ticks_diff(now, started)))
                if game.phase == "lost":
                    raise RuntimeError("link reported lost during the match")
                previous = game.phase
            await asyncio.sleep_ms(100)

        say("match over: %d:%d after %d ms" %
            (game.score_l, game.score_r,
             time.ticks_diff(time.ticks_ms(), started)))
        say("PASS")
    except Exception as exc:
        say("FAIL: %s" % exc)
    finally:
        await game._stop()
        say("after stop, " + espnow_link.memory())


asyncio.run(main())
