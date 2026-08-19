"""The relational store the application keeps its own tables in.

``sqlite``
    The async/sync URL split and the connection pragmas that appkit_commons'
    single database URL cannot express.
"""

from mailarc_core.database.sqlite import (
    database_path,
    ensure_database_directory,
    install_pragmas,
    prepare,
    sync_database_url,
)

__all__ = [
    "database_path",
    "ensure_database_directory",
    "install_pragmas",
    "prepare",
    "sync_database_url",
]
