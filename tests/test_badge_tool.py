import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("badge_tool", ROOT / "scripts" / "badge.py")
badge_tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = badge_tool
SPEC.loader.exec_module(badge_tool)


class BadgeToolTests(unittest.TestCase):
    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_2026_battery_voltage_line(self, _prefix, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "BADGE_BATTERY_VOLTAGE=4.087\r\n"
        line = badge_tool.battery_voltage_line(
            "COM10", {"badge_version": "2026"})
        self.assertEqual(line, "Battery voltage: 4.087 V")

    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_2026_battery_voltage_warning_range(self, _prefix, run):
        cases = (
            ("3.799", True),
            ("3.800", False),
            ("4.200", False),
            ("4.201", True),
        )
        for voltage, should_warn in cases:
            with self.subTest(voltage=voltage):
                run.return_value.returncode = 0
                run.return_value.stdout = (
                    "BADGE_BATTERY_VOLTAGE={}\r\n".format(voltage)
                )
                line = badge_tool.battery_voltage_line(
                    "COM10", {"badge_version": "2026"})
                self.assertEqual("WARNING!!" in line, should_warn)

    @patch.object(badge_tool, "run")
    def test_non_2026_badge_skips_battery_measurement(self, run):
        line = badge_tool.battery_voltage_line(
            "COM10", {"badge_version": "2025"})
        self.assertIsNone(line)
        run.assert_not_called()

    @patch("builtins.print")
    @patch.object(badge_tool, "build_parser")
    def test_battery_voltage_is_last_output_line(self, build_parser, print_mock):
        args = Namespace(handler=lambda _args: "Battery voltage: 4.100 V")
        build_parser.return_value.parse_args.return_value = args
        self.assertEqual(badge_tool.main([]), 0)
        self.assertEqual(print_mock.call_args_list[-1],
                         call("Battery voltage: 4.100 V"))

    @patch.object(badge_tool.shutil, "which")
    @patch.object(badge_tool.importlib.util, "find_spec")
    def test_tool_command_prefers_importable_module(self, find_spec, which):
        find_spec.return_value = object()
        self.assertEqual(badge_tool.tool_command("mpremote"),
                         [sys.executable, "-m", "mpremote"])
        which.assert_not_called()

    @patch.object(badge_tool.shutil, "which", return_value="/usr/bin/mpremote")
    @patch.object(badge_tool.importlib.util, "find_spec", return_value=None)
    def test_tool_command_falls_back_to_path(self, _find_spec, _which):
        self.assertEqual(badge_tool.tool_command("mpremote"),
                         ["/usr/bin/mpremote"])

    def test_parse_latest_stable_firmware(self):
        page = """
        <a href="/resources/firmware/ESP32_GENERIC_C3-20250911-v1.26.1.bin">old</a>
        <a href="/resources/firmware/ESP32_GENERIC_C3-20260824-v1.29.0.bin">latest</a>
        <a href="/resources/firmware/ESP32_GENERIC_C3-20260919-v1.30.0-preview.1.bin">preview</a>
        """
        firmware = badge_tool.parse_latest_firmware(page)
        self.assertEqual(firmware.version, "1.29.0")
        self.assertEqual(firmware.date, "20260824")
        self.assertTrue(firmware.url.endswith("ESP32_GENERIC_C3-20260824-v1.29.0.bin"))

    def test_upload_filter(self):
        files = {path.relative_to(badge_tool.SOFTWARE_DIR).as_posix()
                 for path in badge_tool.upload_files()}
        self.assertIn("main.py", files)
        self.assertIn("lib/ssd1306.py", files)
        self.assertIn("certs/isrg-root-x1.pem", files)
        self.assertIn("wifi_fetch.py", files)
        self.assertNotIn("badge.json", files)
        self.assertFalse(any("__pycache__" in path or path.endswith(".pyc") for path in files))

    def test_upload_filter_excludes_macos_metadata_and_requirements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            software_dir = Path(temp_dir)
            (software_dir / ".DS_Store").write_bytes(b"metadata")
            (software_dir / "requirements.txt").write_text(
                "mpremote\n", encoding="utf-8")
            nested = software_dir / "games"
            nested.mkdir()
            (nested / ".DS_Store").write_bytes(b"metadata")
            (nested / "game.py").write_text("pass\n", encoding="utf-8")
            with patch.object(badge_tool, "SOFTWARE_DIR", software_dir):
                files = {path.relative_to(software_dir).as_posix()
                         for path in badge_tool.upload_files()}
            self.assertEqual(files, {"games/game.py"})

    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_removes_remote_sponsor_logos_recursively(self, _prefix, run):
        badge_tool.remove_remote_sponsor_logos("/dev/cu.usbmodem1")
        run.assert_called_once_with(
            ["mpremote", "fs", "rm", "-r", ":/logos"],
            check=False, capture=True, timeout=20)

    def test_clean_bytecode_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            software_dir = Path(temp_dir)
            cache_dir = software_dir / "lib" / "__pycache__"
            cache_dir.mkdir(parents=True)
            (cache_dir / "module.pyc").write_bytes(b"cache")
            (software_dir / "orphan.pyo").write_bytes(b"cache")
            (software_dir / "main.py").write_text("pass\n", encoding="utf-8")
            with patch.object(badge_tool, "SOFTWARE_DIR", software_dir):
                self.assertEqual(badge_tool.clean_bytecode_cache(), 3)
            self.assertFalse(cache_dir.exists())
            self.assertFalse((software_dir / "orphan.pyo").exists())
            self.assertTrue((software_dir / "main.py").exists())

    @patch.object(badge_tool, "write_remote_config")
    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    @patch.object(badge_tool, "clean_bytecode_cache", return_value=0)
    def test_upload_uses_one_recursive_copy(
            self, _clean, _prefix, run, _write_config):
        files = [badge_tool.SOFTWARE_DIR / "main.py",
                 badge_tool.SOFTWARE_DIR / "games" / "snake.py"]
        with patch.object(badge_tool, "upload_files", return_value=files):
            badge_tool.upload_tree("/dev/ttyACM0", {}, compile_modules=False)
        copy_commands = [call.args[0] for call in run.call_args_list
                         if "cp" in call.args[0]]
        self.assertEqual(len(copy_commands), 1)
        self.assertEqual(copy_commands[0][:4], ["mpremote", "fs", "cp", "-r"])
        self.assertEqual([Path(path).name for path in copy_commands[0][4:-1]],
                         ["games", "main.py"])
        self.assertEqual(copy_commands[0][-1], ":")
        self.assertTrue(all(Path(path).parent != badge_tool.SOFTWARE_DIR
                            for path in copy_commands[0][4:-1]))
        remove_commands = [call.args[0] for call in run.call_args_list
                           if "rm" in call.args[0]]
        self.assertIn(["mpremote", "fs", "rm", ":/bsides25.py"],
                      remove_commands)

    def test_merge_preserves_identity_and_parameters(self):
        remote = {
            "device_id": "A1B2C3D4E5F6",
            "holder_name": "Ada",
            "badge_version": "2025",
            "git_commit": "old main",
            "params": {"Brightness": 42},
        }
        result = badge_tool.merge_config(remote, "2026", None, write_git=False)
        self.assertEqual(result["device_id"], remote["device_id"])
        self.assertEqual(result["holder_name"], "Ada")
        self.assertEqual(result["badge_version"], "2026")
        self.assertEqual(result["params"]["Brightness"], 42)

    @patch.object(badge_tool, "read_remote_config")
    def test_wipe_skips_reading_existing_config(self, read_remote_config):
        self.assertEqual(badge_tool.existing_config("/dev/cu.usbmodem1", True), {})
        read_remote_config.assert_not_called()

        read_remote_config.return_value = {"holder_name": "Ada"}
        self.assertEqual(
            badge_tool.existing_config("/dev/cu.usbmodem1", False),
            {"holder_name": "Ada"},
        )
        read_remote_config.assert_called_once_with("/dev/cu.usbmodem1")

    def test_wipe_option_is_available_for_upload_and_flash(self):
        parser = badge_tool.build_parser()
        for command in ("upload", "flash"):
            with self.subTest(command=command):
                args = parser.parse_args([
                    command, "--badge-version", "2026", "--wipe"])
                self.assertTrue(args.wipe)

    @patch.object(badge_tool, "write_remote_config")
    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "remove_remote_sponsor_logos")
    @patch.object(badge_tool, "clean_bytecode_cache", return_value=0)
    def test_upload_replaces_sponsor_logos(
            self, _clean, remove_logos, _run, _write_config):
        with patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"]):
            badge_tool.upload_tree("COM10", {}, compile_modules=False)
        remove_logos.assert_called_once_with("COM10")

    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "remove_remote_sponsor_logos")
    @patch.object(badge_tool, "clean_bytecode_cache", return_value=0)
    def test_refused_upload_keeps_the_sponsor_logos(
            self, _clean, remove_logos, run):
        refused = badge_tool.BadgeToolError("mpy-cross writes .mpy v6 ...")
        with patch.object(badge_tool, "check_mpy_compatible",
                          side_effect=refused), \
                self.assertRaises(badge_tool.BadgeToolError):
            badge_tool.upload_tree("COM10", {}, compile_modules=True)
        remove_logos.assert_not_called()
        run.assert_not_called()


