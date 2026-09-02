#!/usr/bin/env python3
"""HTTP + WebSocket para el PowerMate. Solo stdlib (adecuado a Pi 1).

  python3 ~/pi/server.py
  En el PC: http://192.168.0.27:8080  (wifi) o http://192.168.0.28:8080 (LAN)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from powermate import (
    PULSE_SPEED_NORMAL,
    PULSE_SPEED_MIN,
    PULSE_SPEED_MAX,
    Button,
    PowerMate,
    Rotate,
    pulse_level_to_speed,
)

ROOT = Path(__file__).resolve().parent
WEB = (ROOT / "web").resolve()
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json",
}


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list["WSClient"] = []

    def add(self, client: "WSClient") -> None:
        with self._lock:
            self._clients.append(client)

    def remove(self, client: "WSClient") -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        with self._lock:
            clients = list(self._clients)
        dead: list[WSClient] = []
        for client in clients:
            try:
                client.send_bytes(data)
            except OSError:
                dead.append(client)
        for client in dead:
            self.remove(client)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._clients)

    def has_app(self, name: str) -> bool:
        with self._lock:
            return any(client.app == name for client in self._clients)


class WSClient:
    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self._sock = handler.connection
        self._rfile = handler.rfile
        self._wlock = threading.Lock()
        self.app = ""

    def send_bytes(self, payload: bytes) -> None:
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", n))
        with self._wlock:
            self._sock.sendall(header + payload)

    def recv_text(self) -> Optional[str]:
        while True:
            hdr = self._rfile.read(2)
            if not hdr or len(hdr) < 2:
                return None
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            length = hdr[1] & 0x7F
            if length == 126:
                ext = self._rfile.read(2)
                if len(ext) < 2:
                    return None
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._rfile.read(8)
                if len(ext) < 8:
                    return None
                length = struct.unpack("!Q", ext)[0]
            mask = self._rfile.read(4) if masked else b""
            data = bytearray(self._rfile.read(length))
            if len(data) < length:
                return None
            if masked:
                for i in range(length):
                    data[i] ^= mask[i % 4]
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._pong(data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                return data.decode("utf-8")
            # binario u otros: ignorar
            continue

    def _pong(self, data: bytes) -> None:
        header = bytearray([0x8A, len(data)])
        with self._wlock:
            self._sock.sendall(header + data)


class App:
    def __init__(self) -> None:
        self.hub = Hub()
        self.pm: Optional[PowerMate] = None
        self.error: Optional[str] = None
        self.brightness = 128
        self.pulse = False
        span = PULSE_SPEED_MAX - PULSE_SPEED_MIN
        self.pulse_level = int(
            round(100 * (PULSE_SPEED_NORMAL - PULSE_SPEED_MIN) / span)
        )
        self._pm_lock = threading.Lock()

    def snapshot(self) -> dict:
        return {
            "type": "hello",
            "device": None if self.pm is None else self.pm.path,
            "error": self.error,
            "brightness": self.brightness,
            "pulse": self.pulse,
            "pulseLevel": self.pulse_level,
            "clients": self.hub.count,
        }

    def apply_led(self) -> None:
        if self.pm is None:
            return
        with self._pm_lock:
            if self.pulse:
                self.pm.led_pulse(
                    pulse_speed=pulse_level_to_speed(self.pulse_level),
                    brightness=0,
                )
            else:
                self.pm.led_solid(self.brightness)

    def handle_client_json(self, raw: str, client: Optional[WSClient] = None) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        cmd = msg.get("cmd")
        if cmd == "app" and client is not None:
            client.app = str(msg.get("name") or "")
            return
        if cmd == "led":
            self.pulse = False
            self.brightness = max(0, min(255, int(msg.get("brightness", self.brightness))))
            self.apply_led()
            self.hub.broadcast({"type": "state", **self._state()})
        elif cmd == "pulse":
            self.pulse = bool(msg.get("on", True))
            if "level" in msg:
                self.pulse_level = max(0, min(100, int(msg["level"])))
            self.apply_led()
            self.hub.broadcast({"type": "state", **self._state()})

    def _state(self) -> dict:
        return {
            "brightness": self.brightness,
            "pulse": self.pulse,
            "pulseLevel": self.pulse_level,
        }

    def on_event(self, event: object) -> None:
        led_live = self.hub.has_app("monitor")
        if isinstance(event, Rotate):
            if led_live:
                if self.pulse:
                    self.pulse_level = max(
                        0, min(100, self.pulse_level + event.delta * 2)
                    )
                else:
                    self.brightness = max(
                        0, min(255, self.brightness + event.delta * 3)
                    )
                self.apply_led()
            self.hub.broadcast(
                {
                    "delta": event.delta,
                    "t": event.timestamp,
                    **self._state(),
                    "type": "rotate",
                }
            )
        elif isinstance(event, Button):
            if led_live and not event.pressed:
                self.pulse = not self.pulse
                self.apply_led()
            self.hub.broadcast(
                {
                    "pressed": event.pressed,
                    "t": event.timestamp,
                    **self._state(),
                    "type": "button",
                }
            )


APP = App()


def make_handler() -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_GET(self) -> None:
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self._websocket()
                return
            parsed = urlparse(self.path)
            rel = parsed.path.lstrip("/")
            fs_path = (WEB / rel).resolve() if rel else WEB
            try:
                fs_path.relative_to(WEB)
            except ValueError:
                self.send_error(403)
                return
            if fs_path.is_dir():
                fs_path = (fs_path / "index.html").resolve()
            if not fs_path.is_file():
                sys.stderr.write("404 %s -> %s\n" % (parsed.path, fs_path))
                self.send_error(404)
                return
            data = fs_path.read_bytes()
            ctype = MIME.get(fs_path.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _websocket(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/ws":
                self.send_error(404)
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_error(400)
                return
            digest = hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()
            accept = base64.b64encode(digest).decode("ascii")
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            client = WSClient(self)
            APP.hub.add(client)
            try:
                client.send_bytes(
                    json.dumps(APP.snapshot(), separators=(",", ":")).encode("utf-8")
                )
                while True:
                    text = client.recv_text()
                    if text is None:
                        break
                    APP.handle_client_json(text, client)
            except OSError:
                pass
            finally:
                APP.hub.remove(client)

    return Handler


def reader_loop() -> None:
    assert APP.pm is not None
    try:
        for event in APP.pm.events():
            APP.on_event(event)
    except (OSError, RuntimeError) as exc:
        APP.error = str(exc)
        APP.hub.broadcast({"type": "error", "message": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor web + WebSocket PowerMate")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    try:
        APP.pm = PowerMate(path=args.device)
        APP.apply_led()
        threading.Thread(target=reader_loop, name="powermate", daemon=True).start()
        print("PowerMate:", APP.pm.path)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        APP.error = str(exc)
        print("PowerMate no disponible:", exc, file=sys.stderr)
        print("El servidor web arranca igual; reconecta el USB y reinicia.", file=sys.stderr)

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler())
    httpd.allow_reuse_address = True
    print("Web root:", WEB)
    if WEB.is_dir():
        for item in sorted(WEB.rglob("*")):
            if item.is_file():
                print("  /%s" % item.relative_to(WEB).as_posix())
    else:
        print("  (no existe la carpeta web)")
    print("Web:       http://%s:%s/" % (args.host, args.port))
    print("WebSocket: ws://%s:%s/ws" % (args.host, args.port))
    print("Desde el PC prueba http://192.168.0.27:%s/ o http://192.168.0.28:%s/" % (args.port, args.port))
    print("Ctrl+C para salir.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nAdiós")
    finally:
        httpd.server_close()
        if APP.pm is not None:
            try:
                APP.pm.led_off()
            except OSError:
                pass
            APP.pm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
