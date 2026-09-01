"""Every role this archive knows, as one catalogue.

Archive-wide policy rather than a piece of user interface, which is why it
lives in the core: it names no Reflex type, it is readable from a worker or a
CLI, and every layer above may import ``mailarc_core``. Putting an
authorisation catalogue inside the UI package would be the surprising choice —
the browser would own a rule the worker also has to obey.

A module rather than a package: it is a list of decisions with no I/O and no
second file to split into.
"""

from appkit_commons.roles import Role

PROJECT_MANAGER_ROLE = Role(
    id=1,
    name="project_manager",
    label="Projektmanager",
    description="Berechtigung für den Projektmanager",
)


ALL_ROLES: list[Role] = [
    PROJECT_MANAGER_ROLE,
]
