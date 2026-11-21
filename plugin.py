"""Stream Deck plugin: Logitech mouse battery / DPI / report rate / onboard mode without G HUB.

Started by launch.js with Stream Deck's arguments: -port P -pluginUUID U -registerEvent E -info J
"""
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hidpp  # noqa: E402

PREFIX = "com.local.logimouse."
POLL_SEC = 20
STATE_FILE = os.path.join(os.environ.get("APPDATA", "."), "logimouse-streamdeck.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.log")


def log(*a):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    except OSError:
        pass


# ---------------- minimal WebSocket client ----------------
class WS:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            c = self.s.recv(1)
            if not c:
                raise ConnectionError("handshake failed")
            hdr += c
        if b" 101 " not in hdr.split(b"\r\n")[0]:
            raise ConnectionError(hdr.decode(errors="replace"))
        self.wlock = threading.Lock()

    def _send(self, op, data):
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.wlock:
            self.s.sendall(head + mask + body)

    def send(self, obj):
        self._send(1, json.dumps(obj).encode())

    def _exact(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b

    def recv(self):
        msg = b""
        while True:
            b0, b1 = self._exact(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            mask = self._exact(4) if b1 & 0x80 else None
            data = self._exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise ConnectionError("closed by server")
            if op == 9:
                self._send(10, data)
                continue
            if op in (0, 1, 2):
                msg += data
                if b0 & 0x80:
                    return json.loads(msg.decode())


# ---------------- key rendering ----------------
def svg_key(big, small="", color="#ffffff", fill=None, sub_color="#9aa0a6"):
    bar = ""
    if fill is not None:
        w = int(96 * max(0, min(100, fill)) / 100)
        bar = (f'<rect x="20" y="96" width="100" height="30" rx="5" fill="none" stroke="#5f6368" stroke-width="4"/>'
               f'<rect x="120" y="104" width="6" height="14" rx="2" fill="#5f6368"/>'
               f'<rect x="22" y="98" width="{w}" height="26" rx="3" fill="{color}"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
           f'<rect width="144" height="144" fill="#111316"/>'
           f'<text x="72" y="34" font-family="Segoe UI,Arial,sans-serif" font-size="20" fill="{sub_color}" text-anchor="middle">{small}</text>'
           f'<text x="72" y="{84 if fill is not None else 96}" font-family="Segoe UI,Arial,sans-serif" font-size="{44 if len(big) <= 4 else 34}" '
           f'font-weight="bold" fill="{color}" text-anchor="middle">{big}</text>{bar}</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def batt_color(p):
    return "#34a853" if p > 50 else "#fbbc04" if p > 20 else "#ea4335"


# ---------------- plugin ----------------
class Plugin:
    def __init__(self, ws):
        self.ws = ws
        self.mouse = hidpp.Mouse()
        self.ctx = {}  # context -> (action, settings)
        self.lock = threading.Lock()
        self.status = {}
        self.state = self._load()

    def _load(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"managed": False}

    def _save(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
        except OSError:
            pass

    # -- device sync --
    def refresh(self):
        st = {}
        try:
            st["battery"] = self.mouse.battery()
            st["dpi"] = self.mouse.get_dpi()
            st["rate"] = self.mouse.get_rate()
            st["mode"] = self.mouse.get_mode()
            st["name"] = self.mouse.name
            if self.state.get("managed"):
                self._enforce(st)
        except hidpp.Offline:
            st = {"offline": True}
        except Exception:
            log(traceback.format_exc())
            st = {"offline": True}
        self.status = st
        self.render_all()

    def _enforce(self, st):
        """Re-apply wanted settings (e.g. after the mouse wakes up and falls back to onboard memory)."""
        want_dpi, want_rate = self.state.get("dpi"), self.state.get("rate")
        if st.get("mode") not in (None, 2):
            self.mouse.set_mode(2)
            st["mode"] = 2
        if want_dpi and st["dpi"] != want_dpi:
            self.mouse.set_dpi(want_dpi)
            st["dpi"] = want_dpi
        if want_rate and st["rate"] != want_rate:
            self.mouse.set_rate(want_rate)
            st["rate"] = want_rate

    # -- rendering --
    def render(self, ctx):
        action, settings = self.ctx[ctx]
        s = self.status
        kind = action[len(PREFIX):]
        if s.get("offline"):
            img = svg_key("OFF", {"battery": "BATTERY", "dpi": "DPI", "rate": "Hz", "mode": "MODE"}.get(kind, ""),
                          "#5f6368")
        elif not s:
            img = svg_key("…", "")
        elif kind == "battery":
            b = s["battery"]
            p = b["percent"]
            label = "+ CHARGING" if b["charging"] else "FULL" if b["full"] else "BATTERY"
            img = svg_key(f"{p}%", label, batt_color(p), fill=p)
        elif kind == "dpi":
            img = svg_key(str(s["dpi"]), "DPI", "#8ab4f8")
        elif kind == "rate":
            img = svg_key(str(s["rate"]), "Hz", "#c58af9")
        elif kind == "mode":
            m = s.get("mode")
            img = (svg_key("HOST", "MODE", "#fbbc04") if m == 2 else
                   svg_key("BOARD", "MODE", "#34a853") if m == 1 else svg_key("N/A", "MODE", "#5f6368"))
        else:
            return
        self.ws.send({"event": "setImage", "context": ctx, "payload": {"image": img, "target": 0}})
        self.ws.send({"event": "setTitle", "context": ctx, "payload": {"title": "", "target": 0}})

    def render_all(self):
        for c in list(self.ctx):
            self.render(c)

    # -- key actions --
    @staticmethod
    def dpi_list(settings):
        raw = str(settings.get("dpiList") or "400,800,1600,3200")
        vals = []
        for x in raw.replace("、", ",").replace(" ", ",").split(","):
            if x.strip().isdigit():
                v = int(x)
                if 50 <= v <= 32000:
                    vals.append(v)
        return vals or [400, 800, 1600, 3200]

    def press(self, ctx):
        action, settings = self.ctx[ctx]
        kind = action[len(PREFIX):]
        try:
            if kind == "dpi":
                vals = self.dpi_list(settings)
                cur = self.mouse.get_dpi()
                nxt = next((v for v in vals if v > cur), vals[0])
                self._take_control()
                self.mouse.set_dpi(nxt)
                self.state["dpi"] = nxt
            elif kind == "rate":
                rates = sorted(self.mouse.rates())
                cur = self.mouse.get_rate()
                nxt = next((r for r in rates if r > cur), rates[0])
                self._take_control()
                self.mouse.set_rate(nxt)
                self.state["rate"] = nxt
            elif kind == "mode":
                if self.mouse.get_mode() == 2:
                    self.mouse.set_mode(1)
                    self.state["managed"] = False
                else:
                    self._take_control()
            self._save()
            self.refresh()
            if kind != "battery":
                self.ws.send({"event": "showOk", "context": ctx})
        except hidpp.Offline:
            self.ws.send({"event": "showAlert", "context": ctx})
            self.refresh()
        except Exception:
            log(traceback.format_exc())
            self.ws.send({"event": "showAlert", "context": ctx})

    def _take_control(self):
        """Switch to host mode; remember current values so they are kept across sleep/wake."""
        if self.mouse.get_mode() != 2:
            dpi, rate = self.mouse.get_dpi(), self.mouse.get_rate()
            self.mouse.set_mode(2)
            self.mouse.set_dpi(dpi)
            self.mouse.set_rate(rate)
            self.state.setdefault("dpi", dpi)
            self.state.setdefault("rate", rate)
        self.state.setdefault("dpi", self.mouse.get_dpi())
        self.state.setdefault("rate", self.mouse.get_rate())
        self.state["managed"] = True

    # -- event loop --
    def handle(self, ev):
        e, ctx = ev.get("event"), ev.get("context")
        with self.lock:
            if e == "willAppear":
                self.ctx[ctx] = (ev["action"], ev.get("payload", {}).get("settings", {}))
                if self.status:
                    self.render(ctx)
                else:
                    threading.Thread(target=self.locked_refresh, daemon=True).start()
            elif e == "willDisappear":
                self.ctx.pop(ctx, None)
            elif e == "didReceiveSettings" and ctx in self.ctx:
                self.ctx[ctx] = (ev["action"], ev.get("payload", {}).get("settings", {}))
            elif e == "keyDown" and ctx in self.ctx:
                self.press(ctx)
            elif e == "systemDidWakeUp" or e == "deviceDidConnect":
                threading.Thread(target=self.locked_refresh, daemon=True).start()

    def locked_refresh(self):
        with self.lock:
            self.refresh()

    def poller(self):
        while True:
            # asleep/off: check often so the keys come back quickly when the mouse wakes up
            time.sleep(5 if self.status.get("offline") else POLL_SEC)
            if self.ctx:
                self.locked_refresh()


def main():
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    ws = WS(int(args["-port"]))
    ws.send({"event": args["-registerEvent"], "uuid": args["-pluginUUID"]})
    p = Plugin(ws)
    threading.Thread(target=p.poller, daemon=True).start()
    log("started")
    while True:
        p.handle(ws.recv())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        raise
