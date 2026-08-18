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
