"""Minimal Logitech HID++ 2.0 client for Windows (stdlib only, ctypes)."""
import ctypes
import ctypes.wintypes as wt
import threading
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.CreateEventW.restype = wt.HANDLE
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p]
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p]
k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.ResetEvent.argtypes = [wt.HANDLE]
k32.CloseHandle.argtypes = [wt.HANDLE]
cfg = ctypes.WinDLL("cfgmgr32")
ole32 = ctypes.WinDLL("ole32")

INVALID = wt.HANDLE(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


def hid_paths():
    g = GUID()
    ole32.CLSIDFromString("{4d1e55b2-f16f-11cf-88cb-001111000030}", ctypes.byref(g))
    size = wt.ULONG()
    if cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(g), None, 0) != 0:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cfg.CM_Get_Device_Interface_ListW(ctypes.byref(g), None, buf, size, 0) != 0:
        return []
    return [p for p in ctypes.wstring_at(ctypes.addressof(buf), size.value).split("\0") if p]


class HidHandle:
    def __init__(self, path):
        self.h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)
        if not self.h or self.h == INVALID:
            raise OSError(ctypes.get_last_error(), "open failed")
        # one event per direction: a reader thread and a writer may run at the same time
        self.ev = {"r": k32.CreateEventW(None, True, False, None),
                   "w": k32.CreateEventW(None, True, False, None)}

    def close(self):
        k32.CloseHandle(self.h)
        for e in self.ev.values():
            k32.CloseHandle(e)

    def _io(self, fn, buf, size, timeout):
        ev = self.ev["r" if fn is k32.ReadFile else "w"]
        ov = OVERLAPPED(hEvent=ev)
        k32.ResetEvent(ev)
        n = wt.DWORD()
        ok = fn(self.h, buf, size, None, ctypes.byref(ov))
        if not ok and ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
            raise OSError(ctypes.get_last_error(), "io failed")
        if k32.WaitForSingleObject(ev, timeout) != 0:
            k32.CancelIoEx(self.h, ctypes.byref(ov))
            # always wait for completion so the kernel never writes into a freed buffer
            k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), True)
            return None
        if not k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), True):
            raise OSError(ctypes.get_last_error(), "io failed")
        return n.value

    def write(self, data):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return self._io(k32.WriteFile, buf, len(data), 1000)

    def read(self, size, timeout):
        buf = ctypes.create_string_buffer(size)
        n = self._io(k32.ReadFile, buf, size, timeout)
        return None if n is None else buf.raw[:n]


class Offline(Exception):
    pass


F_NAME, F_BATT, F_UBATT, F_DPI, F_RATE, F_ONBOARD = 0x0005, 0x1000, 0x1004, 0x2201, 0x8060, 0x8100


class Transport:
    """Long (20B) + short (7B) HID++ collections, each drained by a reader thread into one queue."""

    def __init__(self, long_path):
        import queue
        self.q = queue.Queue()
        self.alive = True
        self.long = HidHandle(long_path)
        self.short = None
        lp = long_path.lower()
        i = lp.find("&col02")
        if i >= 0:
            # sibling short-report collection (its instance suffix differs, so look it up)
            want = lp[:i] + "&col01#"
            for p in hid_paths():
                if p.lower().startswith(want):
                    try:
                        self.short = HidHandle(p)
                    except OSError:
                        pass
                    break
        self.threads = [threading.Thread(target=self._reader, args=(h, n), daemon=True)
                        for h, n in ((self.long, 20), (self.short, 7)) if h]
        for t in self.threads:
            t.start()

    def _reader(self, h, size):
        while self.alive:
            try:
                r = h.read(size, 250)
            except OSError:
                break
            if r:
                self.q.put(r)

    def write(self, data):
        return self.long.write(data)

    def close(self):
        self.alive = False
        for t in self.threads:
            t.join(1)
        self.long.close()
        if self.short:
            self.short.close()


