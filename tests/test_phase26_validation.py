from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.domain.errors import RABError
from app.domain.profile import Profile, RuntimeState
from app.services.local_proxy import LocalProxyCandidate, LocalProxyDiscovery
from tools.validation import phase2_6_integration_validation as validation


SECRET = "RAB-P26-SECRET-DO-NOT-LEAK"


def managed_profile() -> Profile:
    return Profile(
        schema_version=2,
        name="validation-profile",
        ssh_target="alice@example.test",
        local_proxy_port=7897,
        remote_port=17890,
        profile_type="managed",
        host="example.test",
        username="alice",
        ssh_port=22,
        key_id="a" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )


def marked_root(tmp_path: Path) -> Path:
    root = tmp_path / "validation"
    root.mkdir()
    (root / validation.VALIDATION_MARKER).touch()
    return root


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, path):
        return FakeResponse(self.responses[("GET", path)])

    def post(self, path):
        return FakeResponse(self.responses[("POST", path)])


def client_factory(responses):
    return lambda **_kwargs: FakeClient(responses)


def runtime_state() -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        profile_name="validation-profile",
        pid=123,
        process_creation_time=100.0,
        executable_path="C:\\Windows\\System32\\OpenSSH\\ssh.exe",
        tunnel_id="validation-tunnel",
        supervisor_pid=456,
        remote_port=17890,
        started_at="2026-01-01T00:00:00+00:00",
    )


def runtime_snapshot(*, state="READY", runtime_present=True):
    return {
        "profile_name": "validation-profile",
        "state": state,
        "pid": 123 if runtime_present else None,
        "tunnel_id": "validation-tunnel" if runtime_present else None,
        "remote_port": 17890 if runtime_present else None,
        "runtime_present": runtime_present,
    }


def test_validation_parser_exposes_observable_subcommands():
    parser = validation.build_parser()
    choices = parser._subparsers._group_actions[0].choices

    assert set(choices) == {
        "prepare",
        "setup",
        "serve-check",
        "runtime-check",
        "doctor-check",
        "cleanup",
    }


def test_validation_parser_has_no_password_argument(tmp_path):
    with pytest.raises(SystemExit):
        validation.build_parser().parse_args(
            [
                "setup",
                "--state-root",
                str(tmp_path),
                "--name",
                "validation-profile",
                "--host",
                "example.test",
                "--username",
                "alice",
                "--confirm-fingerprint",
                "SHA256:" + "A" * 43,
                "--password",
                SECRET,
            ]
        )


def test_validation_root_requires_marker_for_existing_state(tmp_path):
    root = tmp_path / "unmarked"
    root.mkdir()

    with pytest.raises(RABError) as caught:
        validation._validation_root(root, create=False)

    assert caught.value.code == "VALIDATION_STATE_ROOT_UNSAFE"


def test_validation_root_creation_adds_marker(tmp_path):
    root = tmp_path / "new-validation"

    resolved = validation._validation_root(root, create=True)

    assert resolved == root.resolve()
    assert (resolved / validation.VALIDATION_MARKER).is_file()


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8000",
        "http://192.168.1.20:8000",
        "https://127.0.0.1:8000",
        "http://user:secret@127.0.0.1:8000",
        "http://127.0.0.1:8000/unexpected",
    ],
)
def test_validation_api_url_rejects_non_loopback_or_non_origin(url):
    with pytest.raises(RABError) as caught:
        validation._validated_api_url(url)

    assert caught.value.code == "VALIDATION_API_URL_UNSAFE"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"])
def test_validation_api_url_allows_loopback_origins(url):
    assert validation._validated_api_url(url) == url


def test_setup_reads_password_locally_and_never_prints_it(tmp_path, capsys):
    root = marked_root(tmp_path)
    services = SimpleNamespace(setup=MagicMock())
    services.setup.setup_managed_profile.return_value = managed_profile()

    result = validation.run(
        [
            "setup",
            "--state-root",
            str(root),
            "--name",
            "validation-profile",
            "--host",
            "example.test",
            "--username",
            "alice",
            "--confirm-fingerprint",
            "SHA256:" + "A" * 43,
            "--candidate-port",
            "7897",
            "--selected-local-proxy-port",
            "7897",
        ],
        password_reader=lambda _prompt: SECRET,
        service_factory=lambda _root: services,
    )

    assert result == 0
    assert services.setup.setup_managed_profile.call_args.args[3] == SECRET
    assert SECRET not in capsys.readouterr().out


def test_prepare_never_requests_password(tmp_path):
    root = tmp_path / "prepare-root"
    candidate = LocalProxyCandidate("127.0.0.1", 7897, True, True, True)
    services = SimpleNamespace(local_proxy=MagicMock(), setup=MagicMock())
    services.local_proxy.discover.return_value = LocalProxyDiscovery(candidate, (candidate,))
    services.setup.prepare.side_effect = RABError(
        "HOST_KEY_CONFIRMATION_REQUIRED",
        "confirm fingerprint",
        details={
            "host": "example.test",
            "port": 22,
            "key_type": "ssh-ed25519",
            "fingerprint": "SHA256:" + "A" * 43,
        },
    )
    password_reader = MagicMock()

    result = validation.run(
        [
            "prepare",
            "--state-root",
            str(root),
            "--host",
            "example.test",
            "--username",
            "alice",
            "--candidate-port",
            "7897",
            "--selected-local-proxy-port",
            "7897",
        ],
        password_reader=password_reader,
        service_factory=lambda _root: services,
    )

    assert result == 3
    password_reader.assert_not_called()


