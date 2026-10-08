"""Best-effort automatic control of a Focusrite Scarlett 4i4 4th Gen's
on-device direct monitor mix, so the Record page's Production/Recording
Monitoring toggle (see Backend.set_monitoring_mode) can flip the audio
interface's own zero-latency hardware monitor along with the software
mix, instead of only reminding the operator to do it by hand.

There's no official API for this — Focusrite Control 2 is the only
supported way to touch it. This module is a small, targeted reimplementation
of the relevant slice of Focusrite's USB control protocol, ported from the
GPL-licensed Linux kernel driver (sound/usb/mixer_scarlett2.c, by Geoffrey
Bennett et al.), which fully documents the reverse-engineered wire format.
It talks to the device directly over USB via pyusb — no Focusrite Control 2
involvement — which also means it only works when Focusrite Control 2 isn't
running (it holds the control interface open exclusively).

Each of the 4i4's 4 analogue inputs has its own permanently dedicated
channel-strip fader (crosspoint index = channel - 1) present in every one
of its 8 internal mixes — confirmed against Focusrite Control 2's own
Mixer view, where e.g. "Guitar / Analogue 1" and "SM57 / Analogue 2" each
appear as a fader feeding whichever mix tab is open. So "direct monitor
on/off" for a given input is just: zero or restore that channel's own
fader, in whichever mix(es) actually feed the monitor outputs. No routing
(MUX) changes are ever needed — every input is already permanently wired
in; only its level in the relevant mix(es) changes.

MONITOR_MIX_BUSES below is this studio's own choice, not a device default:
per its Focusrite Control 2 Routing tab, both "Headphones" and "Output 1-2"
are set to draw from "Mix A" (raw 0-indexed buses 0 and 1 — Focusrite Control
2 shows one merged stereo "Mix A" selector, but the underlying protocol
addresses its Left/Right halves as two separate mix buses). If that routing
is ever changed in Focusrite Control 2 to draw from a different mix, this
constant needs to change to match.

Every public function fails soft: any USB/protocol error (device not
plugged in, pyusb/libusb unavailable, wrong model, or — the common case —
Focusrite Control 2 already holding the control interface open) is caught
and turned into a False return rather than raised. This is always a
nice-to-have layered on top of the Record page's on-screen manual-toggle
reminder, never a hard requirement for recording to work.
"""

from __future__ import annotations

import struct
import time

FOCUSRITE_DEVICE_NAME = "Scarlett 4i4 4th Gen"

_VENDOR_ID = 0x1235
_PRODUCT_ID = 0x821A  # Scarlett 4i4 4th Gen

_CMD_INIT = 0
_CMD_REQ = 2
_CMD_RESP = 3

_USB_INIT_1 = 0x00000000
_USB_INIT_2 = 0x00000002
_USB_GET_MIX = 0x00002001
_USB_SET_MIX = 0x00002002

_CLASS_INTERFACE_OUT = 0x21  # USB_TYPE_CLASS | USB_RECIP_INTERFACE | USB_DIR_OUT
_CLASS_INTERFACE_IN = 0xA1   # USB_TYPE_CLASS | USB_RECIP_INTERFACE | USB_DIR_IN

_NUM_MIX_IN = 10  # s4i4_gen4_info: port_count[MIX][OUT] (12) minus dsp_count (2)
_NUM_ANALOGUE_INPUTS = 4

# See module docstring — this studio's Focusrite Control 2 Routing tab has
# both Headphones and Output 1-2 set to "Mix A", whose Left/Right halves
# are raw mix-bus indices 0 and 1.
MONITOR_MIX_BUSES = (0, 1)

# Gain applied to a channel's fader when direct monitor is turned on —
# unity (0 dB); confirmed audible at a sane level in live testing.
DIRECT_MONITOR_GAIN = 8192


def _channel_to_crosspoint(channel: int) -> int | None:
    if 1 <= channel <= _NUM_ANALOGUE_INPUTS:
        return channel - 1
    return None


class _ProtocolError(Exception):
    pass


