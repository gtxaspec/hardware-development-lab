#!/usr/bin/env python3
# uart-listen-all.py [SECONDS] [OUTDIR]: capture every ser2net port at once,
# one file per port, then print which ports spoke and their U-Boot CPU/Model
# lines. Power the boards on while it runs to find which board is on which
# port. Host and ports come from UART_HOST, UART_BASE_PORT, UART_COUNT.
import os, re, selectors, socket, sys, tempfile, time
HOST = os.environ.get("UART_HOST", "localhost")
BASE = int(os.environ.get("UART_BASE_PORT", "3000"))
COUNT = int(os.environ.get("UART_COUNT", "24"))
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 40
out = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="uartscan-")
os.makedirs(out, exist_ok=True)
sel, data = selectors.DefaultSelector(), {}
for port in range(BASE, BASE + COUNT):
    try:
        s = socket.create_connection((HOST, port), timeout=3)
    except OSError as e:
        print(f"{port}: {e}")
        continue
    s.setblocking(False)
    sel.register(s, selectors.EVENT_READ, port)
    data[port] = b""
end = time.time() + dur
while time.time() < end and sel.get_map():
    for key, _ in sel.select(timeout=1):
        try:
            d = key.fileobj.recv(4096)
        except OSError:
            d = b""
        if not d:
            sel.unregister(key.fileobj)
            continue
        data[key.data] += d
for port, d in data.items():
    if not d:
        continue
    open(os.path.join(out, f"{port}.log"), "wb").write(d)
    t = re.sub(rb"\x1b\[[0-9;]*[a-zA-Z]", b"", d).decode(errors="replace")
    ids = re.findall(r"^(?:CPU|Model):.*$", t.replace("\r", ""), re.M)
    print(f"{port}: {len(d)} bytes  " + " | ".join(i.strip() for i in ids[:2]))
print(f"logs in {out}")
