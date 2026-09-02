#!/usr/bin/env python3
"""Griffin PowerMate (USB 077d:0410) via Linux evdev. Sin dependencias extra."""

from __future__ import annotations

import argparse
import glob
import os
import select
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Union

DEVICE_BY_ID = (
    "/dev/input/by-id/usb-Griffin_Technology__Inc._Griffin_PowerMate-event-if00"
)
DEVICE_GLOB = "/dev/input/by-id/*PowerMate*"

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_MSC = 0x04
REL_DIAL = 0x07
BTN_0 = 0x100
MSC_PULSELED = 0x01

# struct input_event: timeval (2 longs) + type + code + value
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

TICKS_PER_TURN = 94

# El firmware mapea speed así:
#   0-254  divide (más lento; por debajo de ~250 el pulso casi no se ve)
#   255    velocidad normal
#   256+   multiply (más rápido; ~320 congela el micro)
# La banda útil en este hardware es 250-275.
PULSE_SPEED_MIN = 250
PULSE_SPEED_NORMAL = 255
PULSE_SPEED_MAX = 275
LED_WRITE_INTERVAL = 0.08


def pulse_level_to_speed(level: int) -> int:
    """Escala 0-100 del usuario a la banda útil del firmware (250-275)."""
    level = max(0, min(100, int(level)))
    span = PULSE_SPEED_MAX - PULSE_SPEED_MIN
    speed = PULSE_SPEED_MIN + int(round(span * level / 100.0))
    return max(PULSE_SPEED_MIN, min(PULSE_SPEED_MAX, speed))


@dataclass(frozen=True)
class Rotate:
    """Giro relativo. delta > 0 es horario, < 0 antihorario."""

    delta: int
    timestamp: float


@dataclass(frozen=True)
class Button:
    """Pulsación firme hacia abajo. pressed True = BTN_0 value 1."""

    pressed: bool
    timestamp: float


Event = Union[Rotate, Button]
RotateCallback = Callable[[Rotate], None]
ButtonCallback = Callable[[Button], None]


def pack_led(
    brightness: int,
    pulse_speed: int = 255,
    pulse_table: int = 0,
    pulse_asleep: bool = False,
    pulse_awake: bool = False,
) -> int:
    """Empaqueta el comando MSC_PULSELED del driver powermate del kernel.

    bits 0-7:   brillo estático 0-255
    bits 8-16:  velocidad de pulso (banda útil 250-275, 255 = normal)
    bits 17-18: tabla de pulso 0-2
    bit 19:     pulsar en sleep
    bit 20:     pulsar en awake (pulso continuo)
    """
    brightness = max(0, min(255, int(brightness)))
    pulse_speed = max(PULSE_SPEED_MIN, min(PULSE_SPEED_MAX, int(pulse_speed)))
    pulse_table = max(0, min(2, int(pulse_table)))
    return (
        brightness
        | (pulse_speed << 8)
        | (pulse_table << 17)
        | (int(bool(pulse_asleep)) << 19)
        | (int(bool(pulse_awake)) << 20)
    )


def find_device(path: Optional[str] = None) -> str:
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError(f"No existe el dispositivo: {path}")
        return os.path.realpath(path)
    if os.path.exists(DEVICE_BY_ID):
        return os.path.realpath(DEVICE_BY_ID)
    matches = sorted(glob.glob(DEVICE_GLOB))
    if matches:
        return os.path.realpath(matches[0])
    raise FileNotFoundError(
        "PowerMate no encontrado. Comprueba el USB y "
        "/dev/input/by-id/*PowerMate*"
    )


