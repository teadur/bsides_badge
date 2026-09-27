# Wi-Fi badge games: handoff

Branch: `claude/badge-wifi-extension-uja3jh` (teadur/bsides_badge), on top of
`758acfe` (the original UART-cable two-player tic-tac-toe).

Goal: bring badge-to-badge play over Wi-Fi (ESP-NOW) to every two-player
game, with a real lobby (so a room full of badges doesn't break pairing),
without regressing the cable path or the single-player games' memory
budget. Done in four phases, each committed and pushed separately.

## Where things stand

Everything below is committed and pushed. Unit tests: 198, all passing
(`make test` / `python3 -m unittest discover -s tests`, ~43s). Nothing is
merged yet - no PR has been opened (the user didn't ask for one).

| Phase | Commit | What |
| --- | --- | --- |
| 1 | `fd4f8df` | `badge_link` lobby module; tic-tac-toe migrated onto it |
| 2 | `424a132` | Pong migrated onto `badge_link`; G/E/R folded into the state stream |
| 3 | `5d25c86` | Menu-path hardware tests for Pong and every single-player game |
| 4 | (this session, uncommitted at hand-off time - see below) | `DEBUG_LINK` off by default; README pass |
| - | `1c3d670` | Unrelated fix: `badge.py` recovers a badge stuck on the serial port (came up mid-session when the user hit it) |

**Phase 4 is in progress and not yet committed.** Working tree has:
`DEBUG_LINK = False` in both `software/games/tictactoe.py` and
`software/games/pong.py` (was `True`); the two `EspNowLinkTests.setUp`
methods in `tests/test_tictactoe.py` / `tests/test_pong.py` now wrap
pairing in `patch.object(<module>, "DEBUG_LINK", True)` so the
debug-logging test still exercises real debug output; and a README pass
(the DEBUG_LINK paragraph rewritten, Pong added to the "radio stays off
until..." line). All tests pass. Still to do: commit and push this, then
tell the user it's ready for review/PR if they want one.

## Architecture

### The lobby: `software/badge_link.py` (`PeerLink`)

Shared by any two-player game. Replaces the old "broadcast a hello and
accept whoever answers" pairing (which breaks with more than two badges in
range) with an explicit lobby:

- Badges advertise themselves (`A<id>,<name>`) once a second while
  unpaired, and list who else they hear.
- NEXT/PREV picks a player, SELECT invites (`I<from>,<to>,<session>`,
  repeated until answered); the other badge accepts (`K`) or declines
  (`N`). A badge already in a game declines as busy.
- A cable auto-pairs (no lobby UI needed - two badges on a cable are
  unambiguously a pair).
- Once paired, both badges share a random session code. Every game message
  (`G<session>,<message>`) goes by ESP-NOW unicast to the partner's MAC (or
  over the cable), and a badge only accepts a game message that matches
  both its current session code and (over the radio) the partner's MAC.
  This is what lets any number of pairs play in one room without
  interfering - confirmed by `tests/test_badge_link.py`'s `CrowdTests`
  (several pairs plus bystanders on one lossy shared channel).
- `X<session>` signals a deliberate leave, returning the partner to its
  lobby.

`PeerLink` owns the transport (cable UART + ESP-NOW), the lobby state
machine, and rendering the lobby screen (`render(oled, title)` - text only,
no fill/show, so the game controls the frame). The game only sees
`on_paired()` / `on_unpaired(reason)` / `on_message(payload)` callbacks and
calls `link.send(payload)`, `link.poll(now)`, `link.tick(now)`,
`link.handle_button(btn)`.

`game_loader.py`'s `GAME_HELPERS` list includes `"badge_link"`, so it gets
unloaded along with the game module when the player leaves - keeping its
~6 KB out of the heap when Wi-Fi needs it (this was the original motivating
bug: badges running out of memory when Wi-Fi first turned on inside a
game).

### The two games' own protocols, on top of the lobby

Both games still run their own protocol atop `PeerLink.send`/`on_message`
- the lobby is purely transport/pairing, not game state.

**Tic-tac-toe** (`software/games/tictactoe.py`): unchanged protocol
(H/S/C messages - hello, host's state, guest's client message), just
carried over `link.send()` instead of raw UART/ESP-NOW. Still does its own
post-pairing H handshake for host election (higher device ID hosts) and
restart/resume detection, independent of the lobby.

**Pong** (`software/games/pong.py`): redesigned, not just re-plumbed. The
original protocol had single-shot messages - `G` (start), `E` (match
over), `R` (rematch request) - that could get lost on a lossy Wi-Fi link
and desync the game or hang it. Phase 2 folded all three into the existing
repeating heartbeats:

- Host's `S<match_no>,<phase C/P/O>,<bx>,<by>,<hy>,<sl>,<sr>,<time_left>`
  is sent every ~100 ms *whenever paired*, including once the match is
  over - so "game over" is inferred from the next heartbeat, not a message
  that could be dropped.
- Guest's `C<match_no>,<paddle_y>,<rematch 0/1>` is sent every ~200 ms the
  same way - a rematch request just goes out again next tick if lost.
- Both carry `match_no`, so a stale message from a previous match is
  ignored rather than reopening it.
- Side effect (intentional, documented in the README): a lost link now
  *pauses* the match in place (frozen ball, kept score) and resumes
  exactly once messages flow again, instead of always forcing a fresh
  match. Only BACK or a real unpair sends a badge back to the lobby.

### Tests

- `tests/link_fakes.py`: shared fakes (FakeUart, an `Air` class modeling a
  shared lossy ESP-NOW radio space with per-MAC unicast, `install()` /
  `restore()` for swapping in machine/network/espnow/time stubs and loading
  fresh `linklog`/`espnow_link`/`badge_link` module instances per test
  file). Used by `test_badge_link.py`, `test_tictactoe.py`, `test_pong.py`.
- `tests/test_badge_link.py`: lobby mechanics in isolation - frames,
  invite/accept/decline/timeout/cancel, cable auto-pair, session/MAC
  filtering, crowd tests with several pairs on one lossy channel.
- `tests/test_tictactoe.py` / `tests/test_pong.py`: each game's full
  protocol against the shared fakes, including lossy-link and crowd
  scenarios. Fuzzed against ~150-300 random seeds each during development
  (not part of the committed suite - the seeded tests use a fixed seed).
- Hardware scripts (`mpremote run tests/<name>.py`, wired to
  `make wifi-test` / `make menu-test` / `make pong-wifi-test` /
  `make pong-menu-test` / `make solo-test`, all rolled into
  `make badge-test`): each pairs two real badges (or, for `solo-test`,
  drives one) through the actual menu or a lightweight stand-in, and prints
  a `<TAG> <device ID>: PASS`/`FAIL: <reason>` line plus writes to the link
  log.
- A MicroPython-unix simulator lives in this session's scratchpad (not in
  the repo - see below) and was used repeatedly during development to
  rehearse hardware scripts end-to-end (real asyncio, a simulated ESP-NOW
  radio) before ever touching real hardware. It caught real bugs, e.g. an
  early "unload the wrong modules" upload bug in Phase 0/1 work, and a
  false-positive in the Phase 3 memory-leak check (the link log's own
  bounded ring buffer filling up for the first time looks like a leak
  until primed).

