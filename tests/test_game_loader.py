import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "game_loader_test", ROOT / "software" / "game_loader.py")
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)


class ReadGameNameTests(unittest.TestCase):
    def name_of(self, source):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "g.py"
            path.write_text(source, encoding="utf-8")
            return loader.read_game_name(str(path))

    def test_reads_double_and_single_quoted_literals(self):
        self.assertEqual(self.name_of('import x\nGAME_NAME = "Tic-tac-toe"\n'),
                         "Tic-tac-toe")
        self.assertEqual(self.name_of("GAME_NAME='Snake'  \n"), "Snake")

    def test_rejects_non_literals_and_missing_names(self):
        self.assertIsNone(self.name_of("GAME_NAME = make_name()\n"))
        self.assertIsNone(self.name_of("NAME = 'x'\n"))
        self.assertIsNone(self.name_of("    GAME_NAME = 'nested'\n"))


class RealGamesTests(unittest.TestCase):
    def test_every_bundled_game_is_listed_without_importing_it(self):
        before = set(sys.modules)
        games = loader.discover(str(ROOT / "software" / "games"))
        self.assertEqual(games, [
            ("Flappy Bird", "flappy"), ("Pacman", "pacman"), ("Pong", "pong"),
            ("Snake", "snake"), ("Tetris", "tetris"),
            ("Tic-tac-toe", "tictactoe")])
        self.assertFalse({m for m in set(sys.modules) - before
                          if "games" in m})


class LoadGameTests(unittest.TestCase):
    PACKAGE = "fake_games_pkg"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        pkg = Path(self.dir.name) / self.PACKAGE
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        for name in ("alpha", "beta"):
            (pkg / (name + ".py")).write_text(
                "GAME_NAME = '{0}'\nclass GameScreen:\n    tag = '{0}'\n"
                .format(name), encoding="utf-8")
        (pkg / "broken.py").write_text("GAME_NAME = 'x'\nraise ValueError\n",
                                       encoding="utf-8")
        sys.path.insert(0, self.dir.name)

    def tearDown(self):
        sys.path.remove(self.dir.name)
        loader.unload_games(self.PACKAGE)
        self.dir.cleanup()

    def test_loading_a_game_unloads_the_previous_one(self):
        alpha = loader.load_game("alpha", self.PACKAGE)
        self.assertEqual(alpha.tag, "alpha")
        self.assertIn(self.PACKAGE + ".alpha", sys.modules)
        beta = loader.load_game("beta", self.PACKAGE)
        self.assertEqual(beta.tag, "beta")
        self.assertNotIn(self.PACKAGE + ".alpha", sys.modules)
        self.assertFalse(hasattr(sys.modules[self.PACKAGE], "alpha"))

    def test_unload_drops_the_package_and_all_games(self):
        loader.load_game("alpha", self.PACKAGE)
        loader.unload_games(self.PACKAGE)
        self.assertFalse([m for m in sys.modules if m.startswith(self.PACKAGE)])

    def test_import_errors_reach_the_caller(self):
        with self.assertRaises(ValueError):
            loader.load_game("broken", self.PACKAGE)


if __name__ == "__main__":
    unittest.main()
