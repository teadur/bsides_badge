"""Run on one badge: open every single-player game from the menu in turn.

    mpremote connect /dev/ttyACM0 run tests/singleplayer_menu_hardware.py

Starts the normal application (main.py: Wi-Fi reserve, then the whole menu
UI) and drives it with simulated button presses: Games -> each single-player
game in turn (Flappy Bird, Pacman, Snake, Tetris), a few seconds of input to
make sure it runs and answers the buttons, then BACK. No second badge or
Wi-Fi link is needed - this only checks that each game loads, plays, and
gives its memory back when unloaded, unlike Pong and Tic-tac-toe's own menu
tests which check the link. Prints SOLO-TEST <id>: PASS or FAIL; the same
lines go to the link log, including free memory before the first game and
after every game leaves.
"""

import gc
import sys
import uasyncio as asyncio

STEP_MS = 250            # lets the UI handle one press before the next

# Each game gets a short, game-specific button sequence: buttons that move
# it out of its start screen and keep it answering input, not a scripted
# playthrough. NEXT/PREV/SELECT are 1/2/3 (BTN_NEXT etc; see open_game()).
BUTTON_SEQUENCE = {
    "Flappy Bird": [3, 3, 3, 3, 3, 3, 3, 3],       # SELECT: flap, flap, ...
    "Pacman": [1, 1, 2, 2, 1, 1, 2, 2],            # NEXT/PREV: turn around
    "Snake": [1, 1, 2, 2, 1, 1, 2, 2],              # NEXT/PREV: turn
    "Tetris": [1, 2, 3, 1, 2, 3, 1, 2],             # NEXT/PREV/SELECT
}
GAMES = list(BUTTON_SEQUENCE)


class Finished(Exception):
    """Stops the application once the test is over."""


class MenuDriver:
    """Presses buttons through bsides' own UI loop, like a player would."""

    async def run(self):
        import bsides
        import espnow_link
        import linklog
        self.ui = bsides
        self.linklog = linklog
        self.espnow_link = espnow_link
        # The link log's in-memory ring buffer is capped, but filling it up
        # still costs a little memory the first time; do that once, up
        # front, so it does not look like a leak in the games below.
        for i in range(linklog.RING_LINES):
            linklog.log("menu", "priming the link log ring buffer", i)
        try:
            await asyncio.sleep_ms(500)
            await self.check_every_game()
            self.say("PASS")
        except Exception as exc:
            self.say("FAIL: %s" % exc)
        linklog.flush()
        raise Finished()

    def say(self, text):
        print("SOLO-TEST %s: %s" % (self.ui.device_id, text))
        self.linklog.log("menu", text)

    def memory(self):
        gc.collect()
        return self.espnow_link.memory()

    def mem_free(self):
        gc.collect()
        return gc.mem_free()

    async def press(self, btn):
        self.ui._push_button(btn)
        await asyncio.sleep_ms(STEP_MS)

    async def choose(self, names, wanted):
        """Move the current list to wanted the short way round, then SELECT."""
        target = names.index(wanted)
        steps = (target - self.ui.screen.index) % len(names)
        btn = self.ui.BTN_NEXT
        if steps > len(names) // 2:
            btn, steps = self.ui.BTN_PREV, len(names) - steps
        for _ in range(steps):
            await self.press(btn)
        if self.ui.screen.index != target:
            raise RuntimeError("could not move to " + wanted)
        await self.press(self.ui.BTN_SELECT)

    async def open_games_menu(self):
        ui = self.ui
        await self.press(ui.BTN_BACK)       # wakes the UI on its main menu
        if not isinstance(ui.screen, ui.MenuScreen):
            raise RuntimeError("main menu did not open")
        await self.choose([item[0] for item in ui.MenuScreen.items], "Games")
        if not isinstance(ui.screen, ui.GamesScreen):
            raise RuntimeError("Games menu did not open")

    async def check_every_game(self):
        await self.open_games_menu()
        names = [name for name, _ in self.ui.screen.games]
        self.say("games menu lists %s; %s" % (", ".join(names), self.memory()))
        present = [g for g in GAMES if g in names]
        missing = [g for g in GAMES if g not in names]
        if missing:
            self.say("not on this badge, skipping: " + ", ".join(missing))

        # The first couple of visits to each game cost a little one-time
        # memory (module caches, interned strings, first-use allocations)
        # that settles rather than comes back; that is normal. Once it has
        # settled, a further lap must not cost anything more - that would
        # be an actual per-visit leak, repeating every time a player opens
        # that game.
        for _ in range(2):
            for name in present:
                await self.play_one(name, names)
        start_free = self.mem_free()
        for name in present:
            await self.play_one(name, names)
        end_free = self.mem_free()
        self.say("mem_free %d before a settled lap, %d after it" %
                 (start_free, end_free))
        if end_free < start_free - 512:
            raise RuntimeError("memory did not come back after a lap "
                               "through every game (%d -> %d)" %
                               (start_free, end_free))

    async def play_one(self, name, names):
        ui = self.ui
        if not isinstance(ui.screen, ui.GamesScreen):
            await self.open_games_menu()
        await self.choose(names, name)
        screen = ui.screen
        if type(screen).__name__ != "GameScreen":
            raise RuntimeError(name + " did not open")
        before = self.mem_free()
        self.say(name + " loaded, " + self.memory())
        for btn in BUTTON_SEQUENCE[name]:
            if ui.screen is not screen:
                raise RuntimeError(name + " screen changed unexpectedly")
            await self.press(btn)
        await asyncio.sleep_ms(500)         # let its own loop run a bit more
        if ui.screen is not screen:
            raise RuntimeError(name + " screen changed unexpectedly")
        await self.press(ui.BTN_BACK)
        if not isinstance(ui.screen, ui.GamesScreen):
            raise RuntimeError(name + " did not return to the Games menu")
        after = self.mem_free()
        self.say(name + " left, " + self.memory())
        if after < before - 2048:
            raise RuntimeError(name + " leaked memory (%d -> %d)" %
                               (before, after))


sys.modules["badge_test_driver"] = MenuDriver()
try:
    import main         # the normal start-up: Wi-Fi reserve, then the menu UI
except Finished:
    pass
finally:
    del sys.modules["badge_test_driver"]
