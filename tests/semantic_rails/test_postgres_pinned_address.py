"""The real libpq driver with ``hostaddr``: it dials the given address and never resolves
``host``, which still names the server for TLS.

A local socket server stands in for the database and records what each connection sent. The
host name ends in ``.invalid``, which no resolver answers (RFC 6761), so a connection that reached
the server cannot have come from a lookup of the name.
"""

from __future__ import annotations

import datetime
import socket
import ssl
import struct
import threading
from pathlib import Path

import pytest

from semantic_rails.db_parts.adbc import AdbcAdapter
from semantic_rails.errors import SemanticLayerError

pytest.importorskip("adbc_driver_postgresql.dbapi")

HOST = "warehouse.example.invalid"
SSL_REQUEST = struct.pack("!II", 8, 80877103)


@pytest.fixture(autouse=True)
def _hermetic_libpq(monkeypatch: pytest.MonkeyPatch) -> None:
    # libpq reads these as defaults; none may decide where or how a test connects.
    for name in (
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSSLMODE",
        "PGSSLROOTCERT",
        "PGSSLSNI",
        "PGREQUIRESSL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SR_PIN_TEST_PASSWORD", "not-a-real-password")


class _Server:
    """Accept connections on 127.0.0.1 and record the server name and first packet of each."""

    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        self.tls = tls
        self.server_names: list[str | None] = []
        self.startups: list[bytes] = []
        self._socket = socket.create_server(("127.0.0.1", 0))
        self._socket.settimeout(0.05)
        self._stopped = threading.Event()
        self.port = self._socket.getsockname()[1]
        if tls is not None:
            tls.sni_callback = lambda _sock, name, _ctx: self.server_names.append(name)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    first = conn.recv(8)
                    if first == SSL_REQUEST and self.tls is not None:
                        conn.sendall(b"S")
                        with self.tls.wrap_socket(conn, server_side=True) as secured:
                            first = secured.recv(8)
                    if first:
                        # A startup packet: its length, then protocol 3.0.
                        self.startups.append(first)
                except (OSError, ssl.SSLError):
                    pass
                # Closing here ends the session; libpq reports it as a failed connection.

    def close(self) -> None:
        self._stopped.set()
        self._thread.join(5)
        self._socket.close()


@pytest.fixture
def plain_server():
    server = _Server()
    yield server
    server.close()


def _options(port: int, **extra: str) -> dict[str, str]:
    return {
        "host": HOST,
        "port": str(port),
        "user": "svc",
        "password_env": "SR_PIN_TEST_PASSWORD",
        "connect_timeout_seconds": "5",
        **extra,
    }


def _failed_query(adapter: AdbcAdapter) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("SELECT 1")
    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    return exc.value


def test_a_pinned_address_is_dialed_on_every_connect_without_resolving_the_host(plain_server):
    adapter = AdbcAdapter(_options(plain_server.port, hostaddr="127.0.0.1", sslmode="disable"))

    # The server ends each session, so the adapter discards it and the next query connects
    # again, as a pooled adapter does after a failure.
    _failed_query(adapter)
    _failed_query(adapter)

    assert len(plain_server.startups) == 2
    assert all(packet[4:] == struct.pack("!I", 0x00030000) for packet in plain_server.startups)


def test_without_a_pinned_address_the_host_is_resolved_as_before(plain_server):
    adapter = AdbcAdapter(_options(plain_server.port, sslmode="disable"))

    error = _failed_query(adapter)

    # libpq looked the name up itself, and nothing answers it.
    assert "could not translate host name" in str(error.__cause__)
    assert plain_server.startups == []


def _certificates(tmp_path: Path, name: str) -> tuple[Path, ssl.SSLContext]:
    """A throwaway CA file for libpq, and a server context whose certificate is for ``name``."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file, key_file = tmp_path / "server.crt", tmp_path / "server.key"
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return ca_file, context


@pytest.mark.parametrize(
    ("certificate_for", "verified"),
    [(HOST, True), ("other.example.invalid", False)],
    ids=["certificate-for-host", "certificate-for-another-name"],
)
def test_verify_full_checks_the_host_name_at_a_pinned_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate_for: str, verified: bool
):
    ca_file, context = _certificates(tmp_path, certificate_for)
    monkeypatch.setenv("PGSSLROOTCERT", str(ca_file))
    server = _Server(tls=context)
    try:
        adapter = AdbcAdapter(_options(server.port, hostaddr="127.0.0.1", sslmode="verify-full"))
        error = _failed_query(adapter)
    finally:
        server.close()

    # TLS names the host, never the address it was reached at.
    assert server.server_names == [HOST]
    if verified:
        # libpq accepted the certificate and went on to start the session.
        assert len(server.startups) == 1
    else:
        assert server.startups == []
        assert f'does not match host name "{HOST}"' in str(error.__cause__)
