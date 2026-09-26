"""List games without importing them, and keep at most one game loaded.

Importing every game just to show its name kept all of them compiled in RAM,
which left the Wi-Fi driver too little memory to start ESP-NOW in a game.
"""

import gc
import os
import sys


GAMES_FOLDER = "games"


def read_game_name(path):
    """Return the string literal assigned to GAME_NAME, or None."""
    with open(path) as f:
        for line in f:
            if line.startswith("GAME_NAME"):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] and \
                        value[0] in "\"'":
                    return value[1:-1]
                return None
    return None


def discover(folder=GAMES_FOLDER):
    """Return (display name, module name) pairs for folder/*.py."""
    games = []
    try:
        filenames = sorted(os.listdir(folder))
    except OSError as exc:
        print("Cannot scan games directory:", exc)
        return games
    for filename in filenames:
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        try:
            name = read_game_name(folder + "/" + filename)
        except OSError as exc:
            print("Cannot read game {}: {}".format(filename, exc))
            continue
        if name is None:
            print("Cannot list game {}: no GAME_NAME string".format(filename))
            continue
        games.append((name, filename[:-3]))
    return games


def unload_games(package=GAMES_FOLDER):
    # The package object keeps each imported game as an attribute, so drop
    # the package too, or the modules stay reachable.
    for key in list(sys.modules):
        if key == package or key.startswith(package + "."):
            del sys.modules[key]
    gc.collect()


def load_game(module_name, package=GAMES_FOLDER):
    """Unload any other game, then import this one and return GameScreen."""
    unload_games(package)
    module = __import__(package + "." + module_name, None, None,
                        ("GameScreen",))
    return module.GameScreen
