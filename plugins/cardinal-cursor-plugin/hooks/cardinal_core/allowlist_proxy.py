#!/usr/bin/env python3
"""The network lock of an unattended Gemini CLI wake: a CONNECT-only HTTPS
proxy with an exact host allow-list (docs/specs/resume-and-wake.md §5.3,
proof C-9).

Gemini CLI's `restrictive-proxied` seatbelt profile denies every outbound
connection except localhost:8877, and Gemini starts the command in
GEMINI_SANDBOX_PROXY_COMMAND (through a shell, in its own process group)
before it enters the sandbox, pointing HTTPS_PROXY inside at
http://localhost:8877. cardinal-wake sets that command to this script, so
the sandboxed agent, its hooks and its MCP servers can reach only:

  - the hosts named on the command line (the Gemini API, the Google OAuth /
    Code Assist hosts when the user signs in with Google, the Cardinal hosts
    of the Gemini plugin's connection), exactly (no wildcard, no suffix);
  - on port 443, through CONNECT (a TLS tunnel the proxy never reads).

Anything else is answered 403 (a host or port not allowed), 405 (not
CONNECT) or 400 (malformed), and closed. A connection from a non-loopback
peer is closed unanswered (Gemini's profile requires the proxy to listen on
[::]:8877; only this machine may use it). Each decision is one line,
ALLOW or DENY, in the log (the wake's per-run log, 0600): the target and
the reason, never a byte of the tunnel. The process exits when the
process that started it is gone (its parent pid changes), or on SIGTERM
(Gemini signals the group when it exits).

The port is owned exclusively: never SO_REUSEADDR (with it, a wildcard
bind succeeds over another process already listening on 127.0.0.1:8877,
which would then receive the sandbox's traffic), and after the bind the
loopback addresses must be refused to a fresh socket (verify_exclusive);
otherwise the proxy exits 3 and starts nothing (fail closed).

Usage:
    python3 -I allowlist_proxy.py --log PATH [--port 8877] HOST [HOST ...]
        serve until the parent process is gone;
    python3 -I allowlist_proxy.py --log PATH [--port 8877] HOST [HOST ...] -- COMMAND ...
        cardinal-wake's Gemini launch: own the port first, then run COMMAND
        (sandbox-exec around gemini) as a child and exit with its status.

Standard library only; Python 3.9; importable (serve() / make_server()
take the port, the upstream connector and the log so tests bind an
ephemeral port and never reach the internet).
"""

from __future__ import annotations

import argparse
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time
from typing import Callable, Iterable, Optional

PORT = 8877
ALLOWED_PORT = 443
MAX_HEAD = 8192
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 600.0
HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")  # fullmatch
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def valid_host(host: str) -> bool:
    return isinstance(host, str) and bool(HOST_RE.fullmatch(host))


def parse_connect(head: bytes):
    """(method, host, port) of a request head, or None when malformed."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    method, target = parts[0], parts[1]
    if method != "CONNECT":
        return method, None, None
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit() or len(port) > 5:
        return None
    return method, host.lower(), int(port)


class _Logger:
    def __init__(self, path: Optional[str]):
        self.lock = threading.Lock()
        self.fd = None
        if path:
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    def __call__(self, verdict: str, target: str, why: str = "") -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} proxy {verdict} {target}" \
               + (f" ({why})" if why else "") + "\n"
        with self.lock:
            if self.fd is not None:
                try:
                    os.write(self.fd, line.encode("ascii", "replace"))
                except OSError:
                    pass
            else:
                sys.stderr.write(line)


def _relay(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    while True:
        ready, _, _ = select.select(socks, [], [], IDLE_TIMEOUT)
        if not ready:
            return
        for s in ready:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                (b if s is a else a).sendall(data)
            except OSError:
                return


def make_handler(allowed: Iterable[str], log: Callable[..., None],
                 upstream: Callable[[str, int], socket.socket]):
    allow = frozenset(h.lower() for h in allowed if valid_host(h.lower()))

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            conn = self.request
            peer = self.client_address[0] if self.client_address else ""
            if peer not in LOOPBACK:
                log("DENY", str(peer), "not a loopback peer")
                return
            conn.settimeout(HEAD_TIMEOUT)
            head = b""
            try:
                while b"\r\n\r\n" not in head and len(head) < MAX_HEAD:
                    chunk = conn.recv(MAX_HEAD - len(head))
                    if not chunk:
                        return
                    head += chunk
            except OSError:
                return
            if b"\r\n\r\n" not in head:
                log("DENY", "-", "malformed request")
                self._answer(400, "Bad Request")
                return
            got = parse_connect(head)
            if got is None:
                log("DENY", "-", "malformed request")
                self._answer(400, "Bad Request")
                return
            method, host, port = got
            if method != "CONNECT":
                log("DENY", method[:16], "only CONNECT")
                self._answer(405, "Method Not Allowed")
                return
            target = f"{host[:253]}:{port}"
            if port != ALLOWED_PORT:
                log("DENY", target, "port not allowed")
                self._answer(403, "Forbidden")
                return
            if host not in allow:
                log("DENY", target, "host not allowed")
                self._answer(403, "Forbidden")
                return
            try:
                up = upstream(host, port)
            except OSError as e:
                log("DENY", target, f"upstream unreachable: {type(e).__name__}")
                self._answer(502, "Bad Gateway")
                return
            log("ALLOW", target)
            try:
                conn.settimeout(None)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                extra = head.split(b"\r\n\r\n", 1)[1]
                if extra:
                    up.sendall(extra)
                _relay(conn, up)
            except OSError:
                pass
            finally:
                try:
                    up.close()
                except OSError:
                    pass

        def _answer(self, code: int, reason: str) -> None:
            try:
                self.request.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                                     .encode("ascii"))
            except OSError:
                pass

    return Handler


class NotExclusive(OSError):
    """The proxy's port is (also) held by another socket."""