class HardwareDetectionTests(unittest.TestCase):
    def test_classifies_each_version_from_its_pins(self):
        classify = badge_tool.classify_hardware
        self.assertEqual(classify([0x3D], 3100), "2025_prototype")
        self.assertEqual(classify([0x3C], 3100), "2025")      # SELECT pull-up
        self.assertEqual(classify([0x3C], 2480), "2025")      # 11 dB saturates
        self.assertEqual(classify([0x3C], 500), "2026")       # 3.0 V battery
        self.assertEqual(classify([0x3C], 700), "2026")       # 4.2 V battery

    def test_ambiguous_readings_are_not_guessed(self):
        classify = badge_tool.classify_hardware
        self.assertIsNone(classify([], 3100))                 # no OLED answered
        self.assertIsNone(classify([0x3C], 20))               # SELECT held
        self.assertIsNone(classify([0x3C], 1200))

    @patch("builtins.print")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_probe_parses_the_badge_report(self, _prefix, _print):
        output = Namespace(returncode=0, stderr="",
                           stdout="BADGE_PROBE i2c=60 gpio4_mv=612\r\n")
        with patch.object(badge_tool, "run", return_value=output) as run:
            self.assertEqual(badge_tool.probe_hardware("/dev/ttyACM1"), "2026")
        self.assertEqual(run.call_args.args[0][:2], ["mpremote", "exec"])

    @patch("builtins.print")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_probe_failure_returns_none_and_shows_why(self, _prefix, print_mock):
        output = Namespace(
            returncode=1, stdout="",
            stderr="Traceback:\nmpremote: failed to access /dev/ttyACM1 "
                   "(it may be in use by another program)\n")
        with patch.object(badge_tool, "run", return_value=output):
            self.assertIsNone(badge_tool.probe_hardware("/dev/ttyACM1"))
        self.assertEqual(
            print_mock.call_args.args[0],
            "Could not probe the badge hardware: mpremote: failed to access "
            "/dev/ttyACM1 (it may be in use by another program)")

    def test_probe_code_runs_against_fake_pins(self):
        """The snippet sent to the badge, run with stand-in machine classes."""
        fake_machine = (
            "import sys, types\n"
            "m = types.ModuleType('machine')\n"
            "class Pin:\n    def __init__(self, n): pass\n"
            "class I2C:\n    def __init__(self, *a, **k): pass\n"
            "    def scan(self): return [60]\n"
            "class ADC:\n    ATTN_11DB = 3\n"
            "    def __init__(self, pin): pass\n"
            "    def atten(self, a): pass\n"
            "    def read_uv(self): return 612000\n"
            "m.Pin, m.I2C, m.ADC = Pin, I2C, ADC\n"
            "sys.modules['machine'] = m\n")
        result = subprocess.run(
            [sys.executable, "-c", fake_machine + badge_tool.PROBE_CODE],
            check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "BADGE_PROBE i2c=60 gpio4_mv=612")

    @patch("builtins.print")
    def test_version_precedence(self, print_mock):
        resolve = badge_tool.resolve_badge_version
        self.assertEqual(resolve("2025", "2026", "2026"), "2025")
        self.assertIn("hardware looks like 2026", print_mock.call_args.args[0])
        self.assertEqual(resolve(None, "2026", "2025"), "2026")
        self.assertEqual(resolve(None, None, "2025_prototype"), "2025_prototype")
        with self.assertRaises(badge_tool.BadgeToolError):
            resolve(None, None, None)

    def test_upload_and_flash_no_longer_require_a_version(self):
        parser = badge_tool.build_parser()
        for command in ("upload", "flash"):
            with self.subTest(command=command):
                self.assertIsNone(parser.parse_args([command]).badge_version)

    @patch("builtins.print")
    @patch.object(badge_tool, "upload_tree", return_value=None)
    @patch.object(badge_tool, "remove_remote_sponsor_logos")
    @patch.object(badge_tool, "existing_config",
                  return_value={"badge_version": "2025", "device_id": "A" * 12})
    @patch.object(badge_tool, "maybe_check_firmware")
    @patch.object(badge_tool, "detect_port", return_value="/dev/ttyACM1")
    @patch.object(badge_tool, "ensure_tools")
    def test_upload_writes_the_detected_version(
            self, _ensure, _detect, _check, _existing, _logos, upload_tree,
            _print):
        args = badge_tool.build_parser().parse_args(["upload", "--no-git-info"])
        with patch.object(badge_tool, "probe_hardware", return_value="2026"):
            badge_tool.command_upload(args)
        config = upload_tree.call_args.args[1]
        self.assertEqual(config["badge_version"], "2026")
        self.assertEqual(config["device_id"], "A" * 12)

    @patch("builtins.print")
    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "existing_config", return_value={})
    @patch.object(badge_tool, "detect_port", return_value="/dev/ttyACM0")
    @patch.object(badge_tool, "download_firmware", return_value=Path("fw.bin"))
    @patch.object(badge_tool, "latest_firmware")
    @patch.object(badge_tool, "ensure_tools")
    def test_flash_refuses_to_erase_an_unidentified_badge(
            self, _ensure, _latest, _download, _detect, _existing, run, _print):
        args = badge_tool.build_parser().parse_args(["flash"])
        with patch.object(badge_tool, "probe_hardware", return_value=None), \
                self.assertRaises(badge_tool.BadgeToolError):
            badge_tool.command_flash(args)
        run.assert_not_called()


