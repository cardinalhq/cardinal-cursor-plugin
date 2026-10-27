"""Trust for a Cardinal host whose TLS certificate the machine does not trust.

Most Cardinal hosts carry a certificate the system already trusts and never
touch this module. A self-hosted Cardinal behind a self-signed (or private-CA)
certificate does: `cardinal-connect` probes the host first (`probe`), shows
the certificate and asks, and on consent pins exactly that certificate for
that hostname under ~/.cardinal/tls (`save_pin`). From then on every urllib
request in a process that imported cardinal_core verifies a pinned host
against only its pinned certificate (`PinnedHTTPSHandler`, installed
process-wide by `install`); every other host keeps default verification.
Nothing here ever turns verification off.

See docs/specs/tls-trust.md.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

PIN_DIR_PARTS = (".cardinal", "tls")
BUNDLE_NAME = "ca-bundle.pem"
PROBE_TIMEOUT_SECS = 10.0
# Exit status cardinal-connect uses when the certificate needs the user's
# decision and there is no terminal to ask on (the agent re-runs with
# --trust-cert). Distinct from 1 (any other failure) and 2 (already connected).
EXIT_UNTRUSTED_CERT = 3

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_HOST_FILE_RE = re.compile(r"[^A-Za-z0-9.-]")


class CertificateError(RuntimeError):
    """A user-presentable TLS trust failure (message is the whole story)."""


# ---------------------------------------------------------------------------
# Pin store
# ---------------------------------------------------------------------------

def _home(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    return Path(os.environ.get("HOME") or str(Path.home()))


def pin_dir(home: Optional[Path] = None) -> Path:
    """~/.cardinal/tls — one `<hostname>.pem` per pinned host plus the bundle."""
    return _home(home).joinpath(*PIN_DIR_PARTS)


def bundle_path(home: Optional[Path] = None) -> Path:
    """Every pinned certificate in one PEM file, for runtimes that take a CA
    bundle (Node's NODE_EXTRA_CA_CERTS). Rebuilt by save_pin / forget_pin."""
    return pin_dir(home) / BUNDLE_NAME


def normalize_host(hostname: str) -> str:
    return (hostname or "").strip().strip("[]").lower()


def pin_path(hostname: str, home: Optional[Path] = None) -> Path:
    name = _HOST_FILE_RE.sub("_", normalize_host(hostname)) or "_"
    return pin_dir(home) / f"{name}.pem"


def pinned_hosts(home: Optional[Path] = None) -> Dict[str, Path]:
    """hostname -> pem path for every pinned host (the bundle is not a pin)."""
    out: Dict[str, Path] = {}
    try:
        entries = sorted(pin_dir(home).iterdir())
    except OSError:
        return out
    for path in entries:
        if path.suffix != ".pem" or path.name == BUNDLE_NAME or not path.is_file():
            continue
        out[path.stem.lower()] = path
    return out


def pinned_pem(hostname: str, home: Optional[Path] = None) -> Optional[str]:
    path = pin_path(hostname, home)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


_PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


def pem_block(text: str) -> Optional[str]:
    """The first PEM certificate in `text` (a pin file carries a comment line)."""
    m = _PEM_RE.search(text or "")
    return m.group(0) if m else None


def pinned_fingerprint(hostname: str, home: Optional[Path] = None) -> Optional[str]:
    """The SHA-256 fingerprint of the host's pinned certificate, or None when
    there is no (readable) pin."""
    block = pem_block(pinned_pem(hostname, home) or "")
    if not block:
        return None
    try:
        return fingerprint(ssl.PEM_cert_to_DER_cert(block))
    except ValueError:
        return None


def save_pin(hostname: str, pem: str, home: Optional[Path] = None) -> Path:
    """Pin `pem` (the server's leaf certificate) for `hostname`; rebuild the
    bundle. The directory is 0700 (it is state, not a secret: certificates
    are public), the files 0644 so a runtime launched by another tool can
    read the bundle."""
    hostname = normalize_host(hostname)
    if not hostname:
        raise ValueError("a pin needs a hostname")
    if "-----BEGIN CERTIFICATE-----" not in pem:
        raise ValueError("a pin needs a PEM certificate")
    directory = pin_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = pin_path(hostname, home)
    header = f"# Cardinal: certificate pinned for {hostname} by cardinal-connect. Delete to forget.\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(header + pem.strip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    _rebuild_bundle(home)
    _context_cache.clear()
    return path


def forget_pin(hostname: str, home: Optional[Path] = None) -> bool:
    path = pin_path(hostname, home)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _rebuild_bundle(home)
    _context_cache.clear()
    return True


def _rebuild_bundle(home: Optional[Path]) -> None:
    pins = pinned_hosts(home)
    bundle = bundle_path(home)
    if not pins:
        try:
            bundle.unlink()
        except FileNotFoundError:
            pass
        return
    parts = ["# Cardinal: every certificate pinned under this directory, for NODE_EXTRA_CA_CERTS.\n"]
    for host, path in pins.items():
        try:
            parts.append(f"# {host}\n" + path.read_text(encoding="utf-8").strip() + "\n")
        except OSError:
            continue
    tmp = bundle.with_name(bundle.name + ".tmp")
    tmp.write_text("".join(parts), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, bundle)


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

def fingerprint(der: bytes) -> str:
    """SHA-256 of the DER certificate, colon-separated upper-case hex (what
    `openssl x509 -fingerprint -sha256` prints)."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def normalize_fingerprint(text: str) -> str:
    """A fingerprint as typed (colons or not, any case, an optional
    `sha256:`/`SHA256 Fingerprint=` prefix) in canonical form. ValueError
    when it is not 64 hex digits."""
    raw = (text or "").strip()
    raw = re.sub(r"(?i)^(sha-?256\s*(fingerprint)?\s*[:=]\s*)", "", raw)
    raw = raw.replace(":", "").replace(" ", "").lower()
    if not _HEX_RE.match(raw):
        raise ValueError(f"not a SHA-256 fingerprint: {text!r}")
    return ":".join(raw[i:i + 2] for i in range(0, 64, 2)).upper()


@dataclass
class CertInfo:
    pem: str
    der: bytes
    fingerprint: str
    subject: str = ""
    issuer: str = ""
    names: List[str] = field(default_factory=list)
    not_before: str = ""
    not_after: str = ""

    @property
    def self_signed(self) -> bool:
        return bool(self.subject) and self.subject == self.issuer

    def names_host(self, hostname: str) -> bool:
        """Whether a hostname-checking client (every runtime's own) would
        accept this certificate for `hostname`: a SAN matches exactly, or a
        leftmost-label wildcard does."""
        return any(_name_matches(n, hostname) for n in self.names)


def _name_matches(pattern: str, hostname: str) -> bool:
    pattern, hostname = normalize_host(pattern), normalize_host(hostname)
    if not pattern or not hostname:
        return False
    if pattern == hostname:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return hostname.endswith(suffix) and hostname.count(".") == pattern.count(".")
    return False


def _rdn_text(seq) -> str:
    parts = []
    for rdn in seq or ():
        for key, value in rdn:
            short = {"commonName": "CN", "organizationName": "O", "organizationalUnitName": "OU",
                     "countryName": "C", "stateOrProvinceName": "ST", "localityName": "L"}.get(key, key)
            parts.append(f"{short}={value}")
    return ", ".join(parts)


def _pin_context(pem: str) -> ssl.SSLContext:
    """Verify against exactly `pem`: trust store = that certificate, hostname
    check off (a self-signed certificate rarely names the host it is served
    from), PARTIAL_CHAIN so a pinned leaf under a private CA verifies too."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    partial = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", None)
    if partial is not None:
        ctx.verify_flags |= partial
    ctx.load_verify_locations(cadata=pem)
    return ctx


def _handshake(hostname: str, port: int, ctx: ssl.SSLContext, timeout: float, server_hostname: str):
    """One TLS handshake; the peer certificate as (DER, parsed dict or None)."""
    with socket.create_connection((hostname, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=server_hostname) as tls:
            return tls.getpeercert(binary_form=True) or b"", (tls.getpeercert() or None)


def describe(der: bytes, parsed: Optional[dict] = None) -> CertInfo:
    pem = ssl.DER_cert_to_PEM_cert(der)
    info = CertInfo(pem=pem, der=der, fingerprint=fingerprint(der))
    if parsed:
        info.subject = _rdn_text(parsed.get("subject"))
        info.issuer = _rdn_text(parsed.get("issuer"))
        info.names = [v for kind, v in parsed.get("subjectAltName", ()) if kind == "DNS"]
        info.names += [f"IP:{v}" for kind, v in parsed.get("subjectAltName", ()) if kind == "IP Address"]
        info.not_before = parsed.get("notBefore") or ""
        info.not_after = parsed.get("notAfter") or ""
    return info


@dataclass
class Probe:
    url: str
    hostname: str
    trusted: bool
    via: str = ""            # "system" | "pin" | ""
    reason: str = ""         # verification failure, user-presentable
    cert: Optional[CertInfo] = None
    error: str = ""          # a non-TLS failure (DNS, refused): nothing to trust


def _split(url: str) -> "tuple[str, int]":
    """(hostname, port) of an https URL; ("", 0) for anything else."""
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
        port = parts.port or 443
    except ValueError:
        return "", 0
    if parts.scheme != "https":
        return "", 0
    return normalize_host(parts.hostname or ""), port


def probe(url: str, home: Optional[Path] = None, timeout: float = PROBE_TIMEOUT_SECS) -> Probe:
    """Can this machine verify `url`'s certificate? A pinned host is checked
    against its pin, any other against the system store. When neither
    verifies, the leaf is fetched without verification and described so the
    user can decide. Never raises for a TLS reason; a non-TLS network
    failure lands in `error`."""
    hostname, port = _split(url)
    if not hostname:
        return Probe(url=url, hostname="", trusted=True, via="plain")
    pinned = pinned_pem(hostname, home)
    attempts = []
    if pinned:
        attempts.append(("pin", _pin_context(pinned)))
    attempts.append(("system", ssl.create_default_context()))
    reasons = {}
    for via, ctx in attempts:
        try:
            der, parsed = _handshake(hostname, port, ctx, timeout, hostname)
            return Probe(url=url, hostname=hostname, trusted=True, via=via, cert=describe(der, parsed))
        except ssl.SSLCertVerificationError as exc:
            reasons[via] = getattr(exc, "verify_message", "") or str(exc)
        except ssl.SSLError as exc:
            reasons[via] = str(exc)
        except (OSError, ValueError) as exc:
            return Probe(url=url, hostname=hostname, trusted=False, error=f"{type(exc).__name__}: {exc}")
    # A pinned host that no longer matches its pin is the story, not the
    # system store's verdict (which was never going to trust it).
    if "pin" in reasons:
        reason = f"{reasons['pin']} (certificate changed since it was pinned)"
    else:
        reason = reasons.get("system", "")
    # Untrusted: fetch the leaf without verifying, then parse it by trusting
    # exactly that certificate (getpeercert() is only filled in when the
    # handshake verified something).
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    try:
        der, _ = _handshake(hostname, port, unverified, timeout, hostname)
    except (OSError, ValueError) as exc:
        return Probe(url=url, hostname=hostname, trusted=False, reason=reason,
                     error=f"{type(exc).__name__}: {exc}")
    info = describe(der)
    try:
        _, parsed = _handshake(hostname, port, _pin_context(info.pem), timeout, hostname)
        info = describe(der, parsed)
    except (OSError, ValueError):
        pass
    return Probe(url=url, hostname=hostname, trusted=False, reason=reason, cert=info)


# ---------------------------------------------------------------------------
# urllib integration
# ---------------------------------------------------------------------------

_context_cache: Dict[str, ssl.SSLContext] = {}


def context_for(hostname: str, home: Optional[Path] = None) -> Optional[ssl.SSLContext]:
    """The pin context for a pinned hostname, else None (use the default)."""
    hostname = normalize_host(hostname)
    if not hostname:
        return None
    path = pin_path(hostname, home)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
    ctx = _context_cache.get(key)
    if ctx is None:
        try:
            ctx = _pin_context(path.read_text(encoding="utf-8"))
        except (OSError, ssl.SSLError, ValueError):
            return None
        _context_cache[key] = ctx
    return ctx


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib's HTTPS handler, except that a pinned host is verified against
    its pin. Reads the pin store per request (cached by mtime), so a pin
    saved during this process takes effect at once."""

    def __init__(self, home: Optional[Path] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pin_home = home

    def https_open(self, req):
        ctx = context_for(urllib.parse.urlsplit(req.full_url).hostname or "", self._pin_home)
        if ctx is None:
            return super().https_open(req)
        return self.do_open(http.client.HTTPSConnection, req, context=ctx)


def https_handler(home: Optional[Path] = None) -> PinnedHTTPSHandler:
    """For callers that build their own opener: `build_opener(..., https_handler())`."""
    return PinnedHTTPSHandler(home)


_installed = False


def install(home: Optional[Path] = None, force: bool = False) -> None:
    """Make `urllib.request.urlopen` honor pins process-wide. Idempotent;
    cardinal_core calls it on import. Harmless when nothing is pinned: the
    handler then behaves exactly like urllib's own."""
    global _installed
    if _installed and not force:
        return
    urllib.request.install_opener(urllib.request.build_opener(PinnedHTTPSHandler(home)))
    _installed = True


# ---------------------------------------------------------------------------
# connect-time decision
# ---------------------------------------------------------------------------

def hosts_of(urls: Sequence[Optional[str]]) -> List[str]:
    """The distinct https hostnames in `urls`, each with its port when it is
    not 443 (so the probe reaches the right listener; pins are per hostname)."""
    seen: List[str] = []
    for url in urls:
        if not url:
            continue
        hostname, port = _split(str(url))
        if not hostname:
            continue
        netloc = hostname if port == 443 else f"{hostname}:{port}"
        if netloc not in seen:
            seen.append(netloc)
    return seen


def format_untrusted(p: Probe, command: str) -> str:
    """The message shown when `p` is untrusted: what the certificate is and
    how to trust it. `command` is the connect command to repeat."""
    c = p.cert
    lines = [f"✗ {p.hostname} presents a certificate this machine does not trust:",
             f"    {p.reason or 'verification failed'}"]
    if c:
        issuer = c.issuer + (" (self-signed)" if c.self_signed else "")
        names = ", ".join(c.names) if c.names else "(none)"
        match = "" if c.names_host(p.hostname) else f"   ← does not name {p.hostname}"
        lines += [f"    Subject:   {c.subject or '(unknown)'}",
                  f"    Issuer:    {issuer or '(unknown)'}",
                  f"    Names:     {names}{match}",
                  f"    Valid:     {c.not_before or '?'} → {c.not_after or '?'}",
                  f"    SHA-256:   {c.fingerprint}"]
    lines += ["",
              "  Most Cardinal sites use a certificate this machine already trusts, so this",
              "  is unusual. Trust it only if you expect it (a self-hosted Cardinal you run).",
              f"  To trust exactly this certificate for {p.hostname}, re-run:"]
    if c:
        lines.append(f"    {command} --trust-cert {c.fingerprint}")
    else:
        lines.append(f"    {command} --trust-cert <sha256 fingerprint>")
    return "\n".join(lines)


def ensure_trusted(
    url: str,
    *,
    trust: Sequence[str] = (),
    interactive: Optional[bool] = None,
    log: Callable[[str], None] = print,
    ask: Optional[Callable[[str], str]] = None,
    command: str = "cardinal-connect",
    home: Optional[Path] = None,
    timeout: float = PROBE_TIMEOUT_SECS,
) -> Optional[CertInfo]:
    """Make sure `url`'s host can be verified before connect talks to it.

    Returns None when it already can (system-trusted, pinned, or plain
    http), or the CertInfo just pinned. When the certificate is untrusted:
    a `--trust-cert` fingerprint (`trust`) that matches pins it; a terminal
    (`interactive`, default: stdin is a TTY) asks; otherwise, or on a
    fingerprint that does not match, raises CertificateError with the full
    explanation (connect exits EXIT_UNTRUSTED_CERT).
    """
    p = probe(url, home=home, timeout=timeout)
    if p.trusted:
        return None
    if p.cert is None:
        # Not a trust decision: DNS, refused, timeout. Let the caller's
        # request fail with its own message.
        return None
    wanted = []
    for item in trust:
        try:
            wanted.append(normalize_fingerprint(item))
        except ValueError as exc:
            raise CertificateError(f"--trust-cert: {exc}") from None
    if wanted:
        if p.cert.fingerprint in wanted:
            save_pin(p.hostname, p.cert.pem, home)
            log(f"  ✓ Pinned the certificate for {p.hostname} ({p.cert.fingerprint[:23]}…) "
                f"in {pin_dir(home)}")
            return p.cert
        raise CertificateError(
            f"✗ {p.hostname} presented a certificate whose fingerprint\n"
            f"    {p.cert.fingerprint}\n"
            f"  does not match --trust-cert ({', '.join(wanted)}). Not trusting it.\n"
            "  If the server's certificate changed, run connect again without --trust-cert\n"
            "  and check the new fingerprint with whoever runs this Cardinal.")
    message = format_untrusted(p, command)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        raise CertificateError(message)
    log(message)
    log("")
    answer = (ask or input)(f"  Trust this certificate for {p.hostname}? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise CertificateError(f"✗ Not trusting the certificate for {p.hostname}.")
    save_pin(p.hostname, p.cert.pem, home)
    log(f"  ✓ Pinned the certificate for {p.hostname} in {pin_dir(home)}")
    return p.cert


def ensure_hosts_trusted(urls: Sequence[Optional[str]], **kwargs) -> List[CertInfo]:
    """ensure_trusted for every distinct https host in `urls` (the bundle's
    ingest endpoint, MCP URL, act endpoint). Returns what was newly pinned."""
    pinned: List[CertInfo] = []
    for hostname in hosts_of(urls):
        info = ensure_trusted(f"https://{hostname}", **kwargs)
        if info is not None:
            pinned.append(info)
    return pinned


def pins_for(urls: Sequence[Optional[str]], home: Optional[Path] = None) -> List[str]:
    """The hosts among `urls` that are pinned (what a connection relies on),
    as hosts_of spells them (`host` or `host:port`)."""
    pinned = pinned_hosts(home)
    return [h for h in hosts_of(urls) if hostname_of(h) in pinned]


def hostname_of(netloc: str) -> str:
    """The hostname of a hosts_of entry (`host` or `host:port`)."""
    return normalize_host(netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc)


def connection_urls(bundle: Optional[dict], host: str) -> List[Optional[str]]:
    """Every URL a connection talks to: the app host and whatever the device
    flow's bundle names (ingest endpoint, MCP URL, act endpoint, limits)."""
    b = bundle or {}
    return [host, (b.get("ingest") or {}).get("endpoint"), (b.get("mcp") or {}).get("url"),
            (b.get("act") or {}).get("endpoint"), (b.get("limits") or {}).get("status_url")]


def state_block(bundle: Optional[dict], host: str, home: Optional[Path] = None) -> Optional[dict]:
    """What a connect state file records when the connection relies on a pin:
    {"pinned_hosts": [hostname, …]}, or None."""
    pinned = pins_for(connection_urls(bundle, host), home)
    if not pinned:
        return None
    return {"pinned_hosts": [hostname_of(h) for h in pinned]}


def runtime_lines(runtime: str, bundle: Optional[dict], host: str, home: Optional[Path] = None) -> List[str]:
    """runtime_note for a connection, or [] when it relies on no pin."""
    pinned = pins_for(connection_urls(bundle, host), home)
    if not pinned:
        return []
    names_ok = True
    for netloc in pinned:
        p = probe("https://" + netloc, home=home)
        if p.cert is None or not p.cert.names_host(p.hostname):
            names_ok = False
    return runtime_note(runtime, [hostname_of(h) for h in pinned], names_ok, home)


def runtime_note(runtime: str, hosts: Sequence[str], certs_name_hosts: bool, home: Optional[Path] = None) -> List[str]:
    """What the user must do for the runtime's OWN HTTPS clients (its MCP
    client, its OTel exporter), which this plugin cannot reconfigure except
    for Claude Code. Lines, ready to print."""
    bundle = bundle_path(home)
    which = ", ".join(hosts)
    lines = [f"  ⚠ The pinned certificate ({which}) covers Cardinal's hooks and scripts."]
    if runtime == "claude":
        lines.append(f"    Claude Code's own MCP client and telemetry exporter read NODE_EXTRA_CA_CERTS={bundle}")
        lines.append("    from ~/.claude/settings.json env (written by connect).")
    elif runtime == "codex":
        lines.append("    Codex's own MCP client trusts the system certificate store: add the certificate")
        lines.append(f"    ({bundle}) to it (macOS Keychain: `security add-trusted-cert`; Debian/Ubuntu:")
        lines.append("    copy to /usr/local/share/ca-certificates/ and run update-ca-certificates).")
    elif runtime == "cursor":
        lines.append("    Cursor's own MCP client is not configured by this plugin: add the certificate")
        lines.append(f"    ({bundle}) to the system certificate store, or launch Cursor with")
        lines.append(f"    NODE_EXTRA_CA_CERTS={bundle} in its environment.")
    else:  # gemini, copilot, opencode, pi: Node / Bun runtimes launched from a shell
        lines.append(f"    For the runtime's own MCP client and telemetry, export NODE_EXTRA_CA_CERTS={bundle}")
        lines.append("    in the shell that launches it (Node reads it at startup).")
    if not certs_name_hosts:
        lines.append("    The certificate does not name the host, and those clients check names, so a")
        lines.append("    trust anchor alone will not satisfy them: either give the host a certificate")
        lines.append("    that names it, or reach Cardinal by a name the certificate carries.")
    return lines


# Importing this module is how a process opts in: cardinal_core's HTTP
# modules and every adapter script that talks to Cardinal import it.
install()
