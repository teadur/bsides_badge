# BSides Tallinn badge

MicroPython firmware and hardware documentation for the ESP32-C3 BSides Tallinn
badge.

## Supported hardware

| Badge version | OLED address | SELECT | Battery measurement | Appearance |
| --- | --- | --- | --- | --- |
| `2025_prototype` | `0x3D` | GPIO4 | No | Cylindrical battery, empty back side |
| `2025` | `0x3C` | GPIO4 | No | Cylindrical battery, "BSIDES #5" and wolf on the back side |
| `2026` | `0x3C` | GPIO10 | GPIO4 / ADC1_CH4 | Flat LiPo battery, "BSIDES #6" and wolf on the back side |

The 2026 battery input uses the schematic's 100 kΩ / 20 kΩ divider. The status
screen multiplies the ADC voltage by six and estimates LiPo state of charge from
a rough resting-voltage curve. Charging and LED load can make that percentage
inaccurate.

- [2025 schematic](./hardware/BSides_2025_badge_v1.1_schematics.pdf)
- [2026 schematic](./hardware/BSides_2026_badge_v1.2_schematics.pdf)

Common hardware: ESP32-C3FH4 with 4 MB flash, 128x64 SSD1306 OLED, 16 WS2812B
LEDs, Wi-Fi/Bluetooth, and USB-C flashing/charging.

On all badge versions, **Lights -> Plug-in -> Effects** controls the two plug-in LEDs
on GPIO6 and GPIO7. **Breathe** (default), **Blink**, and **Police** run in
opposite phases; **On** lights both LEDs and **Off** turns both off. The choice
is saved when leaving Lights and is independent of the NeoPixel settings.

## Badge configuration

Runtime settings are stored in `/badge.json` on the badge:

```json
{
  "badge_version": "2026",
  "device_id": "A1B2C3D4E5F6",
  "holder_name": "Badge Holder",
  "git_commit": "0123abcd main",
  "params": {
    "Brightness": 10,
    "Hue": 180,
    "Saturation": 100,
    "Speed": 30,
    "Light_effect": 0,
    "Plugin_effect": 0,
    "GameLightsOff": 1,
    "SnakeHighScore": 0,
    "PacmanHighScore": 0,
    "TetrisHighScore": 0,
    "FlappyHighScore": 0
  }
}
```

On first boot, firmware creates a random device ID when needed. Existing
`params.json`, `id.txt`, and `yourname.txt` files are migrated into `badge.json`
and removed. The upload tool also preserves those legacy values while upgrading
an existing badge.

Open **Menu -> Badge -> Status** to see the ID, hardware version, uploaded git
commit and branch. A 2026 badge also shows battery voltage and approximate state
of charge.

Open **Menu -> Badge -> Settings** to toggle **Game lights**. It defaults to
**Off**: entering any game turns off the NeoPixels and both plug-in LEDs, and
leaving the game resumes the selected effects. The choice is saved in
`badge.json`.

When **Menu -> Badge setup -> Fetch Name** is selected, the badge connects to
Wi-Fi, synchronizes its RTC from NTP, and briefly displays the resulting UTC
date and time. It then verifies `badge.bsides.ee` using the bundled ISRG Root X1
certificate before requesting the name. The fetch runs in a lightweight mode
so mbedTLS has enough contiguous memory to validate the complete certificate
chain. NTP failures and TLS certificate or hostname validation failures stop
the request and are shown on the display.
While this procedure is running, BACK cancels it, turns off Wi-Fi, and returns
to the badge menu. The DHCP hostname advertised to the router is
`bsides26-<device ID>`.
After a Wi-Fi, NTP, TLS connection, or certificate failure, SELECT or NEXT
retries the procedure from the Wi-Fi connection stage without leaving the
special Fetch Name mode. BACK still exits from the error screen.

## Badge management tool

Use Python 3.10 or newer on Windows, Linux, or macOS. One Python tool is used so
port detection, filtering, configuration migration, and release discovery stay
consistent across platforms.

(Linux) Make sure to add your user to `dialout` group to access the hardware serial port. Log-out/in or restart after this command.
```console
sudo usermod -aG dialout "$USER"
```