HAVE_MPY_CROSS = badge_tool.tool_command("mpy-cross") is not None


def fake_mpy_cross(version_line):
    """A stand-in mpy-cross command that only answers --version."""
    return [sys.executable, "-c", "print({!r})".format(version_line)]


def badge_run(mpy="6", removed=True, copied=None):
    """A fake run() answering the .mpy version and file-removal execs.

    Records into copied whether each path given to fs cp exists."""
    def run(command, **_kwargs):
        if copied is not None and command[1:3] == ["fs", "cp"]:
            copied.extend((Path(p).name, Path(p).exists())
                          for p in command[4:-1])
        stdout = ""
        if "BADGE_MPY" in command[-1]:
            stdout = "BADGE_MPY={}\r\n".format(mpy)
        elif "BADGE_REMOVED" in command[-1] and removed:
            stdout = "BADGE_REMOVED\r\n"
        return Namespace(returncode=0, stdout=stdout, stderr="")
    return run


class PrecompileTests(unittest.TestCase):
    @patch.object(badge_tool.shutil, "which", return_value=None)
    @patch.object(badge_tool.importlib.util, "find_spec")
    def test_mpy_cross_is_found_as_its_python_module(self, find_spec, _which):
        find_spec.side_effect = lambda name: object() if name == "mpy_cross" else None
        self.assertEqual(badge_tool.tool_command("mpy-cross"),
                         [sys.executable, "-m", "mpy_cross"])

    @patch.object(badge_tool, "tool_command", return_value=None)
    def test_missing_tool_names_the_install_command(self, _tool):
        with self.assertRaises(badge_tool.BadgeToolError) as caught:
            badge_tool.ensure_tools(install=False, tools=("mpy-cross",))
        self.assertIn("uv pip install mpy-cross", str(caught.exception))

    def test_no_compile_option_for_upload_and_flash(self):
        parser = badge_tool.build_parser()
        for command in ("upload", "flash"):
            with self.subTest(command=command):
                self.assertFalse(parser.parse_args([command]).no_compile)
                self.assertTrue(
                    parser.parse_args([command, "--no-compile"]).no_compile)
                self.assertEqual(
                    badge_tool.upload_tools(parser.parse_args([command])),
                    ("esptool", "mpremote", "mpy-cross"))

    @unittest.skipUnless(HAVE_MPY_CROSS, "mpy-cross is not installed")
    def test_staging_compiles_modules_but_keeps_main_and_boot_as_source(self):
        software = badge_tool.SOFTWARE_DIR
        files = [software / name for name in (
            "boot.py", "main.py", "linklog.py", "certs/isrg-root-x1.pem",
            "games/snake.py", "games/tictactoe.py", "logos/bolt.py")]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entries, replaced = badge_tool.stage_upload_files(files, root, True)
            staged = sorted(p.relative_to(root).as_posix()
                            for p in root.rglob("*") if p.is_file())
            ttt = (root / "games" / "tictactoe.mpy").read_bytes()
            index = json.loads((root / "games" / "index.json").read_text())
            self.assertEqual((root / "main.py").read_bytes(),
                             (software / "main.py").read_bytes())
        self.assertEqual(staged, [
            "boot.py", "certs/isrg-root-x1.pem", "games/index.json",
            "games/snake.mpy", "games/tictactoe.mpy", "linklog.mpy",
            "logos/bolt.mpy", "main.py"])
        self.assertEqual(replaced, ["/linklog.py", "/games/snake.py",
                                    "/games/tictactoe.py", "/logos/bolt.py"])
        self.assertEqual([e.name for e in entries],
                         ["boot.py", "certs", "games", "linklog.mpy", "logos",
                          "main.py"])
        self.assertEqual(ttt[:2], b"M\x06")            # .mpy format v6
        self.assertIn(b"games/tictactoe.py", ttt)     # traceback file name
        self.assertEqual(index["tictactoe"], "Tic-tac-toe")
        self.assertEqual(index["snake"], "Snake")

    @unittest.skipUnless(HAVE_MPY_CROSS, "mpy-cross is not installed")
    @patch.object(badge_tool, "write_remote_config")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    @patch.object(badge_tool, "clean_bytecode_cache", return_value=0)
    @patch("builtins.print")
    def test_upload_checks_the_badge_then_removes_replaced_sources(
            self, _print, _clean, _prefix, _write_config):
        files = [badge_tool.SOFTWARE_DIR / "main.py",
                 badge_tool.SOFTWARE_DIR / "linklog.py",
                 badge_tool.SOFTWARE_DIR / "games" / "snake.py"]
        copied = []
        with patch.object(badge_tool, "upload_files", return_value=files), \
                patch.object(badge_tool, "run",
                             side_effect=badge_run(copied=copied)) as run:
            badge_tool.upload_tree("/dev/ttyACM0", {}, compile_modules=True)
        commands = [c.args[0] for c in run.call_args_list]
        steps = ["mpy check" if "BADGE_MPY" in c[-1] else
                 "remove" if "BADGE_REMOVED" in c[-1] else " ".join(c[1:4])
                 for c in commands]
        self.assertEqual(steps[:4], ["mpy check", "fs rm -r", "fs cp -r",
                                     "remove"])
        self.assertEqual(copied, [("games", True), ("linklog.mpy", True),
                                  ("main.py", True)])
        self.assertIn("'/games/snake.py'", commands[3][-1])
        self.assertIn("'/linklog.py'", commands[3][-1])
        self.assertNotIn("main.py", commands[3][-1])

    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_mismatched_mpy_version_stops_before_uploading(self, _prefix):
        writes_v6 = fake_mpy_cross(
            "MicroPython v1.29.0; mpy-cross emitting mpy v6.3")
        for badge, expected in (("7", "loads v7"),
                                ("0", "too old to report its version")):
            with self.subTest(badge=badge), \
                    patch.object(badge_tool, "tool_command",
                                 return_value=writes_v6), \
                    patch.object(badge_tool, "run",
                                 side_effect=badge_run(mpy=badge)) as run, \
                    self.assertRaises(badge_tool.BadgeToolError) as caught:
                badge_tool.check_mpy_compatible("/dev/ttyACM0")
            self.assertIn("writes .mpy v6", str(caught.exception))
            self.assertIn(expected, str(caught.exception))
            self.assertEqual(len(run.call_args_list), 1)

    @patch("builtins.print")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_matching_mpy_version_passes(self, _prefix, _print):
        writes_v6 = fake_mpy_cross("mpy-cross emitting mpy v6.3")
        with patch.object(badge_tool, "tool_command", return_value=writes_v6), \
                patch.object(badge_tool, "run", side_effect=badge_run(mpy="6")):
            badge_tool.check_mpy_compatible("/dev/ttyACM0")

    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_sources_left_behind_are_an_error(self, _prefix):
        with patch.object(badge_tool, "run",
                          side_effect=badge_run(removed=False)), \
                self.assertRaises(badge_tool.BadgeToolError) as caught:
            badge_tool.remove_replaced_sources("/dev/ttyACM0", ["/bsides.py"])
        self.assertIn("keep running", str(caught.exception))

    def test_remove_code_runs_and_tolerates_missing_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            present = Path(temp_dir) / "gone.py"
            present.write_text("x = 1\n")
            code = badge_tool.REMOVE_FILES_CODE.format(
                paths=[str(present), str(Path(temp_dir) / "never.py")])
            result = subprocess.run([sys.executable, "-c", code], check=True,
                                    capture_output=True, text=True)
            self.assertFalse(present.exists())
        self.assertEqual(result.stdout.strip(), "BADGE_REMOVED")


