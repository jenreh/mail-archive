"""The archive has no sign-in, so where the socket listens *is* the access control.

``tests/test_every_page_is_public.py`` pins the decision that no page asks who
is looking. This file pins the thing that makes that decision survivable: the
backend answers only the machine it runs on, and only pages served from it may
open its event socket. Take either away and "every page is public" stops
meaning *the owner sees everything* and starts meaning *everyone does*.

Its own file rather than a case in that one, because the claim is about the
transport rather than about a page, and the two fail for entirely different
reasons — a decorator swap on one side, a Reflex default reasserting itself on
the other.

Both defaults are Reflex's, and both are the wrong way round for an
application with no authentication:

``backend_host`` defaults to ``"0.0.0.0"`` (``reflex_base/config.py``), and no
launcher in this repository passes ``--backend-host`` — the desktop shell runs
a bare ``reflex run --env prod`` (``src-tauri/src/backend.rs``) and so does
every Taskfile target. Left alone it puts the whole archive on whatever
network the laptop is joined to.

``cors_allowed_origins`` defaults to ``("*",)``, which ``reflex/app.py`` hands
to engineio as the literal ``"*"`` — and engineio then skips the handshake
origin check outright rather than merely widening it
(``base_server.py``: ``'*'`` maps to ``allowed_origins = None``, and
``async_server.py`` only rejects when it is *not* ``None``). Browsers do not
apply CORS to a WebSocket handshake, so that server-side check is the only
thing between any page the reader visits and ``/_event``. A socket accepted
there is a working session, because there is no sign-in for it to fail.

Neither assertion pins a *port*. The ports are configuration and move between
profiles; the host and the absence of a wildcard are the security properties,
and they hold whatever the ports are.
"""

from urllib.parse import urlsplit

import rxconfig

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""The hosts that mean *this machine and no other*.

Spelled as a set because the loopback interface has more than one name and
which one is right is a matter of taste, not of security: any of them binds
the same socket. What matters is that ``0.0.0.0`` and ``::`` are not among
them.
"""


def test_backend_binds_loopback_only() -> None:
    """The bind, which is the whole of the archive's access control.

    ``0.0.0.0`` here is not a hardening lapse — it is the difference between a
    private archive and one that answers to every device on a café network,
    with no credential to stop them.
    """
    assert rxconfig.config.backend_host in LOOPBACK_HOSTS, (
        f"backend_host is {rxconfig.config.backend_host!r}; an archive with no "
        "sign-in must not answer off-machine"
    )


def test_event_socket_names_its_origins() -> None:
    """No wildcard, because a wildcard is not a wide check but no check.

    Asserted as *the wildcard is absent* rather than as a fixed list: the list
    is derived from the configured ports and is allowed to differ per profile.
    A single ``"*"`` anywhere in it collapses the whole thing back to ``None``
    inside engineio, so the membership test is the assertion that matters.
    """
    origins = list(rxconfig.config.cors_allowed_origins)

    assert origins, "an empty origin list would refuse the app's own frontend"
    assert "*" not in origins, (
        "a '*' anywhere in cors_allowed_origins disables engineio's handshake "
        "origin check entirely, letting any site the reader visits open a "
        "session against their mail"
    )


def test_allowed_origins_cover_the_ports_actually_served() -> None:
    """The list and the ports are read from one place, and must stay that way.

    The failure this prevents is a quiet one: an origin list naming a port the
    app no longer serves does not warn, it refuses the app's own frontend and
    the page sits there with a socket that will not open. Asserting the two
    sides against each other means a profile that moves a port fails here, on
    a named line, rather than in a browser.
    """
    served = {
        port
        for port in (rxconfig.config.frontend_port, rxconfig.config.backend_port)
        if port is not None
    }
    named = {
        port
        for origin in rxconfig.config.cors_allowed_origins
        if (port := urlsplit(origin).port) is not None
    }

    assert served, "neither port is configured, so this proves nothing"
    assert served <= named, f"ports {sorted(served - named)} are served but not allowed"


def test_allowed_origins_are_all_loopback() -> None:
    """And the named origins are the app's own, not somebody else's.

    The complement of the test above: dropping the wildcard for a list that
    names a public site would pass it and give the same access away.
    Deployments that genuinely front the app from elsewhere say so through
    ``REFLEX_CORS_ALLOWED_ORIGINS``, which Reflex applies after this module's
    kwargs, or through the configured ``deploy_url``/``api_url``.
    """
    settings = rxconfig.reflex
    configured = (
        {url for url in (settings.deploy_url, settings.api_url) if url}
        if settings
        else set()
    )
    for origin in rxconfig.config.cors_allowed_origins:
        if origin in configured:
            continue
        assert urlsplit(origin).hostname in LOOPBACK_HOSTS, (
            f"{origin} is neither loopback nor the configured deploy url"
        )
