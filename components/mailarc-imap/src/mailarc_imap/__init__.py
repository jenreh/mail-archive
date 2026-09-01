"""Any IMAP mailbox, behind the one seam this design allows to be abstract.

Implements the core's mail source port and nothing else — folder listing, UID
paging, and the ``RFC822`` fetch that yields the same bytes every other
provider yields — so that protocol shapes stop at this boundary. It hangs off
the core alone: the engine that drives it never learns its name, because
``app/composition.py`` does the registering.

The first provider with **no consent runner**, which is the path this component
exists to prove. An app password is complete the moment it is typed, so
``mail_credentials.secret`` is written by the account form rather than by a
browser round trip — ``json.dumps`` over this provider's own
``credential_fields``, every value a string, port numbers and booleans
included. Anything reading that secret has to accept that shape.

``source/`` is the whole of it. What ``app/composition.py`` registers — the
descriptor the account form renders and the class that builds a mailbox from a
decrypted secret — belongs at this level, so the composition root never has
to reach into a submodule.
"""

from mailarc_imap.source import IMAP_DESCRIPTOR, ImapSource

__all__ = ["IMAP_DESCRIPTOR", "ImapSource"]