class PowerMate:
    def __init__(self, path: Optional[str] = None, grab: bool = False) -> None:
        self.path = find_device(path)
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
            self._can_write = True
        except PermissionError as exc:
            raise PermissionError(self._permission_hint()) from exc
        except OSError:
            self._fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            self._can_write = False
        self._grabbed = False
        self._last_led_value: Optional[int] = None
        self._last_led_write = 0.0
        self._pending_led: Optional[int] = None
        if grab:
            self.grab()

    @staticmethod
    def _permission_hint() -> str:
        return (
            "Sin permiso de lectura/escritura en el PowerMate.\n"
            "  sudo usermod -aG input $USER\n"
            "  sudo cp ~/pi/99-powermate.rules /etc/udev/rules.d/\n"
            "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
            "Cierra sesión y vuelve a entrar, o reinicia."
        )

    def close(self) -> None:
        if getattr(self, "_fd", None) is None:
            return
        if self._grabbed:
            try:
                self.ungrab()
            except OSError:
                pass
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> "PowerMate":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def grab(self) -> None:
        # EVIOCGRAB = _IOW('E', 0x90, int)
        import fcntl

        fcntl.ioctl(self._fd, 0x40044590, 1)
        self._grabbed = True

    def ungrab(self) -> None:
        import fcntl

        fcntl.ioctl(self._fd, 0x40044590, 0)
        self._grabbed = False

    def set_led(
        self,
        brightness: int = 128,
        *,
        pulse: bool = False,
        pulse_speed: int = 255,
        pulse_table: int = 0,
        pulse_asleep: bool = False,
    ) -> None:
        if not self._can_write:
            raise PermissionError(
                "El dispositivo se abrió solo lectura; no se puede controlar el LED.\n"
                + self._permission_hint()
            )
        value = pack_led(
            brightness,
            pulse_speed=pulse_speed,
            pulse_table=pulse_table,
            pulse_asleep=pulse_asleep,
            pulse_awake=pulse,
        )
        mode_changed = (
            self._last_led_value is None
            or bool(value & (1 << 20)) != bool(self._last_led_value & (1 << 20))
        )
        if value == self._last_led_value and self._pending_led is None:
            return
        now = time.monotonic()
        if mode_changed or (now - self._last_led_write) >= LED_WRITE_INTERVAL:
            self._write_led(value)
            self._pending_led = None
        else:
            self._pending_led = value

    def _write_led(self, value: int) -> None:
        payload = struct.pack(EVENT_FORMAT, 0, 0, EV_MSC, MSC_PULSELED, value)
        os.write(self._fd, payload)
        self._last_led_value = value
        self._last_led_write = time.monotonic()

    def _flush_led(self) -> None:
        if self._pending_led is None or not self._can_write:
            return
        if (time.monotonic() - self._last_led_write) < LED_WRITE_INTERVAL:
            return
        self._write_led(self._pending_led)
        self._pending_led = None

    def led_off(self) -> None:
        self.set_led(0, pulse=False)

    def led_solid(self, brightness: int) -> None:
        self.set_led(brightness, pulse=False)

    def led_pulse(self, pulse_speed: int = 255, brightness: int = 0) -> None:
        self.set_led(brightness, pulse=True, pulse_speed=pulse_speed)

    def read(self, timeout: Optional[float] = None) -> Optional[Event]:
        for event in self._read_available(timeout=timeout, first_only=True):
            return event
        return None

    def events(self) -> Iterator[Event]:
        while True:
            event = self.read(timeout=None)
            if event is not None:
                yield event

    def run(
        self,
        on_rotate: Optional[RotateCallback] = None,
        on_button: Optional[ButtonCallback] = None,
        on_click: Optional[Callable[[], None]] = None,
    ) -> None:
        for event in self.events():
            if isinstance(event, Rotate) and on_rotate:
                on_rotate(event)
            elif isinstance(event, Button):
                if on_button:
                    on_button(event)
                if on_click and not event.pressed:
                    on_click()

    def _read_available(
        self, timeout: Optional[float], first_only: bool
    ) -> Iterator[Event]:
        if self._fd is None:
            raise RuntimeError("PowerMate cerrado")
        self._flush_led()
        if self._pending_led is not None:
            remain = LED_WRITE_INTERVAL - (time.monotonic() - self._last_led_write)
            if remain < 0:
                remain = 0
            if timeout is None or remain < timeout:
                timeout = remain
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return
        try:
            data = os.read(self._fd, EVENT_SIZE * 32)
        except BlockingIOError:
            return
        if not data:
            raise OSError("PowerMate desconectado")
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            event = self._parse(data[offset : offset + EVENT_SIZE])
            if event is None:
                continue
            yield event
            if first_only:
                return

    @staticmethod
    def _parse(raw: bytes) -> Optional[Event]:
        sec, usec, ev_type, code, value = struct.unpack(EVENT_FORMAT, raw)
        timestamp = sec + usec / 1_000_000
        if ev_type == EV_REL and code == REL_DIAL and value != 0:
            return Rotate(delta=value, timestamp=timestamp)
        if ev_type == EV_KEY and code == BTN_0:
            return Button(pressed=bool(value), timestamp=timestamp)
        return None


def _demo(device: Optional[str], grab: bool) -> int:
    try:
        pm = PowerMate(path=device, grab=grab)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1

    brightness = 128
    pulse = False
    pulse_span = PULSE_SPEED_MAX - PULSE_SPEED_MIN
    pulse_level = int(
        round(100 * (PULSE_SPEED_NORMAL - PULSE_SPEED_MIN) / pulse_span)
    )

    def apply_led() -> None:
        if pulse:
            speed = pulse_level_to_speed(pulse_level)
            pm.led_pulse(pulse_speed=speed, brightness=0)
            print(f"LED pulso  {pulse_level:3d}%  (kernel {speed})")
        else:
            pm.led_solid(brightness)
            print(f"LED sólido brightness={brightness}")

    def on_rotate(event: Rotate) -> None:
        nonlocal brightness, pulse_level
        if pulse:
            pulse_level = max(0, min(100, pulse_level + event.delta * 2))
        else:
            brightness = max(0, min(255, brightness + event.delta * 3))
        apply_led()

    def on_click() -> None:
        nonlocal pulse
        pulse = not pulse
        apply_led()

    print(f"PowerMate en {pm.path}")
    print("SFTP auto: 2026-09-01 13:46")
    print(
        f"Pulso 0-100% = kernel {PULSE_SPEED_MIN}-{PULSE_SPEED_MAX} "
        "(100% no debe pasar de 275)."
    )
    print("Gira: brillo (o velocidad de pulso). Clic firme: cambia modo. Ctrl+C sale.")
    apply_led()
    try:
        pm.run(on_rotate=on_rotate, on_click=on_click)
    except KeyboardInterrupt:
        print("\nAdiós")
    finally:
        try:
            pm.led_off()
        except OSError:
            pass
        pm.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Interfaz Griffin PowerMate")
    parser.add_argument(
        "--device",
        help="Ruta evdev (por defecto el by-id del PowerMate)",
    )
    parser.add_argument(
        "--grab",
        action="store_true",
        help="Exclusivo: otros procesos no reciben los eventos",
    )
    args = parser.parse_args()
    return _demo(args.device, args.grab)


if __name__ == "__main__":
    raise SystemExit(main())