### The scratchpad simulator (not committed, may not survive this session)

Lives under this session's scratchpad directory
(`/tmp/claude-.../scratchpad/`), not in the repository:

- `micropython/` - a MicroPython 1.29 unix build
  (`ports/unix/build-standard/micropython`), built with a frozen asyncio
  manifest (`FROZEN_MANIFEST=$S/asyncio_manifest.py`) since unix
  MicroPython doesn't otherwise ship `uasyncio`.
- `menusim/` - a staged copy of the real firmware tree (via
  `scripts/badge.py`'s own `stage_upload_files`/`upload_files`, precompiled
  to `.mpy`), plus `simlaunch.py` (fake `machine`/`network`/`espnow`
  modules, a shared `AIR` list standing in for the radio, a real
  `framebuf.FrameBuffer`-backed OLED) and `simlaunch_pong.py` (same idea,
  peer plays Pong instead of tic-tac-toe).
- Rehearsal was: restage `menusim/` from current sources, copy the
  hardware test script(s) in, run
  `micropython simlaunch.py <script>.py` (or `simlaunch_pong.py` for
  Pong), read the output.
- **This is not part of the deliverable** and doesn't need to move into
  the repo unless the user wants a standing simulator for future work - it
  was a development aid. If a future session wants to rebuild it: the
  build commands and `simlaunch.py`/`simlaunch_pong.py` contents are fully
  described in this session's transcript; the gist is
  `stage_upload_files(upload_files(), root, True)` from `scripts/badge.py`
  plus the fake hardware modules shown above.

