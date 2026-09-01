"""What the web application's root DEBUG level must not switch on.

``app/app.py`` calls ``logging.basicConfig(level=logging.DEBUG)`` at import, and
that one line is a decision about every library in the process, not only about
this application's own modules. Three of them answer a DEBUG root by writing
somebody's secret into the log: ``oauthlib`` and ``requests_oauthlib`` print the
complete token response, refresh token included, and ``aiosqlite`` prints every
statement it executes together with its bound values — which is how the two
tables holding encrypted secrets would end up in an application log.

Asserted from a fresh interpreter, because the levels are module-level side
effects of importing ``app.app`` and this suite has already imported half of it
by the time any test runs. The probe prints numbers rather than names so a
future move from ``INFO`` to some other level is a visible change here.

The root itself is *not* left at DEBUG, which is the first thing below: appkit
runs ``dictConfig`` later in the same import and puts it back to INFO. The
guards are therefore explicit per-logger levels rather than a narrower root —
an explicit level wins whatever the root turns out to be.
"""

import subprocess
import sys

PROBE = """
import logging
import app.app

for name in ("oauthlib", "requests_oauthlib", "aiosqlite", "sqlalchemy.engine"):
    print(name, logging.getLogger(name).getEffectiveLevel(), sep="=")
print("root", logging.getLogger().getEffectiveLevel(), sep="=")
"""


def _levels() -> dict[str, int]:
    """The effective level of each named logger, after the application booted."""
    result = subprocess.run(  # noqa: S603 - this interpreter and a literal script
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"importing app.app failed:\n{result.stderr}"
    return {
        line.split("=", 1)[0]: int(line.split("=", 1)[1])
        for line in result.stdout.strip().splitlines()
        if "=" in line
    }


def test_the_basic_config_line_does_not_decide_the_root_level() -> None:
    """Measured, and worth writing down because the line says otherwise.

    ``app/app.py`` asks for ``DEBUG`` and the process ends at ``INFO``:
    appkit's ``init_logging`` runs ``dictConfig`` later in the same import and
    puts the root back. So a reader who raises or lowers that call and expects
    the application's own modules to follow will be looking at an unchanged
    log — which is the sort of thing that gets debugged for an hour.

    It also means the guards below are not merely tightening a DEBUG root:
    they are explicit levels, which beat whatever the root ends up at.
    """
    assert _levels()["root"] == 20


def test_no_library_that_prints_a_secret_is_left_at_debug() -> None:
    """The three that do, raised above DEBUG by name.

    ``aiosqlite`` is the one this project learned the hard way: at root DEBUG an
    insert into a table with an ``EncryptedString`` column prints as
    ``executing functools.partial(<cursor>, 'INSERT INTO ...',
    ('gAAAAAB...',))``, so ``mail_credentials.secret`` and
    ``semantic_settings.api_key`` reach the log as ciphertext and every other
    bound value reaches it in the clear.
    """
    levels = _levels()

    for name in ("oauthlib", "requests_oauthlib", "aiosqlite"):
        assert levels[name] > 10, f"{name} logs a secret at DEBUG"


def test_the_sqlalchemy_echo_is_held_below_info() -> None:
    """The one that must stay above INFO rather than above DEBUG.

    SQLAlchemy gates its statement-and-parameter echo on
    ``logger.isEnabledFor(INFO)`` against ``sqlalchemy.engine`` — not on
    ``echo=True`` — so quieting it to ``INFO`` alongside the other three would
    *switch the echo on* rather than off.
    """
    assert _levels()["sqlalchemy.engine"] > 20
