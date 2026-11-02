#!/usr/bin/env python3
"""NESP32 bridge for Mesen's Lua script (nesp32.lua).

Mesen runs the console. nesp32.lua watches controller port 2: every change
of OUT0 ($4016 bit 0) it sends here with its CPU cycle, and what the console
reads from D0 ($4017 bit 0) it works out from the bytes this bridge says are
on the line, and when.

This side is the NESP32 itself: the board's own firmware (its link and
services, built for a computer as nesp32's build/libnesp_host) behind
nesp32's own line model (tests/wire.py), with real Internet fetches, real
time, and a Wi-Fi network that is always there.

    nesp32_bridge.py [--host 0.0.0.0] [--port 7878] [--nesp32 /path/to/nesp32]

Protocol, one line each way, ASCII:
  Mesen -> bridge   S <cycle> <n> <c1> <l1> ... <cn> <ln>
                    (the console's time, and OUT0's changes since the last S)
                    B <button>     the NESP32's own button pressed (1 = A, 2 = B)
  bridge -> Mesen   R <busy> <runs> [<start> <period> <hexbytes>]...
                    (bytes newly on the line: each run back to back from
                    <start>, <period> cycles a bit, 11 bits a byte. busy 1:
                    a byte from the console is partway in, so sync again
                    on the next read of $4017.)
"""

import argparse
import os
import queue
import socket
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def log(text):
    print(time.strftime("%H:%M:%S ") + text, flush=True)


def load_nesp32(root):
    for sub in ("host", "build", "tests"):
        sys.path.insert(0, os.path.join(root, sub))
    global nesp, proto, wire
    import nesp_proto as proto
    import nesp
    import wire
    # The board's UART holds 2048 bytes; the tests' model holds 256, which
    # is less than a frame of a radio station's sound.
    wire.TX_BUFFER = 2048


class Console:
    """What wire.Port asks of a console: the time, in CPU cycles."""

    def __init__(self):
        self.now = 0


class Fetcher:
    """A fetch on a thread of its own. What it gets is queued, and handed to
    the firmware's sink from the main thread between syncs."""

    def __init__(self, fetch_id, url, agent, icy):
        self.id = fetch_id
        self.items = queue.Queue()
        self.cancelled = False
        self.thread = threading.Thread(target=self.run, args=(url, agent, icy), daemon=True)
        self.thread.start()

    def run(self, url, agent, icy):
        headers = {"User-Agent": agent or "nesp32"}
        if icy:
            headers["Icy-MetaData"] = "1"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=20) as reply:
                if icy:
                    self.items.put(("icy", int(reply.headers.get("icy-metaint") or 0)))
                while not self.cancelled:
                    chunk = reply.read1(8192) if hasattr(reply, "read1") else reply.read(8192)
                    if not chunk:
                        break
                    self.items.put(("body", chunk))
            self.items.put(("done", True, b""))
        except urllib.error.HTTPError as error:
            self.items.put(("done", False, ("HTTP %d" % error.code).encode()))
        except Exception:
            self.items.put(("done", False, b"NO CONNECTION"))