## Design decisions worth knowing about

- **Prefixes**: tic-tac-toe is `b"T"`, Pong is `b"Q"` (matching its
  historical wire tag from before the lobby). `wifi_neighbours.py`'s
  `describe()` has a `LINK_GAMES` dict mapping prefix -> short name so
  adding a third `badge_link` game only needs one new entry there.
- **`DEBUG_LINK`**: per-game module constant (not a `badge_link` setting),
  gates the *game's own* phase-change/hello logging; `badge_link`'s own
  frame-level tx/rx dump is gated by the `debug=` arg passed to
  `PeerLink()`, which each game wires to its own `DEBUG_LINK`. Pairing
  events themselves (`inviting`, `paired with`, `unpaired`) are logged by
  `badge_link` unconditionally, regardless of the flag - only the
  per-frame noise is gated. As of Phase 4, both default to `False`; turn
  either on to chase a real link problem.
- **Pong's "pause, don't restart" behaviour on a lost link** is a
  deliberate improvement over the pre-Wi-Fi cable-only design (which
  always restarted on any interruption) - flagged explicitly in this
  handoff because it's a behavior change a reviewer might ask about.
- **`game_loader.GAME_HELPERS`**: currently just `["badge_link"]`. If a
  third helper module gets shared between games later, add it here rather
  than teaching `game_loader` about it more specifically.

## Suggested next steps (not started)

The original plan had an optional fifth item, not committed to:

- **Single-player Wi-Fi extras** - nothing concrete was scoped for this;
  it was listed as "optional" in the original plan and never picked up.
  Ask the user before starting anything here.

Otherwise: the four planned phases are complete. Once Phase 4 is committed
and pushed, a reasonable next message to the user is: summarize what
changed, confirm `make test` is green, and ask whether they want a PR
opened (per this session's standing instruction, never open one
unprompted).

## Files touched across all four phases

```
software/badge_link.py                 new (Phase 1)
software/games/tictactoe.py            migrated onto badge_link (Phase 1); DEBUG_LINK off (Phase 4)
software/games/pong.py                  migrated onto badge_link + protocol redesign (Phase 2); DEBUG_LINK off (Phase 4)
software/espnow_link.py                 Broadcaster.send() takes a mac (Phase 1)
software/game_loader.py                 GAME_HELPERS unloads badge_link with the game (Phase 1)
software/wifi_neighbours.py             recognises badge_link frames for any game (Phase 1, extended Phase 2)
tests/link_fakes.py                     new (Phase 1)
tests/test_badge_link.py                new (Phase 1)
tests/test_tictactoe.py                 rewritten on link_fakes (Phase 1); DEBUG_LINK patch (Phase 4)
tests/test_pong.py                      rewritten on link_fakes (Phase 2); DEBUG_LINK patch (Phase 4)
tests/tictactoe_wifi_pair_hardware.py   pairs through the lobby (Phase 1)
tests/tictactoe_menu_hardware.py        pairs through the lobby (Phase 1)
tests/pong_wifi_pair_hardware.py        new (Phase 2)
tests/pong_pair_hardware.py             updated for badge_link API (Phase 2)
tests/pong_hardware_smoke.py            updated for badge_link API (Phase 2)
tests/pong_menu_hardware.py             new (Phase 3)
tests/singleplayer_menu_hardware.py     new (Phase 3)
Makefile                                pong-wifi-test/pong-menu-test/solo-test targets (Phase 3)
scripts/badge.py                        unrelated: wake_badge()/reset command (mid-session fix)
README.md                               updated throughout all four phases
```
