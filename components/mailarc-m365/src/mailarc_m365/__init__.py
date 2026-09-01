"""Microsoft 365 mail over the Graph API, behind the one abstract seam.

Implements the core's mail source port and nothing else — consent, the
``/messages/delta`` walk, and the ``$value`` fetch that yields the same RFC
5322 bytes every other provider yields — so that Graph shapes stop at this
boundary. It hangs off the core alone: the engine that drives it never learns
its name, because ``app/composition.py`` does the registering.

The only provider whose cursor is a whole URL.
:attr:`~mailarc_core.mail.model.SyncCursor.token` is opaque to the engine,
which is what makes that legal — Graph's entire ``deltaLink`` goes in, nothing
above the port looks inside, and the day Microsoft changes its shape no other
component notices.

Also the only provider with two ways in. The plan left delegated (per user) and
app-only (per tenant, admin consent) as an open decision and noted that the
opaque credential blob carries both; it does, so both are here, discriminated
by a literal ``mode`` the account form asks for and defaulting to delegated.

``source/`` is the whole of it. The three names below are what
``app/composition.py`` needs — the descriptor the account form renders, the
class that builds a mailbox from a decrypted secret, and the consent step bound
to a configuration — so the composition root never has to reach into a
submodule:

.. code-block:: python

    registry.register(
        M365Source.DESCRIPTOR,
        M365Source.using(m365_config()),
        consent=consent_runner(m365_config()),
    )
"""

from mailarc_m365.source import M365_DESCRIPTOR, M365Source, consent_runner

__all__ = ["M365_DESCRIPTOR", "M365Source", "consent_runner"]