def test_cleanup_source_is_exact_ownership_scoped_and_has_no_blind_commands():
    source = inspect.getsource(validation._cleanup)
    module_source = Path(validation.__file__).read_text(encoding="utf-8")

    assert "remove_public_key" in source
    assert "pair.public_key" in source
    assert "remove_key" in source
    assert "profile.key_id" in source
    for forbidden in ("pkill", "killall", "taskkill", "sudo", "iptables", "shell=True"):
        assert forbidden not in module_source


def test_serve_check_matches_complete_profile_identity_from_requested_root(tmp_path):
    root = marked_root(tmp_path)
    profile = managed_profile()
    services = SimpleNamespace(profiles=MagicMock())
    services.profiles.get.return_value = profile
    responses = {
        ("GET", "/health"): {"status": "ok"},
        ("GET", "/profiles/validation-profile"): profile.to_dict(),
    }

    result = validation.run(
        ["serve-check", "--state-root", str(root), "--name", profile.name],
        service_factory=lambda selected_root: services if selected_root == root.resolve() else None,
        client_factory=client_factory(responses),
    )

    assert result == 0
    services.profiles.get.assert_called_once_with(profile.name)


def test_serve_check_rejects_same_name_profile_from_different_state_root(tmp_path, capsys):
    root = marked_root(tmp_path)
    expected = managed_profile()
    actual = expected.to_dict()
    actual["key_id"] = "b" * 32
    services = SimpleNamespace(profiles=MagicMock())
    services.profiles.get.return_value = expected
    responses = {
        ("GET", "/health"): {"status": "ok"},
        ("GET", "/profiles/validation-profile"): actual,
    }

    result = validation.run(
        ["serve-check", "--state-root", str(root), "--name", expected.name],
        service_factory=lambda _root: services,
        client_factory=client_factory(responses),
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "API_STATE_ROOT_MISMATCH"


def test_runtime_connect_rejects_ready_without_state_in_requested_root(tmp_path, capsys):
    root = marked_root(tmp_path)
    store = MagicMock()
    store.load_runtime.return_value = None
    responses = {
        ("POST", "/runtime/validation-profile/connect"): runtime_snapshot(),
    }

    result = validation.run(
        [
            "runtime-check",
            "--state-root",
            str(root),
            "--name",
            "validation-profile",
            "--action",
            "connect",
        ],
        service_factory=lambda _root: SimpleNamespace(store=store),
        client_factory=client_factory(responses),
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "RUNTIME_STATE_ROOT_MISMATCH"


def test_runtime_connect_rejects_runtime_identity_mismatch(tmp_path, capsys):
    root = marked_root(tmp_path)
    mismatched = runtime_state()
    snapshot = runtime_snapshot()
    snapshot["tunnel_id"] = "other-tunnel"
    store = MagicMock()
    store.load_runtime.return_value = mismatched
    responses = {
        ("POST", "/runtime/validation-profile/connect"): snapshot,
    }

    result = validation.run(
        [
            "runtime-check",
            "--state-root",
            str(root),
            "--name",
            "validation-profile",
            "--action",
            "connect",
        ],
        service_factory=lambda _root: SimpleNamespace(store=store),
        client_factory=client_factory(responses),
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "RUNTIME_STATE_ROOT_MISMATCH"


@pytest.mark.parametrize(
    ("persisted_runtime", "expected_result", "expected_status"),
    [
        (None, 0, "RUNTIME_CHECK_COMPLETE"),
        (runtime_state(), 2, "RUNTIME_EVIDENCE_REMAINS"),
    ],
)
def test_runtime_disconnect_uses_store_persistence_abstraction(
    tmp_path, capsys, persisted_runtime, expected_result, expected_status
):
    root = marked_root(tmp_path)
    store = MagicMock()
    store.load_runtime.return_value = persisted_runtime
    responses = {
        ("POST", "/runtime/validation-profile/disconnect"): runtime_snapshot(
            state="STOPPED", runtime_present=False
        ),
    }

    result = validation.run(
        [
            "runtime-check",
            "--state-root",
            str(root),
            "--name",
            "validation-profile",
            "--action",
            "disconnect",
        ],
        service_factory=lambda _root: SimpleNamespace(store=store),
        client_factory=client_factory(responses),
    )

    assert result == expected_result
    assert json.loads(capsys.readouterr().out)["status"] == expected_status
    store.load_runtime.assert_called_once_with("validation-profile")


@pytest.mark.parametrize(
    ("snapshot", "persisted_runtime", "expected_result"),
    [
        (runtime_snapshot(), runtime_state(), 0),
        (runtime_snapshot(state="STOPPED", runtime_present=False), None, 0),
        (runtime_snapshot(state="UNSUPERVISED", runtime_present=False), runtime_state(), 2),
    ],
)
def test_runtime_status_checks_persistence_consistency(
    tmp_path, snapshot, persisted_runtime, expected_result
):
    root = marked_root(tmp_path)
    store = MagicMock()
    store.load_runtime.return_value = persisted_runtime
    responses = {
        ("GET", "/runtime/validation-profile"): snapshot,
    }

    result = validation.run(
        [
            "runtime-check",
            "--state-root",
            str(root),
            "--name",
            "validation-profile",
            "--action",
            "status",
        ],
        service_factory=lambda _root: SimpleNamespace(store=store),
        client_factory=client_factory(responses),
    )

    assert result == expected_result
