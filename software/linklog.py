"""Wi-Fi link debug log shared by every badge-to-badge Wi-Fi feature.

Each line goes to three places: the serial console, an in-memory ring buffer
that Utils -> WiFi neighbours shows on the OLED, and a size-capped file on
flash that survives resets and is pulled off the badge with
`python scripts/badge.py logs`.
"""

import os
import time


LOG_PATH = "/linklog.txt"
OLD_PATH = "/linklog.old.txt"
MAX_FILE_BYTES = 16384      # per file; the previous file is kept as OLD_PATH
FLUSH_LINES = 16            # batch flash writes instead of one per line
FLUSH_MS = 2000
RING_LINES = 60

lines = []                  # newest last
revision = 0                # bumps on every line so screens know to redraw
identity = ""               # device ID for the session header, set by users

_pending = []
_last_flush = time.ticks_ms()
_session_started = False


def _text(part):
    if isinstance(part, (bytes, bytearray)):
        try:
            return bytes(part).decode()
        except UnicodeError:
            return repr(bytes(part))
    return str(part)


def log(*parts):
    global revision
    now = time.ticks_ms()
    line = "%d.%03d %s" % (now // 1000, now % 1000,
                           " ".join(_text(p) for p in parts))
    print(line)
    lines.append(line)
    if len(lines) > RING_LINES:
        del lines[0]
    revision += 1
    _pending.append(line)
    if len(_pending) >= FLUSH_LINES or \
            time.ticks_diff(now, _last_flush) >= FLUSH_MS:
        flush()


def _size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return 0


def flush():
    global _last_flush, _session_started
    _last_flush = time.ticks_ms()
    if not _pending:
        return
    chunk = "\n".join(_pending) + "\n"
    if not _session_started:
        chunk = "--- session start, id %s ---\n" % (identity or "?") + chunk
    del _pending[:]
    try:
        size = _size(LOG_PATH)
        if size and size + len(chunk) > MAX_FILE_BYTES:
            try:
                os.remove(OLD_PATH)
            except OSError:
                pass
            os.rename(LOG_PATH, OLD_PATH)
        with open(LOG_PATH, "a") as f:
            f.write(chunk)
        _session_started = True
    except OSError as exc:
        print("linklog write failed:", exc)
