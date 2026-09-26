"""Fake hardware for the badge_link and two-player game tests.

A FakeUart is one end of a cable; an Air is the radio space that FakeEspNow
radios share. Both can drop and corrupt traffic. install() puts the fake
machine, network and espnow modules (and a controllable clock) in place and
loads fresh copies of the badge's link modules from software/.
"""

import importlib.util
import random
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BROADCAST = b"\xff\xff\xff\xff\xff\xff"


class Clock:
    now = 100000

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_add(a, b):
        return a + b


class FakeUart:
    """One end of a cable. Lines written here arrive at .peer, unless the
    cable is unplugged or the lossy filter drops or corrupts them."""

    def __init__(self, *_args, **_kwargs):
        self.rx = b""
        self.peer = None
        self.plugged = True
        self.loss = 0.0
        self.sent = []
        self.closed = False

    def write(self, data):
        self.sent.append(data)
        if not self.peer or not self.plugged or not self.peer.plugged:
            return
        roll = random.random()
        if roll < self.loss:
            return
        if roll < self.loss * 1.5:
            data = bytes([data[0], data[1] ^ 0x01]) + data[2:]   # bit flip
        self.peer.rx += data

    def any(self):
        return len(self.rx)

    def read(self, n):
        data, self.rx = self.rx[:n], self.rx[n:]
        return data

    def deinit(self):
        self.closed = True


def wire(a, b):
    """Plug a cable between two FakeUarts."""
    a.peer, b.peer = b, a


class Air:
    """Radio space shared by FakeEspNow radios. A broadcast reaches every
    active radio in it, a unicast only the radio with that MAC."""

    default = None          # the Air new radios join, None for none

    def __init__(self, loss=0.0):
        self.radios = []
        self.loss = loss

    def join(self, *radios):
        for radio in radios:
            if radio.air is not None:
                radio.air.radios.remove(radio)
            radio.air = self
            self.radios.append(radio)

    def carry(self, src, mac, msg):
        for radio in self.radios:
            if radio is src or not radio.active_:
                continue
            if mac != BROADCAST and mac != radio.mac:
                continue
            if random.random() < self.loss:
                continue
            radio.inbox.append((src.mac, bytes(msg)))


class FakeEspNow:
    """One badge's ESP-NOW radio, with a MAC address of its own."""

    macs = 0
    last_mac = None

    def __init__(self, *_args, **_kwargs):
        FakeEspNow.macs += 1
        self.mac = bytes([0x02, 0, 0, 0, FakeEspNow.macs >> 8,
                          FakeEspNow.macs & 0xFF])
        FakeEspNow.last_mac = self.mac
        self.active_ = False
        self.peers = set()
        self.inbox = []
        self.sent = []
        self.air = None
        if Air.default is not None:
            Air.default.join(self)

    def active(self, value=None):
        if value is None:
            return self.active_
        self.active_ = value

    def add_peer(self, mac):
        if mac in self.peers:
            raise OSError(-12395, "ESP_ERR_ESPNOW_EXIST")
        self.peers.add(mac)

    def del_peer(self, mac):
        if mac not in self.peers:
            raise OSError(-12394, "ESP_ERR_ESPNOW_NOT_FOUND")
        self.peers.remove(mac)

    def send(self, mac, msg, sync=True):
        if mac not in self.peers:
            raise OSError(-12394, "ESP_ERR_ESPNOW_NOT_FOUND")
        self.sent.append((mac, bytes(msg)))
        if self.active_ and self.air is not None:
            self.air.carry(self, mac, msg)

    def recv(self, _timeout_ms=0):
        if self.inbox:
            return self.inbox.pop(0)
        return (None, None)


class FakeWLAN:
    def __init__(self, *_args, **_kwargs):
        self.channel = None

    def active(self, _value=None):
        return True

    def disconnect(self):
        pass

    def config(self, *args, **kwargs):
        if kwargs:
            self.channel = kwargs.get("channel", self.channel)
            return None
        if args and args[0] == "mac":
            return FakeEspNow.last_mac or b"\x02\x00\x00\x00\x00\x00"
        return None


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STUBBED = ("machine", "network", "espnow", "urandom", "time")
_saved = {}


def install():
    """Install the fakes and load linklog, espnow_link and badge_link.

    Modules loaded afterwards (a game) bind the fakes too; call restore()
    once they are loaded so other tests and unittest get the real modules
    back."""
    for name in STUBBED:
        _saved[name] = sys.modules.get(name)
    machine = types.ModuleType("machine")
    machine.UART = FakeUart
    machine.Pin = lambda *args, **kwargs: None
    sys.modules["machine"] = machine

    network = types.ModuleType("network")
    network.WLAN = FakeWLAN
    network.STA_IF = 0
    sys.modules["network"] = network

    espnow_module = types.ModuleType("espnow")
    espnow_module.ESPNow = FakeEspNow
    sys.modules["espnow"] = espnow_module

    sys.modules["urandom"] = types.SimpleNamespace(
        getrandbits=lambda bits: random.getrandbits(bits))
    time_stub = types.ModuleType("time")
    time_stub.ticks_ms = Clock.ticks_ms
    time_stub.ticks_diff = Clock.ticks_diff
    time_stub.ticks_add = Clock.ticks_add
    sys.modules["time"] = time_stub

    linklog = load("linklog", ROOT / "software" / "linklog.py")
    log_dir = Path(tempfile.mkdtemp(prefix="linklog-"))
    linklog.LOG_PATH = str(log_dir / "linklog.txt")
    linklog.OLD_PATH = str(log_dir / "linklog.old.txt")
    linklog.print = lambda *args, **kwargs: None
    espnow_link = load("espnow_link", ROOT / "software" / "espnow_link.py")
    espnow_link.gc = types.SimpleNamespace(
        collect=lambda: None, mem_free=lambda: 123456)
    badge_link = load("badge_link", ROOT / "software" / "badge_link.py")
    return linklog, espnow_link, badge_link


def restore():
    for name in STUBBED:
        if _saved.get(name) is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = _saved[name]
    if not hasattr(sys.modules.get("time"), "monotonic"):
        sys.modules.pop("time", None)
        importlib.import_module("time")
