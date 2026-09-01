"""Getting a mailbox into the archive, without knowing whose mailbox it is.

The engine walks one account through :class:`~mailarc_core.mail.ports.MailSourcePort`
and hands every message to ``mailarc_core.archive``. It never names a provider:
the registry is how Gmail reaches it, and ``app/composition.py`` is the only
place that puts it there.

One module per concern, layered so nothing points back up:

``model``
    Value objects — what a run targets, what it carries, what it counted.
``config``
    ``SyncConfig`` — the pipeline's shape and the worker's timings.
``registry``
    ``ProviderRegistry`` — descriptor plus factory, keyed by provider.
``engine``
    ``ImportEngine`` — the pipeline of §7.3, in that order.
``fake``
    ``FakeMailSource`` — a directory of ``.eml`` files behind the port, and the
    second implementation that makes the port a port.
"""

from mailarc_sync.engine.config import SyncConfig, default_worker_id
from mailarc_sync.engine.engine import ImportEngine
from mailarc_sync.engine.fake import DESCRIPTOR as FAKE_DESCRIPTOR
from mailarc_sync.engine.fake import FakeMailSource
from mailarc_sync.engine.model import (
    ImportCounts,
    ImportProgress,
    ImportResult,
    ImportTarget,
    MessageFailure,
    PreparedMessage,
)
from mailarc_sync.engine.registry import ProviderRegistry, UnknownProviderError

__all__ = [
    "FAKE_DESCRIPTOR",
    "FakeMailSource",
    "ImportCounts",
    "ImportEngine",
    "ImportProgress",
    "ImportResult",
    "ImportTarget",
    "MessageFailure",
    "PreparedMessage",
    "ProviderRegistry",
    "SyncConfig",
    "UnknownProviderError",
    "default_worker_id",
]
