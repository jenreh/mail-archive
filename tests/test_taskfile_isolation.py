"""No taskfile may push its settings into another taskfile's tasks.

go-task merges every included taskfile's file-level ``env:`` into ONE
environment shared by the whole project. A block written to configure one
namespace therefore configures all of them, and this project has now been bitten
by that twice, in both directions:

* ``Taskfile.{db,graph}.yml`` templated ``{{.PROFILES}}``, which resolved
  against ``Taskfile.tauri.yml``'s global var ``prod`` and exported
  ``PROFILES=prod`` to every namespace — including the agent sandbox, which then
  ran a schema migration against the real archive.
* ``Taskfile.agent.yml`` declared the sandbox as a file-level ``env:``, which
  pointed ``task run``'s blob store at ``.state-agent/mailstore`` and its graph
  data directory at ``.state-agent/falkordb`` — the sandbox contaminating the
  real application, whose store is content-addressed and write-once.

Neither was visible in the file that caused it; both were only visible from the
outside. So this asserts the property from the outside, by running ``task`` and
reading what a task actually gets, rather than by reading YAML and reasoning
about precedence.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SANDBOX_KEYS = (
    "app_archive_store_dir",
    "app_graph_data_dir",
    "app_graph_graph_name",
    "app_graph_port",
    "app_database_url_override",
)
"""The settings that decide which archive is written to.

Every one of these has a default that points at the real archive, so a task
that inherits one is a task pointed somewhere its author did not choose.
"""

PROFILE_KEY = "PROFILES"
"""The other half, and the one that actually leaked.

``PROFILES`` chooses which ``configuration/config.*.yaml`` is read, which is
where the database URL and the graph name come from — so a leaked profile
points a task at another archive just as surely as a leaked ``app_*`` does. It
is listed separately because it is legitimate *inside* a task's own ``env:``
and illegitimate only when a task that never named it receives one.
"""

LEAK_KEYS = (*SANDBOX_KEYS, PROFILE_KEY)

PROBE = "env-probe"
"""The task the behavioural half reads.