class LogsCommandTests(unittest.TestCase):
    def dump_output(self, old=None, current=None, badge_json=None):
        """Run the on-badge dump snippet under CPython against temp files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            files = {}
            for name, text in (("linklog.old.txt", old), ("linklog.txt", current)):
                path = Path(temp_dir) / name
                if text is not None:
                    path.write_text(text, encoding="utf-8")
                files["/" + name] = str(path)
            code = badge_tool.DUMP_LOG_CODE
            for remote, local in files.items():
                code = code.replace(repr(remote), repr(local))
            json_path = Path(temp_dir) / "badge.json"
            if badge_json is not None:
                json_path.write_text(badge_json, encoding="utf-8")
            code = code.replace("'/badge.json'", repr(str(json_path)))
            result = subprocess.run([sys.executable, "-c", code], check=True,
                                    capture_output=True, text=True)
        return result.stdout.replace("\n", "\r\n")   # as seen over mpremote

    def test_dump_joins_old_then_current_and_reads_the_device_id(self):
        stdout = self.dump_output(
            old="--- session start, id A1B2C3D4E5F6 ---\n1.000 ttt old\n",
            current="2.000 ttt new",
            badge_json='{"device_id": "a1b2c3d4e5f6"}')
        device_id, log = badge_tool.parse_log_dump(stdout)
        self.assertEqual(device_id, "A1B2C3D4E5F6")
        self.assertEqual(log, "--- session start, id A1B2C3D4E5F6 ---\n"
                              "1.000 ttt old\n2.000 ttt new\n")

    def test_dump_without_log_files_is_empty(self):
        device_id, log = badge_tool.parse_log_dump(self.dump_output())
        self.assertIsNone(device_id)
        self.assertEqual(log, "")

    def test_truncated_dump_is_an_error(self):
        with self.assertRaises(badge_tool.BadgeToolError):
            badge_tool.parse_log_dump("BADGE_LOG_FILE /linklog.txt\r\n1.0 x")

    def run_logs(self, stdout, argv=()):
        results = [Namespace(returncode=0, stdout=stdout, stderr=""),
                   Namespace(returncode=0, stdout="BADGE_LOG_CLEARED\r\n",
                             stderr=""),
                   Namespace(returncode=0, stdout="", stderr="")]
        args = badge_tool.build_parser().parse_args(["logs"] + list(argv))
        with patch.object(badge_tool, "ensure_tools"), \
                patch.object(badge_tool, "detect_port", return_value="COM10"), \
                patch.object(badge_tool, "mpremote_prefix",
                             return_value=["mpremote"]), \
                patch.object(badge_tool, "run", side_effect=results) as run:
            args.handler(args)
        return [c.args[0] for c in run.call_args_list]

    def test_logs_saves_to_output_resumes_session_and_resets(self):
        stdout = self.dump_output(current="1.000 nbr rx x\n",
                                  badge_json='{"device_id": "A1B2C3D4E5F6"}')
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "out" / "log.txt"
            commands = self.run_logs(stdout, ["--output", str(output)])
            self.assertEqual(output.read_text(), "1.000 nbr rx x\n")
        self.assertEqual(commands[0][:3], ["mpremote", "resume", "exec"])
        self.assertEqual(commands[-1], ["mpremote", "reset"])
        self.assertEqual(len(commands), 2)

    def test_logs_default_name_uses_the_device_id(self):
        stdout = self.dump_output(current="1.000 x\n",
                                  badge_json='{"device_id": "A1B2C3D4E5F6"}')
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(badge_tool, "DEFAULT_LOG_DIR", Path(temp_dir)):
            self.run_logs(stdout)
            names = [p.name for p in Path(temp_dir).iterdir()]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("linklog-A1B2C3D4E5F6-"))

    def test_logs_clear_removes_the_badge_log_after_saving(self):
        stdout = self.dump_output(current="1.000 x\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            commands = self.run_logs(
                stdout, ["--output", str(Path(temp_dir) / "l.txt"), "--clear"])
        self.assertEqual(commands[1][:2], ["mpremote", "exec"])
        self.assertIn("os.remove(p)", commands[1][2])
        self.assertEqual(commands[2], ["mpremote", "reset"])


if __name__ == "__main__":
    unittest.main()
