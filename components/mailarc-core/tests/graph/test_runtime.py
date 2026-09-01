import re
import sys
from pathlib import Path

import pytest

from mailarc_core.graph.runtime import (
    DEFAULT_RUNTIME_DIR,
    MACOS_DYLIBS,
    RUNTIME_DIR_ENV_VAR,
    FalkorDBRuntime,
    GraphRuntimeError,
)


@pytest.fixture(autouse=True)
def _no_ambient_runtime_dir(monkeypatch):
    """The env var is set by the Tauri shell; keep it out of these tests."""
    monkeypatch.delenv(RUNTIME_DIR_ENV_VAR, raising=False)


def _complete_runtime(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    redis_server = directory / "redis-server"
    redis_server.write_bytes(b"#!/bin/sh\n")
    redis_server.chmod(0o755)
    module = directory / "falkordb.so"
    module.write_bytes(b"\xcf\xfa\xed\xfe")
    # redis-server refuses to dlopen a module without the execute bit.
    module.chmod(0o755)
    for name in MACOS_DYLIBS:
        (directory / name).write_bytes(b"\xcf\xfa\xed\xfe")
    return directory


def test_resolve_accepts_a_complete_runtime(tmp_path) -> None:
    directory = _complete_runtime(tmp_path / "falkordb")

    runtime = FalkorDBRuntime.resolve(directory)

    assert runtime.directory == directory.resolve()
    assert runtime.redis_server == directory.resolve() / "redis-server"
    assert runtime.module == directory.resolve() / "falkordb.so"


def test_paths_are_absolute(tmp_path, monkeypatch) -> None:
    """redis-server chdirs into its data dir before dlopening the module, so a
    relative module path fails with an opaque dyld "no such file"."""
    monkeypatch.chdir(tmp_path)
    _complete_runtime(tmp_path / DEFAULT_RUNTIME_DIR)

    runtime = FalkorDBRuntime.resolve(Path(DEFAULT_RUNTIME_DIR))

    assert runtime.directory.is_absolute()
    assert runtime.redis_server.is_absolute()
    assert runtime.module.is_absolute()


def test_env_var_wins_over_the_configured_path(tmp_path, monkeypatch) -> None:
    from_env = _complete_runtime(tmp_path / "from-env")
    monkeypatch.setenv(RUNTIME_DIR_ENV_VAR, str(from_env))

    runtime = FalkorDBRuntime.resolve(tmp_path / "configured")

    assert runtime.directory == from_env.resolve()


def test_falls_back_to_the_in_repo_default(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _complete_runtime(tmp_path / DEFAULT_RUNTIME_DIR)

    runtime = FalkorDBRuntime.resolve(None)

    assert runtime.directory == (tmp_path / DEFAULT_RUNTIME_DIR).resolve()


def test_missing_redis_server_names_the_file_and_the_fix(tmp_path) -> None:
    directory = _complete_runtime(tmp_path / "falkordb")
    (directory / "redis-server").unlink()

    with pytest.raises(GraphRuntimeError) as excinfo:
        FalkorDBRuntime.resolve(directory)

    assert "redis-server" in str(excinfo.value)
    assert "task tauri:vendor" in str(excinfo.value)


def test_missing_module_is_reported(tmp_path) -> None:
    directory = _complete_runtime(tmp_path / "falkordb")
    (directory / "falkordb.so").unlink()

    with pytest.raises(GraphRuntimeError, match=re.escape("falkordb.so")):
        FalkorDBRuntime.resolve(directory)


def test_an_empty_directory_lists_everything_that_is_missing(tmp_path) -> None:
    directory = tmp_path / "falkordb"
    directory.mkdir()

    with pytest.raises(GraphRuntimeError) as excinfo:
        FalkorDBRuntime.resolve(directory)

    message = str(excinfo.value)
    assert "redis-server" in message
    assert "falkordb.so" in message


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS dylib vendoring")
def test_missing_openssl_dylib_is_caught_before_dlopen(tmp_path) -> None:
    directory = _complete_runtime(tmp_path / "falkordb")
    (directory / "libssl.3.dylib").unlink()

    with pytest.raises(GraphRuntimeError, match=re.escape("libssl.3.dylib")):
        FalkorDBRuntime.resolve(directory)


@pytest.mark.parametrize("filename", ["redis-server", "falkordb.so"])
def test_a_file_without_the_execute_bit_is_rejected(tmp_path, filename) -> None:
    """redis-server aborts at startup on a non-executable module, so catch it here."""
    directory = _complete_runtime(tmp_path / "falkordb")
    (directory / filename).chmod(0o644)

    with pytest.raises(GraphRuntimeError) as excinfo:
        FalkorDBRuntime.resolve(directory)

    assert "Not executable" in str(excinfo.value)
    assert filename in str(excinfo.value)