A root task whose whole body is ``env``, so everything it prints was pushed at
it by somebody else. The previous version of this file ran ``task --list``
instead and looked for ``KEY=VALUE`` lines in the *listing* — a listing prints
task names and descriptions and never an environment, so the filter matched
nothing, the assertion compared ``{}`` to ``{}``, and the test could not fail.
It did not fail when two taskfiles reintroduced the exact block it exists to
catch.
"""


@pytest.fixture(scope="module")
def task_binary() -> str:
    """The ``task`` runner, or a skip. It is not a Python dependency."""
    found = shutil.which("task")
    if found is None:
        pytest.skip("the `task` runner is not installed")
    return found


def _env_of(task_binary: str, task_name: str) -> dict[str, str]:
    """What one task sees, read by making it print its own environment.

    ``--dry`` will not do: it prints the command without running it, and the
    question here is precisely what the shell around the command holds. So the
    task is run for real — the task named below prints its environment and
    writes nothing.

    The inherited environment is replaced rather than extended, so that a
    ``PROFILES`` exported by whoever started pytest cannot be mistaken for one
    a taskfile pushed. ``PATH`` and ``HOME`` are the two ``task`` itself needs.
    """
    result = subprocess.run(  # noqa: S603 - a fixed binary and a literal task name
        [task_binary, task_name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(Path.home()),
        },
    )
    assert result.returncode == 0, f"task {task_name} failed:\n{result.stderr}"
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line and line.split("=", 1)[0] in LEAK_KEYS:
            key, value = line.split("=", 1)
            found[key] = value
    return found


def _tasks_of(taskfile: Path) -> dict[str, str]:
    """Each task in *taskfile*, mapped to the lines under it.

    Split on indentation rather than parsed as YAML, because ``pyyaml`` is not
    a declared dependency of this project and adding one to read six files
    would be a heavier change than the property is worth. Task names sit at two
    spaces under ``tasks:`` and everything below one, until the next, is its
    body — which is all this needs to ask "does this task declare a profile".
    """
    tasks: dict[str, list[str]] = {}
    current: str | None = None
    in_tasks = False
    for line in taskfile.read_text().splitlines():
        if line.rstrip() == "tasks:":
            in_tasks = True
            continue
        if not in_tasks or not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 2 and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
            tasks[current] = []
        elif current is not None:
            tasks[current].append(line)
    return {name: "\n".join(body) for name, body in tasks.items()}


class TestNoTaskfileConfiguresAnotherTaskfilesTasks:
    """A file-level ``env:`` is global, so no taskfile in this project may have one."""

    @pytest.mark.parametrize(
        "taskfile", sorted((ROOT / "taskfiles").glob("Taskfile.*.y*ml"))
    )
    def test_no_taskfile_declares_a_file_level_env(self, taskfile: Path) -> None:
        """The structural half: the block that leaks is simply not there.

        Every file rather than ``Taskfile.agent.yml`` alone, which is how the
        second occurrence got in — the check named the file that had made the
        mistake instead of naming the mistake, so ``Taskfile.db.yml`` and
        ``Taskfile.graph.yml`` each grew one and the suite stayed green.

        Asserted separately from the behavioural check below because it points
        at the line. A future edit that reintroduces ``env:`` fails here with a
        sentence and a file name, rather than failing somewhere confusing at
        run time.
        """
        body = "\n".join(
            line
            for line in taskfile.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )

        assert "\nenv:" not in body, (
            f"{taskfile.name} declares a file-level `env:`. go-task merges "
            "that into the environment of EVERY task in the project, so it "
            "configures namespaces this file has never heard of. Put the "
            "block inside the tasks that need it, the way "
            "Taskfile.reflex.yml does, or state it inline on the command, the "
            "way Taskfile.agent.yml's {{.SANDBOX}} does."
        )

    def test_a_task_that_asks_for_nothing_receives_nothing(
        self, task_binary: str
    ) -> None:
        """The behavioural half, read from outside the files that could break it.

        ``env-probe`` is a root task whose body is ``env``: it declares no
        environment of its own, so every variable it prints was put there by
        somebody else. Anything from :data:`LEAK_KEYS` arriving here is a
        file-level block in an included taskfile reaching across the project.
        """
        leaked = _env_of(task_binary, PROBE)

        assert leaked == {}, (
            f"{sorted(leaked)} reached a task that never asked for them. "
            "A file-level `env:` in an included taskfile is global."
        )

    @pytest.mark.parametrize("name", ["Taskfile.db.yml", "Taskfile.graph.yml"])
    def test_the_migration_tasks_still_name_their_own_profile(self, name: str) -> None:
        """The other half, or the fix above is satisfiable by deleting a setting.

        ``alembic`` and ``runic`` read ``DatabaseConfig`` and the composed
        ``AppConfig``, so which archive ``task db:upgrade`` migrates is decided
        by ``PROFILES`` and nothing else. Moving the declaration out of the
        file header is only correct if it lands in every task that runs one —
        and a task added later, under a header comment that no longer applies
        to it, is exactly how it would not.
        """
        for task, body in _tasks_of(ROOT / "taskfiles" / name).items():
            if "{{.RUNNER}}" not in body:
                continue
            assert "PROFILES:" in body, (
                f"{name}'s `{task}` runs a command against the configured "
                "archive without declaring PROFILES in its own `env:`. It "
                "would migrate whatever the default profile points at."
            )


class TestTheSandboxStillReachesItsOwnTasks:
    """The other half of the property: inline must actually work."""

    def test_the_agent_namespace_resolves_to_the_sandbox(
        self, task_binary: str
    ) -> None:
        """``task agent:check`` refuses unless every path is under .state-agent."""
        result = subprocess.run(  # noqa: S603 - fixed binary, literal task name
            [task_binary, "agent:check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, (
            "the agent sandbox no longer resolves to .state-agent/:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert ".state-agent" in result.stdout
