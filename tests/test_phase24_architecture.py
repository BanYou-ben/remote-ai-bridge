from __future__ import annotations

import ast
import inspect
import json
from unittest.mock import MagicMock

import pytest

from app import cli
from app.domain.supervisor import SupervisorSnapshot, SupervisorState, utc_now
from app.infrastructure.profile_store import ProfileStore
from app.services.runtime_manager import RuntimeManager
from app.services.tunnel_supervisor import SupervisorPolicy, TunnelSupervisor
from .phase24_helpers import FakeProfiles


def test_snapshot_is_immutable_serializable_and_redacts_message():
    snapshot = SupervisorSnapshot(
        "server",
        SupervisorState.FAILED,
        utc_now(),
        message="Authorization: Bearer secret-value",
    )

    assert "secret-value" not in snapshot.message
    assert json.loads(json.dumps(snapshot.to_dict()))["state"] == "FAILED"
    with pytest.raises(Exception):
        snapshot.state = SupervisorState.READY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"backoff_seconds": ()},
        {"backoff_seconds": (-1,)},
        {"health_interval_seconds": -1},
        {"verify_timeout_seconds": -1},
    ],
)
def test_supervisor_policy_rejects_invalid_intervals(kwargs):
    with pytest.raises(ValueError):
        SupervisorPolicy(**kwargs)


def test_default_backoff_policy_preserves_phase1_sequence():
    assert SupervisorPolicy().backoff_seconds == (1, 2, 5, 10, 30)


def test_service_layer_contains_no_print_or_input_calls():
    for service in (TunnelSupervisor, RuntimeManager):
        tree = ast.parse(inspect.getsource(service))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "print" not in calls
        assert "input" not in calls


def test_cli_connect_contains_no_tunnel_or_proxy_state_machine_calls():
    source = inspect.getsource(cli.command_connect)

    assert ".acquire(" not in source
    assert ".verify(" not in source
    assert ".inspect(" not in source
    assert "BACKOFF" not in source
    assert "save_profile" not in source
    assert "input(" not in source


def test_runtime_status_performs_no_proxy_or_remote_network_calls(tmp_path):
    profiles = FakeProfiles("server")
    store = ProfileStore(tmp_path)
    local = MagicMock()
    tunnel = MagicMock()
    manager = RuntimeManager(profiles, store, local, tunnel)

    snapshot = manager.status("server")

    assert snapshot.state is SupervisorState.UNSUPERVISED
    local.assert_not_called()
    tunnel.remote_probe.assert_not_called()


def test_runtime_manager_uses_validated_profile_service_snapshot(tmp_path):
    profiles = FakeProfiles("server")
    profiles.get = MagicMock(wraps=profiles.get)
    store = ProfileStore(tmp_path)
    supervisor = MagicMock()
    supervisor.snapshot.return_value = SupervisorSnapshot("server", SupervisorState.STARTING, utc_now())
    supervisor.run.return_value = supervisor.snapshot.return_value
    manager = RuntimeManager(
        profiles,
        store,
        MagicMock(),
        MagicMock(),
        supervisor_factory=lambda name: supervisor,
    )

    manager.start("server")
    manager.wait("server", 0.5)

    profiles.get.assert_called_once_with("server")
    supervisor.run.assert_called_once()


def test_runtime_state_schema_is_not_replaced_by_supervisor_snapshot():
    fields = SupervisorSnapshot.__dataclass_fields__

    assert "process_creation_time" not in fields
    assert "remote_ssh_connection" not in fields
    assert "state" in fields
