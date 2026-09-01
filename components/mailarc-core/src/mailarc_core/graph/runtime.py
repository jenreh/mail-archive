"""Locating the vendored FalkorDB binaries.

The desktop app ships its own ``redis-server`` plus the FalkorDB module so it
needs nothing installed on the target machine. This module only *finds and
validates* those files — producing them is the job of
``scripts/vendor_falkordb.py``, which runs at build time.
"""

import logging
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

#: Set by the Tauri shell to its bundle resource directory.
RUNTIME_DIR_ENV_VAR = "MAIL_ARCHIVE_FALKORDB_DIR"

#: Where `task tauri:vendor` writes, relative to the repository root.
DEFAULT_RUNTIME_DIR = Path("src-tauri/resources/falkordb")

REDIS_SERVER_NAME = "redis-server"
MODULE_NAME = "falkordb.so"

#: The FalkorDB module links OpenSSL, which we vendor next to it and repoint at
#: ``@loader_path``. Missing dylibs fail at ``dlopen`` time with an opaque
#: redis-server error, so check for them up front instead.
MACOS_DYLIBS = ("libssl.3.dylib", "libcrypto.3.dylib")

VENDOR_HINT = "run `task tauri:vendor` to fetch and prepare them"


class GraphRuntimeError(RuntimeError):
    """The vendored FalkorDB binaries are missing or unusable."""


class FalkorDBRuntime(BaseModel):
    """Validated paths to the binaries needed to start a local FalkorDB."""

    model_config = ConfigDict(frozen=True)

    directory: Path
    redis_server: Path
    module: Path

    @classmethod
    def resolve(cls, configured: Path | None = None) -> FalkorDBRuntime:
        """Find the runtime directory and check it is complete.

        Resolution order: the ``MAIL_ARCHIVE_FALKORDB_DIR`` environment
        variable, then the configured path, then the in-repo default.
        """
        directory = _resolve_directory(configured)
        redis_server = directory / REDIS_SERVER_NAME
        module = directory / MODULE_NAME

        missing = [p.name for p in (redis_server, module) if not p.is_file()]
        if sys.platform == "darwin":
            missing += [
                name for name in MACOS_DYLIBS if not (directory / name).is_file()
            ]
        if missing:
            raise GraphRuntimeError(
                f"FalkorDB runtime incomplete in {directory}: "
                f"missing {', '.join(missing)} — {VENDOR_HINT}"
            )

        # redis-server dlopens the module and rejects it unless it too carries
        # the execute bit.
        not_executable = [
            p.name for p in (redis_server, module) if not os.access(p, os.X_OK)
        ]
        if not_executable:
            raise GraphRuntimeError(
                f"Not executable in {directory}: {', '.join(not_executable)} "
                f"— {VENDOR_HINT}"
            )

        logger.debug("Resolved FalkorDB runtime at %s", directory)
        return cls(directory=directory, redis_server=redis_server, module=module)


def _resolve_directory(configured: Path | None) -> Path:
    """Resolve to an ABSOLUTE directory.

    redis-server chdirs into its `--dir` before dlopening the module, so a
    relative module path fails with a bare "no such file" from dyld.
    """
    from_env = os.environ.get(RUNTIME_DIR_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser().resolve()
    if configured is not None:
        return configured.expanduser().resolve()
    return DEFAULT_RUNTIME_DIR.resolve()
