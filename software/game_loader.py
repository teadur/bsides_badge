"""List games without importing them, and keep at most one game loaded.

Importing every game just to show its name kept all of them loaded in RAM,
which left the Wi-Fi driver too little memory to start ESP-NOW in a game.
"""

import gc
import json
import os
import sys


GAMES_FOLDER = "games"
INDEX = "index.json"      # {module: name}, written by the upload tool


def module_files(folder):
    """Return {module name: ".py" or ".mpy"} for the modules in folder.

    When both files exist, import loads the .py, so that one is reported."""
    found = {}
    for filename in os.listdir(folder):
        if filename.endswith(".mpy"):
            found.setdefault(filename[:-4], ".mpy")
        elif filename.endswith(".py"):
            found[filename[:-3]] = ".py"
    return found


def read_index(folder):
    try:
        with open(folder + "/" + INDEX) as f:
            index = json.load(f)
    except (OSError, ValueError) as exc:
        print("Cannot read games index:", exc)
        return {}
    return index if isinstance(index, dict) else {}


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
    """Return (display name, module name) pairs for the games in folder.

    A game uploaded as source is named by its GAME_NAME line. A precompiled
    .mpy has no readable source, so its name comes from the index."""
    games = []
    try:
        modules = module_files(folder)
    except OSError as exc:
        print("Cannot scan games directory:", exc)
        return games
    index = None
    for module in sorted(modules):
        if module.startswith("_"):
            continue
        filename = module + modules[module]
        if modules[module] == ".mpy":
            if index is None:
                index = read_index(folder)
            name = index.get(module)
            reason = "not in " + INDEX
        else:
            try:
                name = read_game_name(folder + "/" + filename)
            except OSError as exc:
                print("Cannot read game {}: {}".format(filename, exc))
                continue
            reason = "no GAME_NAME string"
        if name is None:
            print("Cannot list game {}: {}".format(filename, reason))
            continue
        games.append((name, module))
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