class _Scarlett2Device:
    """One-shot connection: open, do a handful of get/set calls, close.
    Not held open persistently — this is a rarely-triggered, user-initiated
    action (a monitoring-mode toggle or a take starting), not a hot path."""

    def __init__(self, usb_core, usb_util):
        self._usb_core = usb_core
        self._usb_util = usb_util
        self.dev = usb_core.find(idVendor=_VENDOR_ID, idProduct=_PRODUCT_ID)
        if self.dev is None:
            raise _ProtocolError("Scarlett 4i4 4th Gen not found on USB")
        self.iface_num, self.int_ep = self._find_control_interface()
        usb_util.claim_interface(self.dev, self.iface_num)
        self.seq = 1

    def _find_control_interface(self) -> tuple[int, int]:
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            if intf.bInterfaceClass == 0xFF:  # Vendor Specific
                ep = intf.endpoints()[0]
                return intf.bInterfaceNumber, ep.bEndpointAddress
        raise _ProtocolError("no vendor-specific control interface found")

    def close(self) -> None:
        try:
            self._usb_util.release_interface(self.dev, self.iface_num)
        except Exception:
            pass

    def __enter__(self) -> "_Scarlett2Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- low level ---

    def _ctrl_out(self, bRequest: int, data: bytes) -> None:
        n = self.dev.ctrl_transfer(_CLASS_INTERFACE_OUT, bRequest, 0, self.iface_num, data, timeout=2000)
        if n != len(data):
            raise _ProtocolError(f"short OUT write: {n} != {len(data)}")

    def _ctrl_in(self, bRequest: int, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(_CLASS_INTERFACE_IN, bRequest, 0, self.iface_num, length, timeout=2000))

    def _wait_ack(self, timeout_ms: int = 2000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            remaining_ms = int((deadline - time.monotonic()) * 1000) or 1
            try:
                data = self.dev.read(self.int_ep, 64, timeout=remaining_ms)
            except self._usb_core.USBError as e:
                if "timeout" in str(e).lower() or getattr(e, "errno", None) == 60:
                    raise _ProtocolError("timed out waiting for ACK notification") from e
                raise
            if len(data) == 8 and struct.unpack("<I", bytes(data[:4]))[0] & 1:
                return
        raise _ProtocolError("timed out waiting for ACK notification")

    def _usb(self, cmd: int, req_data: bytes, resp_size: int, seq_quirk: bool = False) -> bytes:
        seq = self.seq
        self.seq += 1
        self._ctrl_out(_CMD_REQ, struct.pack("<IHHII", cmd, len(req_data), seq, 0, 0) + req_data)
        self._wait_ack()
        resp = self._ctrl_in(_CMD_RESP, 16 + resp_size)
        r_cmd, r_size, r_seq, r_error, _r_pad = struct.unpack("<IHHII", resp[:16])
        if r_cmd != cmd:
            raise _ProtocolError(f"cmd mismatch: sent {cmd:#x} got {r_cmd:#x}")
        if r_seq != seq and not (seq_quirk and seq == 1 and r_seq == 0):
            raise _ProtocolError(f"seq mismatch: sent {seq} got {r_seq}")
        if r_size != resp_size:
            raise _ProtocolError(f"size mismatch: expected {resp_size} got {r_size}")
        if r_error:
            raise _ProtocolError(f"device reported error {r_error:#x}")
        return resp[16:16 + resp_size]

    def init(self) -> None:
        self._ctrl_in(_CMD_INIT, 24)  # step 0, response discarded
        time.sleep(0.02)
        self.seq = 1
        self._usb(_USB_INIT_1, b"", 0, seq_quirk=True)
        self.seq = 1
        self._usb(_USB_INIT_2, b"", 84, seq_quirk=True)

    def get_mix(self, mix_num: int) -> list[int]:
        req = struct.pack("<HH", mix_num, _NUM_MIX_IN)
        resp = self._usb(_USB_GET_MIX, req, _NUM_MIX_IN * 2)
        return list(struct.unpack(f"<{_NUM_MIX_IN}H", resp))

    def set_mix(self, mix_num: int, gains: list[int]) -> None:
        req = struct.pack(f"<H{_NUM_MIX_IN}H", mix_num, *gains)
        self._usb(_USB_SET_MIX, req, 0)


def set_channel_gain(channel: int, volume: float) -> bool:
    """Best-effort: set 1-based analogue input `channel`'s own channel-strip
    fader (see _channel_to_crosspoint) to `volume` (0.0 = off, 1.0 =
    DIRECT_MONITOR_GAIN, the unity level "confirmed audible at a sane level
    in live testing" — see module docstring) in the mix bus(es) that feed
    the monitor outputs (MONITOR_MIX_BUSES). Clamped to [0.0, 1.0]: this
    reverse-engineered protocol has no confirmed-safe value for boosting a
    channel past that validated unity level, so callers wanting more volume
    than that need to do it in software instead.

    Returns True if it actually took effect, False if unavailable for any
    reason (pyusb/libusb missing, device not found, wrong device, Focusrite
    Control 2 has the control interface open, protocol error, etc.) —
    callers should treat False as "fall back to the on-screen reminder",
    not as an error to surface to the user.
    """
    return set_channel_gains({channel: volume})


def set_channel_gains(volumes: dict[int, float]) -> bool:
    """set_channel_gain for several channels at once — one USB session and
    at most one set_mix per bus, rather than reopening the device per
    channel. Used to monitor every input at once, or to mute every input
    but one (see backend.py's _apply_hardware_direct_monitor). Same
    best-effort True/False contract as set_channel_gain."""
    targets: dict[int, int] = {}
    for channel, volume in volumes.items():
        crosspoint = _channel_to_crosspoint(channel)
        if crosspoint is None:
            return False
        targets[crosspoint] = int(DIRECT_MONITOR_GAIN * max(0.0, min(1.0, volume)))
    if not targets:
        return True

    try:
        import usb.core
        import usb.util
    except Exception:
        return False

    try:
        with _Scarlett2Device(usb.core, usb.util) as dev:
            dev.init()
            for bus in MONITOR_MIX_BUSES:
                gains = dev.get_mix(bus)
                changed = False
                for crosspoint, gain in targets.items():
                    if gains[crosspoint] != gain:
                        gains[crosspoint] = gain
                        changed = True
                if changed:
                    dev.set_mix(bus, gains)
        return True
    except Exception:
        return False