class _DualStackServer(socketserver.ThreadingTCPServer):
    """[::]:port with IPv4 mapped in. Never SO_REUSEADDR: with it, a wildcard
    bind succeeds over another process already holding 127.0.0.1:port, which
    would then receive the sandbox's connections to localhost. After the
    bind, the loopback addresses must be refused to anyone else
    (verify_exclusive), else the server closes (fail closed)."""

    address_family = socket.AF_INET6
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (OSError, AttributeError):
            pass
        super().server_bind()


def verify_exclusive(port: int) -> None:
    """Raise NotExclusive unless binding 127.0.0.1:port, ::1:port and
    0.0.0.0:port is refused (EADDRINUSE) to a fresh socket: then this
    process's wildcard bind owns the loopback port on both stacks."""
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port)),
                         (socket.AF_INET, ("0.0.0.0", port))):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue   # no such stack on this machine: nothing can listen there either
        try:
            probe.bind(addr)
        except OSError:
            continue
        else:
            raise NotExclusive(f"{addr[0]}:{port} is not exclusively ours")
        finally:
            probe.close()


def default_upstream(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)


def make_server(hosts: Iterable[str], *, port: int = PORT, bind: str = "::", log_path: Optional[str] = None,
                upstream: Callable[[str, int], socket.socket] = default_upstream,
                log: Optional[Callable[..., None]] = None,
                exclusive: bool = True) -> socketserver.ThreadingTCPServer:
    """A bound (not yet serving) proxy; port 0 picks a free one
    (server.server_address[1]). The bind is exclusive (no SO_REUSEADDR)
    and, with exclusive, verified (verify_exclusive); OSError when either
    fails, and nothing is left listening."""
    handler = make_handler(hosts, log or _Logger(log_path), upstream)
    server = _DualStackServer((bind, port), handler)
    if exclusive:
        try:
            verify_exclusive(server.server_address[1])
        except OSError:
            server.server_close()
            raise
    return server


def _watch_parent(parent: int) -> None:
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(0)


def main(argv: Optional[list] = None, *, popen=None) -> int:
    """`allowlist_proxy.py [--log PATH] [--port N] HOST... [-- COMMAND...]`.
    Without COMMAND: serve until the parent is gone (Gemini's
    GEMINI_SANDBOX_PROXY_COMMAND). With COMMAND (cardinal-wake's Gemini
    launch): bind first, exclusively (exit 3 without running COMMAND when
    the port cannot be owned), then run COMMAND as this process's child,
    forward SIGTERM / SIGINT to it, and exit with its status when it ends."""
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    parser = argparse.ArgumentParser(prog="allowlist_proxy.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--log", default=None, help="append ALLOW / DENY lines here (0600)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("hosts", nargs="+", help="exact host names allowed on port 443")
    args = parser.parse_args(argv)
    bad = [h for h in args.hosts if not valid_host(h.lower())]
    if bad:
        sys.stderr.write(f"allowlist_proxy: not host names: {', '.join(bad)[:200]}\n")
        return 2
    try:
        server = make_server(args.hosts, port=args.port, log_path=args.log)
    except OSError as e:
        sys.stderr.write(f"allowlist_proxy: cannot own port {args.port}: {type(e).__name__}\n")
        return 3
    if not command:
        threading.Thread(target=_watch_parent, args=(os.getppid(),), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        return 0
    import signal
    import subprocess
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    try:
        child = (popen or subprocess.Popen)(command)
    except OSError as e:
        server.shutdown()
        server.server_close()
        sys.stderr.write(f"allowlist_proxy: cannot start the command: {type(e).__name__}\n")
        return 127

    def forward(signum, frame):  # noqa: ARG001
        try:
            child.send_signal(signum)
        except OSError:
            pass
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    rc = child.wait()
    server.shutdown()
    server.server_close()
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
