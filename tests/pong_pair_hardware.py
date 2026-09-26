"""Run concurrently on two badges wired TX/RX/GND together.

    mpremote connect COM14 run tests/pong_pair_hardware.py
    mpremote connect COM31 run tests/pong_pair_hardware.py

Two badges on a cable pair as soon as badge_link hears the advert, with no
lobby button presses needed. Each side reports transitions and fails if its
link is reported lost during one match.
"""

import sys
import time
import uasyncio as asyncio
from machine import I2C, Pin

import ssd1306
from badge_config import load_badge_config


class BadgeStub:
    device_id = load_badge_config()["device_id"]
    BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4
    GamesScreen = None


sys.modules["bsides"] = BadgeStub()
from games import pong

LINK_TIMEOUT_MS = 15000
MATCH_TIMEOUT_MS = 90000                # countdown + a full match + margin


async def main():
    oled = ssd1306.SSD1306_I2C(128, 64, I2C(0, scl=Pin(1), sda=Pin(0)))
    game = pong.PongScreen(oled)
    started = time.ticks_ms()
    previous = None
    try:
        while game.phase == "lobby":
            if time.ticks_diff(time.ticks_ms(), started) > LINK_TIMEOUT_MS:
                raise RuntimeError("cable did not pair (lobby %s)" %
                                  game.link.state)
            await asyncio.sleep_ms(100)
        print("Pong %s: paired as %s, peer %s" %
              (game.my_id, "host" if game.is_host else "guest", game.peer_id))
        while game.phase != "over":
            now = time.ticks_ms()
            if time.ticks_diff(now, started) > MATCH_TIMEOUT_MS:
                raise RuntimeError("match stuck at phase %s" % game.phase)
            if game.phase != previous:
                print("Pong %s: %s at %dms rx_age=%dms" %
                      (game.my_id, game.phase, time.ticks_diff(now, started),
                       time.ticks_diff(now, game.last_rx)))
                if game.phase == "lost":
                    raise RuntimeError("link reported lost during the match")
                previous = game.phase
            await asyncio.sleep_ms(100)
        print("Pong %s: match complete, %d:%d after %dms" %
              (game.my_id, game.score_l, game.score_r,
               time.ticks_diff(time.ticks_ms(), started)))
    finally:
        await game._stop()


asyncio.run(main())
