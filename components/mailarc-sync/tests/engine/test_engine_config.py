"""`SyncConfig` is nine settings; the prefix is the part that can silently rot.

A typo in `env_prefix` costs nothing at import time and everything at run time
— the setting is simply never read and the default quietly wins. The defaults
themselves are checked because three of them are the spec's own figures and a
drift there would be invisible: too many fetch slots earns a rate limit, too
rare a checkpoint loses work a crash should not have cost.
"""

import os
import socket

from mailarc_sync.engine.config import (
    WORKER_ID_LENGTH,
    SyncConfig,
    default_worker_id,
)


def test_the_defaults_are_the_ones_the_spec_names() -> None:
    config = SyncConfig()

    assert config.fetch_concurrency == 8, "§7.3's semaphore"
    assert config.checkpoint_every == 200, "§7.3's checkpoint interval"
    assert config.heartbeat_interval == 10.0, "§7.2's heartbeat"


def test_a_lease_outlives_several_missed_heartbeats() -> None:
    """A worker that is merely slow must not have its job stolen."""
    config = SyncConfig()

    assert config.lease_seconds > 3 * config.heartbeat_interval


class TestTheScheduleIsOffUntilSomebodyTurnsItOn:
    """The one default that is a promise to the user rather than a tuning knob.

    A fresh install must not start talking to two real mailboxes on its own,
    and nothing else in the tree would notice if it did: a schedule that swept
    every fifteen minutes out of the box would leave every test green.
    """

    def test_it_is_zero_out_of_the_box(self) -> None:
        """Read off the field rather than off an instance, so an environment
        that happens to have the variable set cannot make this pass."""
        assert SyncConfig.model_fields["incremental_interval"].default == 0.0

    def test_zero_is_what_the_schedule_reads_as_off(self) -> None:
        assert SyncConfig().incremental_interval <= 0.0

    def test_the_environment_can_turn_it_on(self, monkeypatch) -> None:
        monkeypatch.setenv("app_sync_incremental_interval", "900")

        assert SyncConfig().incremental_interval == 900.0


def test_the_application_supervises_the_worker_unless_told_otherwise() -> None:
    """True for the desktop app; Docker and systemd turn it off."""
    assert SyncConfig().supervise_worker is True


def test_the_environment_prefix_is_app_sync(monkeypatch) -> None:
    monkeypatch.setenv("app_sync_batch_size", "7")
    monkeypatch.setenv("app_sync_supervise_worker", "false")

    config = SyncConfig()

    assert config.batch_size == 7
    assert config.supervise_worker is False


def test_an_explicit_value_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("app_sync_fetch_concurrency", "3")

    assert SyncConfig(fetch_concurrency=2).fetch_concurrency == 2


class TestWorkerIdentity:
    def test_it_names_the_process_a_lease_belongs_to(self) -> None:
        assert str(os.getpid()) in default_worker_id()

    def test_it_fits_the_column_it_is_written_to(self, monkeypatch) -> None:
        """`mail_sync_jobs.worker_id` is 64 characters; hostnames can be longer."""
        monkeypatch.setattr(socket, "gethostname", lambda: "h" * 200)

        assert len(default_worker_id()) == WORKER_ID_LENGTH
        assert default_worker_id().startswith(str(os.getpid()))

    def test_two_workers_on_one_machine_are_told_apart(self) -> None:
        """Two ids that collide would let each worker steal the other's jobs."""
        assert SyncConfig().worker_id == default_worker_id()

    def test_it_can_be_named_explicitly(self, monkeypatch) -> None:
        monkeypatch.setenv("app_sync_worker_id", "importer-1")

        assert SyncConfig().worker_id == "importer-1"
