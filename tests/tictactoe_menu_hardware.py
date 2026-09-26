"""Run concurrently on two badges: tic-tac-toe over Wi-Fi, started from the menu.

    mpremote connect /dev/ttyACM0 run tests/tictactoe_menu_hardware.py
    mpremote connect /dev/ttyACM1 run tests/tictactoe_menu_hardware.py

tictactoe_wifi_pair_hardware.py starts the game by itself. This test instead
starts the normal application (main.py, which loads the whole menu UI: the
state in which Wi-Fi ran out of memory) and drives it with simulated button
presses, Games -> Tic-tac-toe, then plays the same scripted game: each side
takes the first empty cell on its turn, so the board must end XOXOXOX-- won
by X. The cable receiver is ignored, so every frame has to come over Wi-Fi.
Each badge prints MENU-TEST <id>: PASS or FAIL; the same lines go to its
link log.
"""

import gc
import sys
import time
import uasyncio as asyncio

STEP_MS = 250            # lets the UI handle one press before the next
LINK_TIMEOUT_MS = 40000
GAME_TIMEOUT_MS = 60000
EXPECTED = "XOXOXOX--"


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
        try:
            await asyncio.sleep_ms(500)
            game = await self.open_game()
            await self.play(game)
            self.say("PASS")
        except Exception as exc:
            self.say("FAIL: %s" % exc)
        await self.leave_game()
        self.say("after leaving the game, " + self.memory())
        linklog.flush()
        raise Finished()

    def say(self, text):
        print("MENU-TEST %s: %s" % (self.ui.device_id, text))
        self.linklog.log("menu", text)

    def memory(self):
        gc.collect()
        return self.espnow_link.memory()

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

    async def open_game(self):
        ui = self.ui
        await self.press(ui.BTN_BACK)       # wakes the UI on its main menu
        if not isinstance(ui.screen, ui.MenuScreen):
            raise RuntimeError("main menu did not open")
        await self.choose([item[0] for item in ui.MenuScreen.items], "Games")
        if not isinstance(ui.screen, ui.GamesScreen):
            raise RuntimeError("Games menu did not open")
        names = [name for name, _ in ui.screen.games]
        self.say("games menu lists %s; %s" % (", ".join(names), self.memory()))
        await self.choose(names, "Tic-tac-toe")
        game = ui.screen
        if type(game).__name__ != "TicTacToeScreen":
            raise RuntimeError("Tic-tac-toe did not open")
        self.say("game loaded from " + sys.modules["games.tictactoe"].__file__)
        game._poll_uart = lambda: None      # Wi-Fi only, even with a cable
        if game.espnow is None:
            raise RuntimeError("ESP-NOW did not start; see the link log")
        return game

    async def play(self, game):
        ttt = sys.modules["games.tictactoe"]
        start = time.ticks_ms()
        while game.phase != "play" or not game.game_no:
            if time.ticks_diff(time.ticks_ms(), start) > LINK_TIMEOUT_MS:
                raise RuntimeError("no link within %d ms (phase %s)" % (
                    LINK_TIMEOUT_MS, game.phase))
            if game.phase == "nolink":
                await self.press(self.ui.BTN_SELECT)    # "SELECT=Retry"
            await asyncio.sleep_ms(100)
        self.say("linked in %d ms as %s (%s), peer %s" % (
            time.ticks_diff(time.ticks_ms(), start), game.my_mark(),
            "host" if game.is_host else "guest", game.peer_id))

        start = time.ticks_ms()
        while ttt.winner(game.board)[0] == ttt.EMPTY:
            if time.ticks_diff(time.ticks_ms(), start) > GAME_TIMEOUT_MS:
                raise RuntimeError("game stuck at " + "".join(game.board))
            if game.phase != "play":
                raise RuntimeError("link dropped: phase " + game.phase)
            if game.turn == game.my_mark() and \
                    (game.is_host or game.pending is None):
                game.cursor = game.board.index(ttt.EMPTY)
                self.say("placing %s on cell %d" % (game.my_mark(), game.cursor))
                await self.press(self.ui.BTN_SELECT)
            else:
                await asyncio.sleep_ms(100)
        await asyncio.sleep_ms(2000)        # heartbeat lets the peer see the end

        board = "".join(game.board)
        result = ttt.winner(game.board)[0]
        self.say("final board %s, winner %s, %d ms" % (
            board, result, time.ticks_diff(time.ticks_ms(), start)))
        if board != EXPECTED or result != "X":
            raise RuntimeError("expected %s won by X" % EXPECTED)

    async def leave_game(self):
        """BACK out of the game, which turns the radio off, as a player would."""
        if type(self.ui.screen).__name__ == "TicTacToeScreen":
            await self.press(self.ui.BTN_BACK)


sys.modules["badge_test_driver"] = MenuDriver()
try:
    import main         # the normal start-up: Wi-Fi reserve, then the menu UI
except Finished:
    pass
finally:
    del sys.modules["badge_test_driver"]
