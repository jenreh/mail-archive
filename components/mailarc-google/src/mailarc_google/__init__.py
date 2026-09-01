"""Gmail, behind the one seam this design allows to be abstract.

Implements the core's mail source port and nothing else — OAuth, paging, and
the mapping from Google's JSON to the domain's value objects — so that provider
shapes stop at this boundary. It hangs off the core alone: the engine that
drives it never learns its name, because ``app/composition.py`` does the
registering.

``source/`` is the whole of it. The two names below are what
``app/composition.py`` registers — the descriptor the account form renders and
the class that builds a mailbox from a decrypted secret — so the composition
root never has to reach into a submodule.
"""

from mailarc_google.source import GMAIL_DESCRIPTOR, GmailSource

__all__ = ["GMAIL_DESCRIPTOR", "GmailSource"]