class Board:
    """One NESP32, for one Mesen."""

    def __init__(self):
        self.console = Console()
        self.port = wire.Port(self.console, turnaround=wire.ms(0.1))
        self.stick = nesp.Stick(self.port)
        self.stick.set_wifi(proto.NESP_WIFI_JOINED, rssi=-45, address=(192, 168, 0, 32))
        self.stick.set_saved(b"Mesen")
        self.stick.set_time(int(time.time()))
        self.sent = 0               # how many of the wire's bytes Mesen has been told of
        self.scans = 0
        self.joins = 0
        self.fetches = 0
        self.fetch_stops = 0
        self.fetch = None
        self.counted = (0, 0, 0)
        self.last_end = None        # where the last byte sent to Mesen ends, for gaps
        self.instrument()

    def instrument(self):
        """Log the radio's requests, the line's changes of rate (a sound run
        starting or ending), and gaps in a run's samples."""
        port = self.port
        read, line_rate = port.read, port.line_rate
        names = {proto.NESP_TUNER_PLAY: "PLAY", proto.NESP_TUNER_STOP: "STOP",
                 proto.NESP_TUNER_SOUND: "SOUND", proto.NESP_TUNER_URL: "URL"}

        def logged_read():
            data = read()
            if data:
                for i in range(len(data) - 2):
                    if (data[i] == proto.NESP_REQUEST_MARKER and data[i + 1] == proto.NESP_SVC_TUNER
                            and data[i + 2] in names):
                        log("console: TUNER %s (cycle %d)" % (names[data[i + 2]], self.console.now))
            return data

        def logged_rate(period):
            log("line rate %d cycles a bit (%s) at cycle %d" % (
                period, "link" if period == proto.NESP_PERIOD else "sound run", self.console.now))
            line_rate(period)

        port.read, port.line_rate = logged_read, logged_rate

    def counters(self):
        stick = self.stick
        # Noise rises every frame on its own (the strobe for controller 1
        # looks like a start bit to the UART, on a real console too), so
        # only repeats and drops are worth a line.
        now = (stick.repeats, stick.dropped, stick.noise)
        if now[:2] != self.counted[:2]:
            log("link: requests %d, repeats %d, dropped %d, noise %d (console cycle %d)"
                % ((stick.requests,) + now + (self.console.now,)))
            self.counted = now

    def sync(self, cycle, changes):
        console, port, stick = self.console, self.port, self.stick
        console.now = cycle
        for at, level in changes:
            port.tap.write(at, level)
        stick.set_time(int(time.time()))
        self.network()
        self.deliver()
        stick.poll()
        self.network()
        self.counters()
        return self.report()

    def network(self):
        """What the hardware does for the services: scans, joins, fetches."""
        stick = self.stick
        if stick.scans != self.scans:
            self.scans = stick.scans
            stick.scan_set([(b"Mesen", -45, proto.NESP_AUTH_OPEN, 6)])
            stick.scan_finish()
        if stick.joins != self.joins:
            self.joins = stick.joins
            stick.set_wifi(proto.NESP_WIFI_JOINED, rssi=-45, address=(192, 168, 0, 32))
        if stick.fetch_stops != self.fetch_stops:
            self.fetch_stops = stick.fetch_stops
            self.cancel()
        if stick.fetches != self.fetches:
            self.fetches = stick.fetches
            self.cancel()
            log("fetch %d: %s" % (stick.fetch_id, stick.fetch_url))
            self.fetch = Fetcher(stick.fetch_id, stick.fetch_url, stick.fetch_agent,
                                 stick.fetch_wants_icy)

    def cancel(self):
        if self.fetch is not None:
            self.fetch.cancelled = True
            self.fetch = None

    def deliver(self):
        fetch = self.fetch
        if fetch is None:
            return
        while True:
            try:
                item = fetch.items.get_nowait()
            except queue.Empty:
                return
            if item[0] == "icy":
                self.stick.fetch_icy(item[1], fetch=fetch.id)
            elif item[0] == "body":
                if not self.stick.fetch_body(item[1], fetch=fetch.id):
                    fetch.cancelled = True
                    self.fetch = None
                    self.stick.fetch_done(True, b"", fetch=fetch.id)
                    return
            else:
                self.fetch = None
                log("fetch %d done: %s %s" % (fetch.id, "ok" if item[1] else "FAILED", item[2].decode()))
                self.stick.fetch_done(item[1], item[2], fetch=fetch.id)
                return

    def report(self):
        """The reply line: bytes on the wire that Mesen hasn't been told of."""
        w = self.wire_trim()
        runs = []
        index = self.sent
        while index < len(w.starts):
            start, bit = w.starts[index], w.bits[index]
            data = bytearray([w.bytes[index]])
            index += 1
            while (index < len(w.starts) and w.bits[index] == bit
                   and abs(w.starts[index] - (start + bit * wire.NESP_TO_NES_BITS * len(data))) < 0.01):
                data.append(w.bytes[index])
                index += 1
            if bit != proto.NESP_PERIOD and self.last_end is not None and start - self.last_end > bit * 22:
                log("GAP in sound: %.1f ms with nothing on the line (cycle %d)" % (
                    (start - self.last_end) * 1000 / wire.NESP_CPU_HZ, start))
            self.last_end = start + bit * wire.NESP_TO_NES_BITS * len(data)
            runs.append("%d %d %s" % (round(start), round(bit), data.hex()))
        self.sent = index
        return "R %d %d %s\n" % (1 if self.busy() else 0, len(runs), " ".join(runs))

    def busy(self):
        """A byte from the console is partway in: its start bit has come,
        and not its stop bit."""
        tap = self.port.tap
        return any(at > self.port.search_from and level == wire.SPACE
                   for at, level in zip(tap.times[-4:], tap.levels[-4:]))

    def wire_trim(self):
        """Forget bytes long gone from the line, so a radio station that
        plays for hours does not fill the memory."""
        w = self.port.wire
        old = self.console.now - wire.NESP_CPU_HZ   # a second ago
        cut = 0
        while cut < self.sent and cut < len(w.starts) - 1 and w.starts[cut + 1] < old:
            cut += 1
        if cut > 4096:
            del w.starts[:cut], w.bits[:cut], w.bytes[:cut]
            self.sent -= cut
        tap = self.port.tap
        if len(tap.times) > 8192:
            keep = max(0, len(tap.times) - 64)
            del tap.times[1:keep], tap.levels[1:keep]
        return w

    def button(self, which):
        code = proto.NESP_EVENT_BUTTON_A if which == 1 else proto.NESP_EVENT_BUTTON_B
        self.stick.event(code)


def serve(conn, address):
    log("Mesen connected from %s" % (address,))
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    board = Board()
    last = 0
    buffered = conn.makefile("rb")
    try:
        for raw in buffered:
            words = raw.split()
            if not words:
                continue
            if words[0] == b"S":
                cycle = int(words[1])
                if cycle < last:
                    log("console reset: a new board")
                    board.cancel()
                    board = Board()
                last = cycle
                count = int(words[2])
                changes = [(int(words[3 + 2 * i]), int(words[4 + 2 * i])) for i in range(count)]
                conn.sendall(board.sync(cycle, changes).encode())
            elif words[0] == b"B":
                board.button(int(words[1]))
    except (ConnectionError, OSError):
        pass
    finally:
        board.cancel()
        conn.close()
        log("Mesen disconnected")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7878)
    parser.add_argument("--nesp32", default=os.path.join(HERE, "..", "nesp32"),
                        help="a checkout of github.com/Armitage64/nesp32 with `make host` done")
    args = parser.parse_args()
    load_nesp32(os.path.abspath(args.nesp32))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(4)
    print("NESP32 bridge listening on %s:%d" % (args.host, args.port), flush=True)
    while True:
        conn, address = server.accept()
        threading.Thread(target=serve, args=(conn, address), daemon=True).start()


if __name__ == "__main__":
    main()
