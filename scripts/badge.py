#!/usr/bin/env python3
"""Prepare and manage BSides Tallinn ESP32-C3 badges."""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOFTWARE_DIR = ROOT / "software"
DEFAULT_FIRMWARE_DIR = ROOT / ".cache" / "firmware"
DOWNLOAD_PAGE = "https://micropython.org/download/ESP32_GENERIC_C3/"
SUPPORTED_BADGES = ("2025_prototype", "2025", "2026")
SKIPPED_NAMES = {".ds_store", "badge.json", "requirements.txt"}
LEGACY_FILES = ("params.json", "id.txt", "yourname.txt")
OBSOLETE_FILES = ("bsides25.py",)


class BadgeToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Firmware:
    version: str
    date: str
    url: str

    @property
    def filename(self) -> str:
        return urllib.parse.urlsplit(self.url).path.rsplit("/", 1)[-1]


def run(command: list[str], *, check: bool = True, capture: bool = False,
        timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    print("+", subprocess.list2cmdline(command))
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def tool_command(tool: str) -> list[str] | None:
    """Return a command for an importable module or standalone executable."""
    module = tool.replace("-", "_")        # the mpy-cross package is mpy_cross
    if importlib.util.find_spec(module) is not None:
        return [sys.executable, "-m", module]
    executable = shutil.which(tool)
    return [executable] if executable else None


BASE_TOOLS = ("esptool", "mpremote")
ALL_TOOLS = BASE_TOOLS + ("mpy-cross",)


def ensure_tools(install: bool, tools: tuple[str, ...] = BASE_TOOLS) -> None:
    packages = [package for package in tools if tool_command(package) is None]
    if not packages:
        print("{} installed.".format(", ".join(tools)))
        return
    if not install:
        raise BadgeToolError(
            "Missing {0}. Run 'python scripts/badge.py init', or install into "
            "this Python environment, e.g. 'uv pip install {1}'.".format(
                ", ".join(packages), " ".join(packages)))
    run([sys.executable, "-m", "pip", "install", "--user"] + packages)


def parse_latest_firmware(page: str, base_url: str = DOWNLOAD_PAGE) -> Firmware:
    pattern = re.compile(
        r'''href=["']([^"']*ESP32_GENERIC_C3-(\d{8})-v(\d+(?:\.\d+)+)\.bin)["']''',
        re.IGNORECASE,
    )
    found = []
    for href, date, version in pattern.findall(page):
        found.append((tuple(int(part) for part in version.split(".")), date,
                      Firmware(version, date,
                               urllib.parse.urljoin(base_url, html.unescape(href)))))
    if not found:
        raise BadgeToolError("Could not find a stable ESP32_GENERIC_C3 .bin download.")
    return max(found, key=lambda item: (item[0], item[1]))[2]


def latest_firmware() -> Firmware:
    request = urllib.request.Request(DOWNLOAD_PAGE, headers={"User-Agent": "bsides-badge-tool"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise BadgeToolError("Could not query MicroPython downloads: {}".format(exc))
    return parse_latest_firmware(page)


def download_firmware(firmware: Firmware, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / firmware.filename
    if destination.exists() and destination.stat().st_size:
        print("Using cached firmware {}".format(destination))
        return destination
    print("Downloading MicroPython {}...".format(firmware.version))
    try:
        with urllib.request.urlopen(firmware.url, timeout=60) as response:
            data = response.read()
        destination.write_bytes(data)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise BadgeToolError("Firmware download failed: {}".format(exc))
    return destination


def detect_port(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        from serial.tools import list_ports
    except ImportError:
        print("Serial-port detection unavailable; tools will use auto-detection.")
        return None

    ports = list(list_ports.comports())
    if not ports:
        print("No serial port found; tools will use auto-detection.")
        return None
    likely = [port for port in ports if port.vid == 0x303A or any(
        word in "{} {} {}".format(port.description, port.manufacturer, port.product).lower()
        for word in ("espressif", "esp32", "usb jtag/serial"))]
    candidates = likely or ports
    if len(candidates) == 1:
        print("Detected badge port: {}".format(candidates[0].device))
        return candidates[0].device
    raise BadgeToolError(
        "Multiple serial ports found ({}); pass --port.".format(
            ", ".join(port.device for port in candidates)))


def mpremote_prefix(port: str | None) -> list[str]:
    command = tool_command("mpremote")
    if command is None:
        raise BadgeToolError("mpremote is not installed or available on PATH.")
    if port:
        command += ["connect", port]
    return command


def mpremote_error(result: subprocess.CompletedProcess[str]) -> str:
    """The last line mpremote or the badge printed, to explain a failure."""
    lines = (result.stderr or result.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else "exit status {}".format(result.returncode)


PING_CODE = "print('BADGE_OK')"
PING_TIMEOUT = 10
BOOT_WAIT = 20       # seconds for the port to come back after a reset


def badge_answers(port: str | None) -> bool:
    """True if MicroPython on the badge runs a command. resume keeps the
    badge's RAM (the link log not yet on flash) instead of soft-resetting."""
    try:
        result = run(mpremote_prefix(port) + ["resume", "exec", PING_CODE],
                     check=False, capture=True, timeout=PING_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("The badge did not answer within {} seconds.".format(PING_TIMEOUT))
        return False
    if "BADGE_OK" in result.stdout:
        return True
    print("The badge did not answer: {}".format(mpremote_error(result)))
    return False


def hard_reset(port: str | None) -> None:
    """Reset the chip through the USB serial line, as the reset button does."""
    esptool = tool_command("esptool")
    if esptool is None:
        raise BadgeToolError("esptool is not installed or available on PATH.")
    port_args = ["--port", port] if port else []
    # chip-id connects, then esptool's default --after hard-resets the chip.
    run(esptool + port_args + ["chip-id"], check=False, capture=True,
        timeout=30)
    deadline = time.monotonic() + BOOT_WAIT
    while port and not Path(port).exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    time.sleep(3)    # boot.py and main.py start; mpremote interrupts them


def wake_badge(port: str | None) -> None:
    """Make sure the badge answers, resetting it once if it does not.

    A badge busy in a stuck program can hold the serial port open without
    ever reaching the MicroPython prompt, and every mpremote command then
    times out."""
    if badge_answers(port):
        return
    print("Resetting the badge on {} and trying again.".format(port or "auto"))
    hard_reset(port)
    if not badge_answers(port):
        raise BadgeToolError(
            "The badge on {} does not answer, even after a reset. Close other "
            "programs using the port, unplug and replug the badge, or hold "
            "SELECT while pressing reset, then retry.".format(port or "auto"))


def remote_read(port: str | None, filename: str) -> str:
    result = run(mpremote_prefix(port) + ["fs", "cat", ":/" + filename],
                 check=False, capture=True, timeout=20)
    return result.stdout.strip() if result.returncode == 0 else ""


def extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(text[start:end + 1])
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def load_local_defaults() -> dict[str, Any]:
    return json.loads((SOFTWARE_DIR / "badge.json").read_text(encoding="utf-8"))


def read_remote_config(port: str | None) -> dict[str, Any]:
    config = extract_json(remote_read(port, "badge.json"))
    if config:
        return config

    # Upgrade badges that still have the original three settings files.
    legacy_params = extract_json(remote_read(port, "params.json"))
    legacy_id = remote_read(port, "id.txt").strip().upper()
    legacy_name = remote_read(port, "yourname.txt").strip()
    if legacy_params:
        config["params"] = legacy_params
    if re.fullmatch(r"[0-9A-F]{12}", legacy_id):
        config["device_id"] = legacy_id
    if legacy_name:
        config["holder_name"] = legacy_name
    return config


def existing_config(port: str | None, wipe: bool) -> dict[str, Any]:
    """Read settings to preserve unless a fresh configuration was requested."""
    return {} if wipe else read_remote_config(port)


def git_commit_info() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"], cwd=ROOT,
            check=True, text=True, capture_output=True).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=ROOT,
            check=True, text=True, capture_output=True).stdout.strip() or "detached"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            check=True, text=True, capture_output=True).stdout.strip()
        if dirty:
            print("Warning: the working tree has uncommitted changes; git info identifies HEAD.")
        return "{} {}".format(commit, branch)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def merge_config(remote: dict[str, Any], badge_version: str | None,
                 holder_name: str | None, write_git: bool = True) -> dict[str, Any]:
    config = load_local_defaults()
    for key in ("device_id", "holder_name", "badge_version", "git_commit"):
        if key in remote:
            config[key] = remote[key]
    if isinstance(remote.get("params"), dict):
        config["params"].update(remote["params"])
    if badge_version:
        config["badge_version"] = badge_version
    if holder_name is not None:
        config["holder_name"] = holder_name.strip()
    if write_git:
        config["git_commit"] = git_commit_info()
    return config


def should_upload(path: Path) -> bool:
    relative = path.relative_to(SOFTWARE_DIR)
    return (path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in relative.parts
            and path.suffix.lower() != ".pyc"
            and path.name.lower() not in SKIPPED_NAMES)


def upload_files() -> list[Path]:
    return sorted((path for path in SOFTWARE_DIR.rglob("*") if should_upload(path)),
                  key=lambda path: path.as_posix())


def clean_bytecode_cache() -> int:
    caches = list(SOFTWARE_DIR.rglob("__pycache__"))
    bytecode = [path for pattern in ("*.pyc", "*.pyo")
                for path in SOFTWARE_DIR.rglob(pattern)]
    for path in caches:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for path in bytecode:
        path.unlink(missing_ok=True)
    return len(caches) + len(bytecode)


# Compiling a module on the badge needs several times its size in RAM, and
# that peak permanently grows the Python heap into the memory the Wi-Fi driver
# needs, so modules are uploaded as precompiled .mpy bytecode. MicroPython
# only runs these two as source.
SOURCE_ONLY = {"main.py", "boot.py"}
GAMES_INDEX = Path("games") / "index.json"


def load_game_loader() -> Any:
    """The badge's own game_loader, so game names are read the same way."""
    spec = importlib.util.spec_from_file_location(
        "badge_game_loader", SOFTWARE_DIR / "game_loader.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def compile_module(source: Path, destination: Path) -> None:
    command = tool_command("mpy-cross")
    if command is None:
        raise BadgeToolError("mpy-cross is not installed.")
    relative = source.relative_to(SOFTWARE_DIR).as_posix()
    result = subprocess.run(
        command + ["-o", str(destination), "-s", relative, str(source)],
        text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise BadgeToolError("mpy-cross failed on {}: {}".format(
            relative, (result.stderr or result.stdout).strip()))


def stage_upload_files(files: list[Path], root: Path, compile_modules: bool
                       ) -> tuple[list[Path], list[str]]:
    """Copy approved files into an isolated tree for recursive upload.

    With compile_modules, modules are staged as .mpy and the games get an
    index of their names (a .mpy has no GAME_NAME line to read). Returns the
    upload entries and the badge paths of the .py files the .mpy replace."""
    replaced = []
    for source in files:
        relative = source.relative_to(SOFTWARE_DIR)
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if compile_modules and relative.suffix == ".py" \
                and relative.as_posix() not in SOURCE_ONLY:
            compile_module(source, destination.with_suffix(".mpy"))
            replaced.append("/" + relative.as_posix())
        else:
            shutil.copy2(source, destination)
    if compile_modules and (root / GAMES_INDEX.parent).is_dir():
        games = load_game_loader().discover(str(SOFTWARE_DIR / "games"))
        (root / GAMES_INDEX).write_text(
            json.dumps({module: name for name, module in games}),
            encoding="utf-8")
    # Top-level entries of the staged tree, which has .mpy names, not .py.
    return sorted(root.iterdir()), replaced


BADGE_MPY_CODE = (
    "import sys; "
    "print('BADGE_MPY=%d' % (getattr(sys.implementation, '_mpy', 0) & 0xff))")

REMOVE_FILES_CODE = """import os
for p in {paths!r}:
    try:
        os.remove(p)
    except OSError:
        pass
print('BADGE_REMOVED')
"""


def check_mpy_compatible(port: str | None) -> None:
    """Refuse to upload .mpy files the badge's MicroPython cannot import:
    main.py would fail on its first import and the badge would not start."""
    command = tool_command("mpy-cross")
    if command is None:
        raise BadgeToolError("mpy-cross is not installed.")
    cross = subprocess.run(command + ["--version"], text=True,
                           capture_output=True, check=False)
    match = re.search(r"mpy v(\d+)", cross.stdout + cross.stderr)
    if not match:
        raise BadgeToolError("Could not read the mpy-cross version: {}".format(
            (cross.stderr or cross.stdout).strip()))
    writes = int(match.group(1))
    result = run(mpremote_prefix(port) + ["exec", BADGE_MPY_CODE], check=False,
                 capture=True, timeout=20)
    found = re.search(r"BADGE_MPY=(\d+)", result.stdout)
    if result.returncode != 0 or not found:
        raise BadgeToolError("Could not read the badge's .mpy version: {}".format(
            mpremote_error(result)))
    loads = int(found.group(1))
    if loads != writes:
        raise BadgeToolError(
            "mpy-cross writes .mpy v{} but the badge's MicroPython {}. Flash "
            "current firmware, install a matching mpy-cross, or pass "
            "--no-compile.".format(writes, "loads v{}".format(loads) if loads
                                   else "is too old to report its version"))
    print("Precompiling modules to .mpy v{} (the badge's version).".format(writes))


def remove_replaced_sources(port: str | None, paths: list[str]) -> None:
    """Delete the .py files that uploaded .mpy files replace, because
    MicroPython imports a .py in preference to a .mpy of the same name."""
    if not paths:
        return
    result = run(mpremote_prefix(port)
                 + ["exec", REMOVE_FILES_CODE.format(paths=paths)],
                 check=False, capture=True, timeout=60)
    if "BADGE_REMOVED" not in result.stdout:
        raise BadgeToolError(
            "Uploaded .mpy files, but could not delete the .py files they "
            "replace, which the badge would keep running: {}".format(
                mpremote_error(result)))


def remove_remote_sponsor_logos(port: str | None) -> None:
    """Remove the previous sponsor set so recursive copy cannot leave stale logos."""
    run(mpremote_prefix(port) + ["fs", "rm", "-r", ":/logos"],
        check=False, capture=True, timeout=20)


def check_firmware_version(port: str | None, latest: Firmware) -> bool:
    code = (
        "import sys; v=sys.implementation.version; "
        "print('BADGE_MP_VERSION=%d.%d.%d' % (v[0],v[1],v[2]))"
    )
    result = run(mpremote_prefix(port) + ["exec", code], check=False,
                 capture=True, timeout=20)
    match = re.search(r"BADGE_MP_VERSION=(\d+\.\d+\.\d+)", result.stdout)
    if not match:
        print("Warning: could not read the badge's MicroPython version: {}"
              .format(mpremote_error(result)))
        return False
    current = match.group(1)
    if current == latest.version:
        print("MicroPython {} is the latest stable release.".format(current))
        return True
    print("Warning: badge has MicroPython {}; latest stable is {}.".format(
        current, latest.version))
    return False


def maybe_check_firmware(port: str | None, skip: bool) -> None:
    if skip:
        return
    try:
        check_firmware_version(port, latest_firmware())
    except BadgeToolError as exc:
        print("Warning: {}".format(exc))


def write_remote_config(port: str | None, config: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="bsides-badge-") as temp_dir:
        local = Path(temp_dir) / "badge.json"
        local.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        run(mpremote_prefix(port) + ["fs", "cp", str(local), ":/badge.json"])


def battery_voltage_line(port: str | None,
                         config: dict[str, Any]) -> str | None:
    """Measure a 2026 badge now and return the final CLI output line."""
    if config.get("badge_version") != "2026":
        return None
    code = (
        "import battery; "
        "print('BADGE_BATTERY_VOLTAGE=%.3f' % "
        "battery.read_battery_voltage(4))"
    )
    result = run(mpremote_prefix(port) + ["exec", code], check=False,
                 capture=True, timeout=20)
    match = re.search(r"BADGE_BATTERY_VOLTAGE=(\d+(?:\.\d+)?)", result.stdout)
    if result.returncode != 0 or not match:
        return "Battery voltage: unavailable"
    voltage = float(match.group(1))
    line = "Battery voltage: {:.3f} V".format(voltage)
    if voltage < 3.8 or voltage > 4.2:
        line += " WARNING!! outside 3.8-4.2 V range"
    return line


def upload_tree(port: str | None, config: dict[str, Any],
                compile_modules: bool) -> str | None:
    removed = clean_bytecode_cache()
    if removed:
        print("Removed {} Python cache entries.".format(removed))
    files = upload_files()
    if compile_modules:
        check_mpy_compatible(port)
    with tempfile.TemporaryDirectory(prefix="bsides-badge-upload-") as temp_dir:
        entries, replaced = stage_upload_files(files, Path(temp_dir),
                                               compile_modules)
        # Only once nothing can refuse the upload, so a refused upload does
        # not leave the badge without its logos.
        remove_remote_sponsor_logos(port)
        if entries:
            run(mpremote_prefix(port) + ["fs", "cp", "-r"]
                + [str(path) for path in entries] + [":"])
    remove_replaced_sources(port, replaced)
    write_remote_config(port, config)

    for stale_file in LEGACY_FILES + OBSOLETE_FILES:
        run(mpremote_prefix(port) + ["fs", "rm", ":/" + stale_file], check=False,
            capture=True, timeout=20)
    battery_line = battery_voltage_line(port, config)
    run(mpremote_prefix(port) + ["reset"], check=False, timeout=20)
    print("Uploaded {} firmware files ({} precompiled) and badge.json.".format(
        len(files), len(replaced)))
    return battery_line


# Read the pins that differ between hardware versions (see hardware/*.pdf):
# the 2025 prototype's OLED answers at 0x3D, the others at 0x3C; GPIO4 is
# SELECT with a 5.1k pull-up to 3.3 V on 2025 boards, but the 100k/20k
# battery divider (0.5-0.7 V) on 2026 boards.
PROBE_CODE = """from machine import ADC, I2C, Pin
found = I2C(0, scl=Pin(1), sda=Pin(0)).scan()
adc = ADC(Pin(4))
adc.atten(ADC.ATTN_11DB)
uv = 0
for _ in range(8):
    uv += adc.read_uv()
print('BADGE_PROBE i2c=%s gpio4_mv=%d' % (','.join(str(a) for a in found), uv // 8000))
"""


def classify_hardware(addresses: list[int], gpio4_mv: int) -> str | None:
    if 0x3D in addresses and 0x3C not in addresses:
        return "2025_prototype"
    if 0x3C not in addresses:
        return None
    if gpio4_mv >= 1500:
        return "2025"
    if 350 <= gpio4_mv <= 1000:
        return "2026"
    return None      # e.g. SELECT held down on a 2025 badge


def probe_hardware(port: str | None) -> str | None:
    """Identify the badge from its pins, or None if it cannot be read."""
    result = run(mpremote_prefix(port) + ["exec", PROBE_CODE], check=False,
                 capture=True, timeout=20)
    match = re.search(r"BADGE_PROBE i2c=([\d,]*) gpio4_mv=(\d+)", result.stdout)
    if result.returncode != 0 or not match:
        print("Could not probe the badge hardware: {}".format(
            mpremote_error(result)))
        return None
    addresses = [int(a) for a in match.group(1).split(",") if a]
    gpio4_mv = int(match.group(2))
    version = classify_hardware(addresses, gpio4_mv)
    print("Hardware probe: I2C {}, GPIO4 {} mV -> {}".format(
        [hex(a) for a in addresses], gpio4_mv, version or "unknown"))
    return version


def resolve_badge_version(explicit: str | None, probed: str | None,
                          stored: str | None) -> str:
    """Pick the version to write: --badge-version, then the probe, then badge.json."""
    if explicit:
        if probed and probed != explicit:
            print("Warning: hardware looks like {}, but using --badge-version {}."
                  .format(probed, explicit))
        return explicit
    if probed:
        if stored and stored != probed:
            print("Note: badge.json said {}; the hardware is {}.".format(
                stored, probed))
        print("Detected badge version {}.".format(probed))
        return probed
    if stored in SUPPORTED_BADGES:
        print("Using badge version {} from badge.json.".format(stored))
        return stored
    raise BadgeToolError(
        "Could not identify the badge hardware; pass --badge-version. If "
        "every mpremote command above failed, the badge is not answering: "
        "close other programs using the port, or hold SELECT while "
        "resetting the badge, then retry.")


def detect_badge_version(args: argparse.Namespace, port: str | None,
                         remote: dict[str, Any]) -> str:
    return resolve_badge_version(args.badge_version, probe_hardware(port),
                                 remote.get("badge_version"))


def command_init(args: argparse.Namespace) -> None:
    ensure_tools(install=True, tools=ALL_TOOLS)
    firmware = latest_firmware()
    path = download_firmware(firmware, args.firmware_dir)
    print("Ready: MicroPython {} at {}".format(firmware.version, path))


def upload_tools(args: argparse.Namespace) -> tuple[str, ...]:
    return BASE_TOOLS if args.no_compile else ALL_TOOLS


def command_upload(args: argparse.Namespace) -> str | None:
    ensure_tools(install=False, tools=upload_tools(args))
    port = detect_port(args.port)
    wake_badge(port)
    maybe_check_firmware(port, args.skip_version_check)
    remote = existing_config(port, args.wipe)
    version = detect_badge_version(args, port, remote)
    config = merge_config(remote, version, args.holder_name, not args.no_git_info)
    return upload_tree(port, config, not args.no_compile)


def command_flash(args: argparse.Namespace) -> str | None:
    ensure_tools(install=True, tools=upload_tools(args))
    firmware = latest_firmware()
    image = download_firmware(firmware, args.firmware_dir)
    port = detect_port(args.port)
    remote = existing_config(port, args.wipe)
    # Probe before erasing: a blank chip has no MicroPython to answer.
    version = detect_badge_version(args, port, remote)
    config = merge_config(remote, version, args.holder_name, not args.no_git_info)

    esptool = tool_command("esptool")
    if esptool is None:
        raise BadgeToolError("esptool is not installed or available on PATH.")
    port_args = ["--port", port] if port else []
    run(esptool + port_args + ["erase-flash"])
    run(esptool + port_args + ["--baud", str(args.baud), "write-flash", "0", str(image)])
    print("Waiting for MicroPython to start...")
    time.sleep(2)
    battery_line = upload_tree(port, config, not args.no_compile)
    print("Full flash complete with MicroPython {}.".format(firmware.version))
    return battery_line


def command_delete(args: argparse.Namespace) -> str | None:
    ensure_tools(install=False)
    port = detect_port(args.port)
    wake_badge(port)
    config = read_remote_config(port)
    battery_line = battery_voltage_line(port, config)
    code = """import os
def remove_tree(path):
 for entry in list(os.ilistdir(path)):
  child = path.rstrip('/') + '/' + entry[0]
  mode = entry[1] if len(entry) > 1 else os.stat(child)[0]
  if not mode:
   mode = os.stat(child)[0]
  if mode & 0x4000:
   remove_tree(child)
   os.rmdir(child)
  else:
   os.remove(child)
remove_tree('/')
print('BADGE_DELETE_OK')
"""
    result = run(mpremote_prefix(port) + ["exec", code], check=False,
                 capture=True, timeout=60)
    if result.returncode != 0 or "BADGE_DELETE_OK" not in result.stdout:
        message = (result.stderr or result.stdout).strip()
        raise BadgeToolError("Could not delete badge files: {}".format(message))
    print("Deleted all files from the badge filesystem; this cannot be undone.")
    return battery_line


def command_name(args: argparse.Namespace) -> str | None:
    ensure_tools(install=False)
    port = detect_port(args.port)
    wake_badge(port)
    maybe_check_firmware(port, args.skip_version_check)
    remote = read_remote_config(port)
    if not remote and not args.badge_version:
        raise BadgeToolError("Badge is not initialized; also pass --badge-version.")
    config = merge_config(remote, args.badge_version, args.name, not args.no_git_info)
    write_remote_config(port, config)
    battery_line = battery_voltage_line(port, config)
    run(mpremote_prefix(port) + ["reset"], check=False, timeout=20)
    print("Holder name updated.")
    return battery_line


DEFAULT_LOG_DIR = ROOT / "badge-logs"
REMOTE_LOG_FILES = ("/linklog.old.txt", "/linklog.txt")

# `resume` keeps the interrupted application's modules loaded, so linklog can
# flush lines still batched in RAM before the files are read.
DUMP_LOG_CODE = """import sys
m = sys.modules.get('linklog')
if m:
    m.flush()
try:
    import json
    print('BADGE_LOG_ID ' + json.load(open('/badge.json'))['device_id'])
except Exception:
    pass
for p in {files!r}:
    try:
        f = open(p)
    except OSError:
        continue
    print('BADGE_LOG_FILE ' + p)
    while True:
        chunk = f.read(512)
        if not chunk:
            break
        sys.stdout.write(chunk)
    f.close()
print('BADGE_LOG_END')
""".format(files=REMOTE_LOG_FILES)

CLEAR_LOG_CODE = """import os
for p in {files!r}:
    try:
        os.remove(p)
    except OSError:
        pass
print('BADGE_LOG_CLEARED')
""".format(files=REMOTE_LOG_FILES)


def parse_log_dump(stdout: str) -> tuple[str | None, str]:
    """Split DUMP_LOG_CODE output into (device ID, oldest-first log text)."""
    text = stdout.replace("\r\n", "\n")
    if "BADGE_LOG_END" not in text:
        raise BadgeToolError("Could not read the link log from the badge.")
    match = re.search(r"^BADGE_LOG_ID ([0-9A-Fa-f]{12})$", text, re.MULTILINE)
    body = text[:text.index("BADGE_LOG_END")]
    chunks = re.split(r"^BADGE_LOG_FILE \S+\n", body, flags=re.MULTILINE)[1:]
    log = "".join(chunk if chunk.endswith("\n") else chunk + "\n"
                  for chunk in chunks if chunk)
    return (match.group(1).upper() if match else None), log


def command_reset(args: argparse.Namespace) -> None:
    ensure_tools(install=False)
    port = detect_port(args.port)
    hard_reset(port)
    if not badge_answers(port):
        raise BadgeToolError("The badge on {} does not answer after the "
                             "reset.".format(port or "auto"))
    print("The badge on {} answers.".format(port or "auto"))


def command_logs(args: argparse.Namespace) -> None:
    ensure_tools(install=False)
    port = detect_port(args.port)
    wake_badge(port)
    result = run(mpremote_prefix(port) + ["resume", "exec", DUMP_LOG_CODE],
                 check=False, capture=True, timeout=60)
    if result.returncode != 0:
        raise BadgeToolError("Could not read the link log: {}".format(
            (result.stderr or result.stdout).strip()))
    device_id, log = parse_log_dump(result.stdout)
    if not log:
        print("The badge has no link log yet.")
    else:
        output = args.output or DEFAULT_LOG_DIR / "linklog-{}-{}.txt".format(
            device_id or "unknown", time.strftime("%Y%m%d-%H%M%S"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(log, encoding="utf-8")
        print("Saved {} log lines to {}".format(log.count("\n"), output))
        if args.clear:
            cleared = run(mpremote_prefix(port) + ["exec", CLEAR_LOG_CODE],
                          check=False, capture=True, timeout=20)
            if "BADGE_LOG_CLEARED" not in cleared.stdout:
                raise BadgeToolError("Saved the log, but could not clear it.")
            print("Cleared the link log on the badge.")
    run(mpremote_prefix(port) + ["reset"], check=False, timeout=20)


def add_connection_options(parser: argparse.ArgumentParser, *, version: bool = False) -> None:
    parser.add_argument("--port", help="serial port (auto-detected by default)")
    if version:
        parser.add_argument(
            "--badge-version", choices=SUPPORTED_BADGES,
            help="hardware version (detected from the badge by default)")


def add_upload_options(parser: argparse.ArgumentParser) -> None:
    add_metadata_options(parser)
    parser.add_argument(
        "--no-compile", action="store_true",
        help="upload .py sources instead of precompiled .mpy; the badge then "
             "compiles them itself, which leaves much less RAM for Wi-Fi")


def add_metadata_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--holder-name", help="also set the holder name")
    parser.add_argument("--no-git-info", action="store_true",
                        help="keep existing/default git info instead of writing HEAD")
    parser.add_argument(
        "--wipe", action="store_true",
        help="skip existing settings and create badge.json from defaults")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="install tools and download latest stable firmware")
    initialize.add_argument("--firmware-dir", type=Path, default=DEFAULT_FIRMWARE_DIR)
    initialize.set_defaults(handler=command_init)

    upload = subparsers.add_parser("upload", help="upload application files")
    add_connection_options(upload, version=True)
    add_upload_options(upload)
    upload.add_argument("--skip-version-check", action="store_true")
    upload.set_defaults(handler=command_upload)

    flash = subparsers.add_parser("flash", help="erase, flash latest MicroPython, and upload files")
    add_connection_options(flash, version=True)
    add_upload_options(flash)
    flash.add_argument("--firmware-dir", type=Path, default=DEFAULT_FIRMWARE_DIR)
    flash.add_argument("--baud", type=int, default=460800)
    flash.set_defaults(handler=command_flash)

    delete = subparsers.add_parser("delete", help="delete all files from the badge")
    add_connection_options(delete)
    delete.set_defaults(handler=command_delete)

    name = subparsers.add_parser("name", help="write the holder name to badge.json")
    add_connection_options(name)
    name.add_argument("name")
    name.add_argument("--badge-version", choices=SUPPORTED_BADGES)
    name.add_argument("--skip-version-check", action="store_true")
    name.add_argument("--no-git-info", action="store_true")
    name.set_defaults(handler=command_name)

    reset = subparsers.add_parser(
        "reset", help="hard-reset the badge, e.g. when it stops answering")
    add_connection_options(reset)
    reset.set_defaults(handler=command_reset)

    logs = subparsers.add_parser(
        "logs", help="save the Wi-Fi link debug log from the badge")
    add_connection_options(logs)
    logs.add_argument("--output", type=Path,
                      help="file to write (default: badge-logs/linklog-<id>-<time>.txt)")
    logs.add_argument("--clear", action="store_true",
                      help="delete the log on the badge after saving it")
    logs.set_defaults(handler=command_logs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        final_line = args.handler(args)
    except (BadgeToolError, OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    if final_line:
        print(final_line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
