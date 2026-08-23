"""A throwaway certificate authority, so the tests take the path a real account takes.

The adapter has no way to turn TLS off — an app password in the clear is the
credential itself — so a fake IMAP server that spoke plaintext could not be
reached by it at all. The alternative to a certificate would be a client
context with verification disabled, which would mean the one thing this file
exists to prevent: a suite in which the adapter's certificate handling has
never once been exercised.

So the suite mints its own: a self-signed certificate for ``127.0.0.1``, valid
for a day, written to a temporary directory that is gone when the run ends. The
server serves it, ``ImapConfig.tls_ca_file`` trusts it, and nothing about
either is true outside this process.

``cryptography`` is already in this component's dependency closure —
``mailarc-core`` needs ``appkit-commons``, which needs it for the encrypted
columns — so this adds nothing to install.
"""

import datetime as dt
import ipaddress
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

LOOPBACK = "127.0.0.1"
VALID_FOR = dt.timedelta(days=1)
"""Long enough for any test run, short enough that a leaked file is worthless."""


class LoopbackCertificate:
    """A self-signed certificate for ``127.0.0.1`` and the two files it lives in."""

    def __init__(self, directory: Path) -> None:
        self.certificate = directory / "loopback.crt"
        self.key = directory / "loopback.key"
        _write(self.certificate, self.key)

    def server_context(self) -> ssl.SSLContext:
        """What ``asyncio.start_server`` needs to speak TLS."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, self.key)
        return context

    @property
    def ca_file(self) -> str:
        """What ``ImapConfig.tls_ca_file`` needs to believe it."""
        return str(self.certificate)


def _write(certificate: Path, key: Path) -> None:
    """Mint the pair. Elliptic curve, because RSA generation is measured in seconds."""
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "mailarc-imap test server")]
    )
    now = dt.datetime.now(dt.UTC)
    built = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - VALID_FOR)
        .not_valid_after(now + VALID_FOR)
        .add_extension(
            # An IP address in the subject alternative name, not a DNS name:
            # the client connects to a literal `127.0.0.1`, and `check_hostname`
            # matches a literal against `IPAddress` entries alone.
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(LOOPBACK))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private, hashes.SHA256())
    )
    certificate.write_bytes(built.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