class Mouse:
    """Finds the first Logitech HID++ 2.0 mouse behind any receiver (or wired)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.dev = None
        self.idx = None
        self.feats = {}
        self.name = None
        self.sw = 0x0B

    # ---- low level ----
    def _req(self, fi, func, params=b"", timeout=1500):
        import queue
        while not self.dev.q.empty():
            self.dev.q.get_nowait()
        msg = bytes([0x11, self.idx, fi, (func << 4) | self.sw]) + bytes(params)
        self.dev.write(msg.ljust(20, b"\0"))
        end = time.monotonic() + timeout / 1000
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise Offline("timeout")
            try:
                r = self.dev.q.get(timeout=left)
            except queue.Empty:
                raise Offline("timeout")
            if len(r) < 6 or r[1] != self.idx:
                continue
            if r[0] == 0x11 and r[2] == fi and (r[3] & 0x0F) == self.sw:
                return r[4:]
            if r[0] == 0x11 and r[2] == 0xFF and r[3] == fi:
                raise RuntimeError("HID++ error 0x%02x" % r[5])
            if r[0] == 0x10 and r[2] == 0x8F and r[3] == fi:
                # receiver answers for an absent / sleeping device (e.g. 0x09 = resource error)
                raise Offline("receiver error 0x%02x" % r[5])

    def _probe(self, path, idx):
        self.idx = idx
        try:
            try:
                r = self._req(0, 1, b"\0\0Z")
            except Offline as e:
                if "receiver error" in str(e):
                    raise
                r = self._req(0, 1, b"\0\0Z")  # mouse may be waking up
            if r[0] < 2:
                raise Offline("not HID++ 2.0")
            self.feats = {}
            for fid in (F_NAME, F_BATT, F_UBATT, F_DPI, F_RATE, F_ONBOARD):
                fi = self._req(0, 0, bytes([fid >> 8, fid & 0xFF]))[0]
                if fi:
                    self.feats[fid] = fi
            if F_DPI not in self.feats:
                raise Offline("not a mouse")
            self.name = self._read_name()
            return True
        except Exception:
            return False

    def _read_name(self):
        fi = self.feats.get(F_NAME)
        if not fi:
            return "Logitech mouse"
        n = self._req(fi, 0)[0]
        name = b""
        while len(name) < n:
            name += self._req(fi, 1, bytes([len(name)]))[: n - len(name)]
        return name.decode(errors="replace")

    def connect(self):
        if self.dev and self.idx is not None and self.feats:
            return True
        for p in hid_paths():
            lp = p.lower()
            if "vid_046d" not in lp:
                continue
            # receivers: long-report collection lives on MI_02&COL02; wired mice expose it on MI_02 too
            if "mi_02&col02" not in lp and not ("mi_02" in lp and "col" not in lp):
                continue
            try:
                self.dev = Transport(p)
            except OSError:
                continue
            for idx in (1, 2, 3, 4, 5, 6, 0xFF):
                if self._probe(p, idx):
                    return True
            self.dev.close()
            self.dev = None
        self.feats = {}
        return False

    def _drop(self):
        if self.dev:
            self.dev.close()
        self.dev = None
        self.feats = {}

    def call(self, fn):
        with self.lock:
            for attempt in (0, 1):
                if not self.connect():
                    raise Offline("no device")
                try:
                    return fn()
                except (Offline, OSError):
                    self._drop()
                    if attempt:
                        raise Offline("device not responding")

    # ---- features ----
    def battery(self):
        def f():
            if F_UBATT in self.feats:
                r = self._req(self.feats[F_UBATT], 1)
                return {"percent": r[0], "charging": r[2] in (1, 2), "full": r[2] == 3}
            r = self._req(self.feats[F_BATT], 0)
            return {"percent": r[0], "charging": r[2] in (1, 2), "full": r[2] == 3}
        return self.call(f)

    def get_dpi(self):
        return self.call(lambda: int.from_bytes(self._req(self.feats[F_DPI], 2, b"\0")[1:3], "big"))

    def set_dpi(self, dpi):
        self.call(lambda: self._req(self.feats[F_DPI], 3, bytes([0, dpi >> 8, dpi & 0xFF])))

    def rates(self):
        """Supported report rates in Hz (descending)."""
        def f():
            mask = self._req(self.feats[F_RATE], 0)[0]
            return sorted({1000 // (i + 1) for i in range(8) if mask & (1 << i)}, reverse=True)
        return self.call(f)

    def get_rate(self):
        return self.call(lambda: 1000 // max(1, self._req(self.feats[F_RATE], 1)[0]))

    def set_rate(self, hz):
        self.call(lambda: self._req(self.feats[F_RATE], 2, bytes([max(1, 1000 // hz)])))

    def get_mode(self):
        """1 = onboard memory, 2 = host (software) mode, None = unsupported."""
        if F_ONBOARD not in self.feats and not self.connect():
            return None
        if F_ONBOARD not in self.feats:
            return None
        return self.call(lambda: self._req(self.feats[F_ONBOARD], 2)[0])

    def set_mode(self, mode):
        if F_ONBOARD in self.feats or self.connect():
            if F_ONBOARD in self.feats:
                self.call(lambda: self._req(self.feats[F_ONBOARD], 1, bytes([mode])))


if __name__ == "__main__":
    m = Mouse()
    print(m.connect(), m.name, m.feats)
    print(m.battery(), m.get_dpi(), m.get_rate(), m.rates(), m.get_mode())