Initialize the workstation. This installs missing `esptool` and `mpremote`
packages and downloads the newest stable `ESP32_GENERIC_C3` MicroPython image.
If `init` fails on your Linux machine, try installing `esptool` and `mpremote` via `pipx`.

```console
python scripts/badge.py init
```

The following commands will try to connect to ESP32 chip on the badge via
the USB-C connector (the badge has has to be turned on with small switch SW2
near the SELECT button).

Erase the chip, flash that image, and upload the application:

```console
python scripts/badge.py flash
```

The same with full wipe (no previous settings restored)

```console
python scripts/badge.py flash --wipe
```

When reachable before erasing, the tool preserves the badge ID, holder name,
and saved parameters. Add `--holder-name "Ada Lovelace"` to set the name during
either `flash` or `upload`. Add `--wipe` to either command to skip reading and
preserving existing settings. This saves time when flashing a brand-new or
already empty badge and creates `badge.json` from the repository defaults.

Upload only application files:

```console
python scripts/badge.py upload
```

`flash` and `upload` identify the hardware version from the badge itself.
The 2025 prototype's OLED answers at I2C address 0x3D instead of 0x3C, and
GPIO4 reads about 3.3 V on 2025 badges (the SELECT button's pull-up) but
0.5-0.7 V on 2026 badges (the battery voltage divider). The tool prints what
it measured and the version it chose. If the reading is inconclusive (for
example, SELECT is held down), it falls back to the version in the badge's
`badge.json`. Pass `--badge-version 2026` (or `2025`, `2025_prototype`) to
override the detection; the tool warns if that disagrees with the hardware.
`flash` probes before erasing, so a badge without MicroPython (brand new or
erased) needs `--badge-version`.

The `upload` command replaces the badge's entire `/logos` directory with the
current sponsor set, so logos removed from the repository do not remain on the
badge.

Set or change the holder's name:

```console
python scripts/badge.py name "Ada Lovelace"
```

Delete every file from the MicroPython filesystem (not recoverable):

```console
python scripts/badge.py delete
```

Save the Wi-Fi link debug log (see [WiFi neighbours](#wifi-neighbours)) from
the badge:

```console
python scripts/badge.py logs
python scripts/badge.py logs --output my-run.txt --clear
```

By default the log is written to `badge-logs/linklog-<device ID>-<time>.txt`
(ignored by git). The command first flushes lines the running application
still holds in RAM, then saves the previous and current log file oldest
first. `--clear` deletes the log on the badge after it has been saved. The
badge restarts afterwards.

The tool auto-detects a likely ESP32 serial port. If detection is ambiguous,
pass `--port COM4`, `--port /dev/ttyACM0`, or the relevant macOS
`/dev/cu.usbmodem*` path after the command name. Upload and name operations check
the installed MicroPython version against the latest stable release by default;
use `--skip-version-check` only when working offline. `__pycache__` directories,
`*.pyc`, `.DS_Store`, `requirements.txt`, and the template
`software/badge.json` are never copied as ordinary files. Instead, the tool
generates `badge.json`, preserving device settings unless `--wipe` is used and
adding the selected hardware version plus the current eight-character git hash
and branch.

If `mpremote` cannot interrupt the running application, hold SELECT while
resetting or power-cycling the badge. The correct SELECT pin is chosen from
`badge.json` on both 2025 and 2026 hardware.

If your badge is somehow bricked (wrong version flashed, etc.) and does not
respond to `esptool`, try  holding down BACK button (GPIO9) while resetting
or turning your badge on. This will start ESP32 in bootloader mode.

For several badges at once, the `Makefile` wraps these commands. It runs
`uv run python3` by default (set `PY=python3` to change that) against the
ports in `PORTS` (default `/dev/ttyACM0 /dev/ttyACM1`):

```console
make upload                           # upload to every badge, version detected
make logs                             # save every badge's link log
make logs CLEAR=1                     # ...and delete them on the badges
make wifi-test                        # Wi-Fi-only tic-tac-toe test, in parallel
make wifi-check                       # upload, wifi-test, then logs
make upload PORTS=/dev/ttyACM1 BADGE_VERSION=2026
```

`wifi-test` keeps each badge's output in `badge-logs/wifi-test-<port>.txt` and
fails unless every badge printed PASS. `wifi-check` saves the logs even when
the test fails.

Run `python scripts/badge.py --help` or a subcommand with `--help` for all
options. After a successful operation on a 2026 badge, the tool prints the
currently measured battery voltage as its final output line. It adds
`WARNING!!` when the voltage is below 3.8 V or above 4.2 V.

## WiFi neighbours

**Menu -> Utils -> WiFi neighbours** is a proof of concept for badge-to-badge
Wi-Fi. It turns on broadcast ESP-NOW on channel 1, the same channel
Tic-tac-toe uses, and sends a beacon with the badge ID and holder name once a
second. It lists every badge it hears: other badges on this screen and badges
running Tic-tac-toe.

- The list shows the last eight characters of each badge ID (or `m` and the
  end of its MAC address if the sender is unknown), the signal strength in
  dBm, and how long ago it was last heard. The header counts badges heard in
  the last ten seconds.
- SELECT switches to the debug console and back. The console shows the Wi-Fi
  link log, including anything Tic-tac-toe logged earlier since the last
  restart.
- NEXT and PREV scroll. The console follows new lines while it is scrolled to
  the bottom.
- BACK turns the radio off and returns to Utils.

The link log records ESP-NOW start-up (channel and MAC address, or the
exact error), every beacon and frame received with its signal strength,
badges appearing, disappearing and returning, and all Tic-tac-toe link
traffic. Every line also goes to the serial console and to `/linklog.txt` on
the badge. That file is limited to 16 KiB; when it is full it becomes
`/linklog.old.txt`, so up to 32 KiB of history survives restarts. Writes are
batched (every 16 lines or 2 seconds) to limit flash wear. Use
`python scripts/badge.py logs` to save it on a computer.

If a badge shows **Radio failed**, press SELECT: the console shows why the
radio could not start. If two badges both show Wi-Fi as up but neither lists
the other, compare their logs: check that both report channel 1, and that
received (`rx`) lines appear on at least one side.

## Games

Open **Menu -> Games** to select an installed game. The menu discovers Python
files in `software/games` at runtime. Flappy Bird, Pacman, Snake, Tetris, and
the two-player games Pong and Tic-tac-toe are included.

To add a game, place a `.py` file in `software/games`. It must export:

```python
GAME_NAME = "My game"
GameScreen = MyGameScreen
```

The menu reads `GAME_NAME` from the file without importing the game, so it
must be a plain string literal at the start of its own line. Only the game
you open is imported; any previously opened game is unloaded first, and all
games are unloaded when you leave the Games menu. This keeps memory free for
games. The Wi-Fi driver's memory is reserved once at boot, before the menu
loads: its buffers live outside the Python heap, and the heap grows into that
space as the menu and games load, so starting Wi-Fi for the first time inside
a game could fail with `WiFi Out of Memory`. The radio stays off until
Tic-tac-toe or WiFi neighbours turns it on.

`GameScreen(oled)` must provide `render()` and async `handle_button(btn)`
methods. Set `manages_own_render = True` when the game owns an animation loop.
On exit, return `bsides.GamesScreen(oled)`.

### Flappy Bird

Fly through the gaps between scrolling pipes. Each pipe passed scores one
point, and the gaps narrow as the score rises. Touching a pipe or the ground
ends the run; the top of the playfield is safe.

- SELECT or NEXT flaps. The first flap starts the run. Holding NEXT does not
  repeat.
- PREV pauses and resumes. A flap also resumes.
- BACK exits to the Games menu. SELECT restarts after game over.

The high score is stored as `FlappyHighScore` in `badge.json`.

### Pacman

Single player on a 32x12 cell maze with three ghosts, power pellets, and a
wrap-around tunnel. Turns are relative to the current heading and are applied
at the first cell where they fit:

- NEXT turns clockwise, PREV turns counter-clockwise. Press the same button
  twice to reverse.
- SELECT pauses and resumes, or restarts after game over.
- BACK exits to the Games menu.

Dots score 10, power pellets 50, and eaten ghosts 200, 400, 800, and 1600 in a
row. An extra life is awarded at 10000 points. Clearing the maze starts the next
level with faster ghosts. The high score is stored as `PacmanHighScore` in
`badge.json`.

### Tetris

A 10x15 well in the middle of the display with the next piece, level, and line
count on the left and the score and high score on the right. A dot marks where
the falling piece will land.

- NEXT moves right and PREV moves left. Hold either to keep sliding.
- SELECT rotates. Hold SELECT for a soft drop.
- BACK exits to the Games menu. SELECT restarts after game over.

Clearing one to four lines at once scores 40, 100, 300, or 1200 times the
level, and each soft-dropped row adds one point. The level rises every ten
lines and gravity speeds up with it. The high score is stored as
`TetrisHighScore` in `badge.json`.

### Pong link

Pong uses UART1 on GPIO20/GPIO21. Cross-connect TX to RX in both directions and
connect GND between badges:

- Badge A TX (pin 28) -> Badge B RX (pin 27)
- Badge B TX (pin 28) -> Badge A RX (pin 27)
- Badge A GND -> Badge B GND

Open Pong on both badges. The higher device ID becomes host; after a three-second
countdown, the match lasts 60 seconds. NEXT moves up, SELECT moves down, and
BACK exits. Pong resumes pairing automatically after a link interruption;
reconnection starts a new match. Both badges need the same current Pong version
because UART packets now include a checksum.

### Tic-tac-toe link

Tic-tac-toe is the second two-player game and uses the same cable as Pong: TX
to RX in both directions plus GND. Open Tic-tac-toe on both badges. The higher
device ID becomes host, owns the board, and plays X. The other badge plays O.
The first game starts with X and the starter alternates after that.

- NEXT and PREV move the blinking cursor to the next or previous empty cell.
  A small dot shows where the other player's cursor is.
- SELECT places your mark on your turn, and starts a new game once one is
  finished. Either player can start the new game.
- BACK exits.

The side panel shows your mark, whose turn it is, and the session score as your
wins against your losses.

Every message carries a checksum, the host repeats the whole board a few times
per second, and the guest repeats a move until the board shows it. A dropped or
garbled line therefore cannot desynchronise the game. If the cable is
unplugged, both badges show "Link lost" and resume the same game when it is
plugged back in. If one badge leaves and reopens the game, a returning guest
gets the board back, while a returning host starts a fresh game. Pong traffic
on the other end is ignored, so both badges must run the same game.

Every message is also broadcast over ESP-NOW at the same time as the cable, so
two badges in Wi-Fi range can play with no cable at all, and a badge whose
espnow bring-up fails (older firmware, radio issue) transparently falls back
to cable-only play. Both links run together rather than one being chosen over
the other: whichever one is up carries the game, and since every message is
guarded by the current game number, the same message arriving twice (once per
link) is ignored rather than double-applied.

The Wi-Fi side is a proof of concept. No badge joins an access point, so both
radios are set to a fixed channel (`CHANNEL` in `software/espnow_link.py`,
currently channel 1); otherwise ESP-NOW has nothing that makes the two
badges use the same channel. While `DEBUG_LINK` in `tictactoe.py` is on (the
default for now), the game adds phase changes, hello contents, and every
frame sent or received on both the cable and the radio to the
[link log](#wifi-neighbours). ESP-NOW start-up and errors are always logged.
After a failed pairing, open **Utils -> WiFi neighbours** and press SELECT to
see the log on the badge, or save it with `python scripts/badge.py logs`. A
badge whose log has no `espnow up ch 1 mac ...` line has a radio or firmware
problem, not a pairing problem. Set `DEBUG_LINK = False` once linking works on
real hardware.

To test the Wi-Fi link on two uploaded badges without the cable, run the
scripted game on both at the same time (replace the ports with yours):

```console
for port in /dev/ttyACM0 /dev/ttyACM1; do
  python -m mpremote connect "$port" run tests/tictactoe_wifi_pair_hardware.py &
done; wait
```

Each badge ignores its UART receiver, links over ESP-NOW, and plays one game
in which both sides take the first empty cell. Each prints lines starting
with `TTT-WIFI <device ID>:` ending in `PASS` or `FAIL: <reason>`, plus the
free memory before and after. This runs outside the menu. To check that Wi-Fi
also starts from **Games -> Tic-tac-toe**, open the game on both badges
without the cable and save both logs with `python scripts/badge.py logs`.
