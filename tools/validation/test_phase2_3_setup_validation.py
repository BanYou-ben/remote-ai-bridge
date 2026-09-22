from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.domain.errors import RABError
from app.domain.profile import Profile
from app.domain.ssh_bootstrap import HostKeyInfo, HostPreparation, SSHKeyPair
from app.infrastructure.profile_store import ProfileNotFoundError, ProfileStore
from app.services.local_proxy import LocalProxyCandidate, LocalProxyDiscovery
from tools.validation.phase2_3_setup_validation import (
    VALIDATION_MARKER,
    ValidationServices,
    build_parser,
    run,
)


FINGERPRINT = "SHA256:c2VydmVyLWZpbmdlcnByaW50"
KEY_ID = "1" * 32
PUBLIC_KEY = f"ssh-ed25519 YWJjZA== remote-ai-bridge:{KEY_ID}"


def managed_profile(**changes):
    values = {
        "schema_version": 2,
        "name": "validation-server",
        "ssh_target": "tester@server.example",
        "local_proxy_port": 7897,
        "remote_port": 17890,
        "profile_type": "managed",
        "host": "server.example",
        "username": "tester",
        "ssh_port": 22,
        "key_id": KEY_ID,
        "host_key_type": "ssh-ed25519",
        "host_key_fingerprint": FINGERPRINT,
    }
    values.update(changes)
    return Profile(**values)


class FakeSetup:
    def __init__(self, store, *, unknown=False):
        self.store = store
        self.unknown = unknown
        self.prepare_calls = []
        self.setup_calls = []

    def prepare(self, host, username, port):
        self.prepare_calls.append((host, username, port))
        if self.unknown:
            raise RABError(
                "HOST_KEY_CONFIRMATION_REQUIRED",
                "confirmation required",
                details={
                    "host": host,
                    "port": port,
                    "key_type": "ssh-ed25519",
                    "fingerprint": FINGERPRINT,
                },
            )
        return HostPreparation(host, port, "ssh-ed25519", FINGERPRINT, "READY_TO_AUTH")

    def setup_managed_profile(self, name, host, username, password, **kwargs):
        self.setup_calls.append((name, host, username, password, kwargs))
        profile = managed_profile(
            name=name,
            ssh_target=f"{username}@{host}",
            host=host,
            username=username,
            ssh_port=kwargs["port"],
            local_proxy_port=kwargs.get("selected_local_proxy_port") or 7897,
            remote_port=kwargs["start_remote_port"],
            endpoint_probe_url=kwargs["endpoint_probe_url"],
        )
        self.store.save_profile(profile)
        return profile


class FakeLocalProxy:
    def __init__(self, *, multiple=False):
        self.multiple = multiple
        self.calls = []

    def discover(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if self.multiple:
            raise RABError(
                "LOCAL_PROXY_SELECTION_REQUIRED",
                "selection required",
                details={
                    "candidates": [
                        {"host": "127.0.0.1", "port": 7890},
                        {"host": "127.0.0.1", "port": 7897},
                    ]
                },
            )
        candidate = LocalProxyCandidate("127.0.0.1", 7897, True, True, True)
        return LocalProxyDiscovery(candidate, (candidate,))


class StoreProfiles:
    def __init__(self, store):
        self.store = store

    def get(self, name):
        try:
            return self.store.load_profile(name)
        except ProfileNotFoundError as exc:
            raise RABError("PROFILE_NOT_FOUND", str(exc)) from exc


class FakeHostKeys:
    def __init__(self, *, status="confirmed", remove=True):
        self.current_status = status
        self.remove_result = remove
        self.removed = []

    def status(self, info):
        return self.current_status

    def remove_if_matches(self, info):
        self.removed.append(info)
        return self.remove_result


class FakeKeys:
    def __init__(self, *, pair=None, load_error=None):
        self.pair = pair or SSHKeyPair(KEY_ID, "private", "public", PUBLIC_KEY, False)
        self.load_error = load_error
        self.loaded = []
        self.removed = []

    def load(self, key_id):
        self.loaded.append(key_id)
        if self.load_error:
            raise self.load_error
        return self.pair

    def remove_key(self, key_id, public_key):
        self.removed.append((key_id, public_key))


class Session:
    def __init__(self, info=None):
        self.host_key_info = info or HostKeyInfo(
            "server.example",
            22,
            "ssh-ed25519",
            FINGERPRINT,
            "c2VydmVyLWtleQ==",
        )
        self.closed = False

    def close(self):
        self.closed = True


class SFTP:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeBootstrap:
    def __init__(self, *, remove_result=True, info=None):
        self.remove_result = remove_result
        self.session = Session(info)
        self.sftp = SFTP()
        self.handshakes = []
        self.auth = []
        self.removed = []

    def handshake(self, host, port):
        self.handshakes.append((host, port))
        return self.session

    def authenticate_password(self, session, username, password):
        self.auth.append((username, password))

    def open_sftp(self, session):
        return self.sftp

    def remove_public_key(self, sftp, public_key):
        self.removed.append(public_key)
        return self.remove_result


def make_services(root, *, setup=None, local=None, hosts=None, keys=None, bootstrap=None):
    store = ProfileStore(root)
    return ValidationServices(
        setup or FakeSetup(store),
        local or FakeLocalProxy(),
        store,
        StoreProfiles(store),
        hosts or FakeHostKeys(),
        keys or FakeKeys(),
        bootstrap or FakeBootstrap(),
    )


def factory_for(**overrides):
    observed = []

    def factory(root):
        observed.append(root)
        return make_services(root, **overrides)

    return factory, observed


def state_args(command, root):
    return [command, "--state-root", str(root)]


def target_args(command, root):
    return state_args(command, root) + [
        "--host", "server.example", "--username", "tester", "--port", "22"
    ]


def setup_args(root):
    return target_args("setup", root) + [
        "--name", "validation-server", "--confirm-fingerprint", FINGERPRINT
    ]


def cleanup_args(root, **changes):
    values = {
        "name": "validation-server",
        "host": "server.example",
        "username": "tester",
        "port": "22",
    }
    values.update(changes)
    return state_args("cleanup", root) + [
        "--name", values["name"],
        "--host", values["host"],
        "--username", values["username"],
        "--port", values["port"],
    ]


def marked_root(tmp_path):
    root = tmp_path / "phase2-3-validation"
    root.mkdir()
    (root / VALIDATION_MARKER).touch()
    return root


def fail_password(prompt):
    raise AssertionError("password must not be read")


def test_wrapper_has_no_password_argv_option(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(setup_args(tmp_path) + ["--password", "secret"])


def test_prepare_displays_fingerprint_without_reading_password(tmp_path, capsys):
    root = tmp_path / "validation"
    factory, _ = factory_for()
    services = None

    def unknown_factory(value):
        nonlocal services
        services = make_services(value)
        services.setup.unknown = True
        return services

    result = run(target_args("prepare", root), password_reader=fail_password, service_factory=unknown_factory)

    output = capsys.readouterr().out
    assert result == 3
    assert "HOST_KEY_CONFIRMATION_REQUIRED" in output
    assert FINGERPRINT in output
    assert services.setup.prepare_calls == [("server.example", "tester", 22)]


def test_discover_uses_local_proxy_only_and_does_not_read_password(tmp_path, capsys):
    root = tmp_path / "validation"
    local = FakeLocalProxy()
    services = None

    def factory(value):
        nonlocal services
        services = make_services(value, local=local)
        return services

    result = run(
        state_args("discover", root) + ["--candidate-port", "7897"],
        password_reader=fail_password,
        service_factory=factory,
    )

    assert result == 0
    assert local.calls[0][1]["candidate_ports"] == (7897,)
    assert services.setup.prepare_calls == []
    assert services.setup.setup_calls == []
    assert services.bootstrap.handshakes == []
    assert "LOCAL_PROXY_SELECTED" in capsys.readouterr().out


def test_discover_multiple_proxy_returns_selection_required(tmp_path, capsys):
    local = FakeLocalProxy(multiple=True)
    factory, _ = factory_for(local=local)

    result = run(
        state_args("discover", tmp_path / "validation"),
        password_reader=fail_password,
        service_factory=factory,
    )

    output = capsys.readouterr().out
    assert result == 3
    assert "LOCAL_PROXY_SELECTION_REQUIRED" in output
    assert "7890" in output and "7897" in output


def test_setup_uses_getpass_value_without_argv_or_secret_output(tmp_path, capsys):
    root = tmp_path / "validation"
    secret = "validation-password-must-not-appear"
    created = None

    def factory(value):
        nonlocal created
        created = make_services(value)
        return created

    argv = setup_args(root) + ["--selected-local-proxy-port", "7897"]
    result = run(argv, password_reader=lambda prompt: secret, service_factory=factory)

    captured = capsys.readouterr()
    assert result == 0
    assert secret not in repr(argv)
    assert secret not in captured.out
    assert secret not in captured.err
    assert "private_key" not in captured.out
    assert created.setup.setup_calls[0][3] == secret


def test_setup_reloads_identical_profile_from_real_store(tmp_path):
    root = tmp_path / "validation"
    services = None

    def factory(value):
        nonlocal services
        services = make_services(value)
        return services

    assert run(setup_args(root), password_reader=lambda prompt: "secret", service_factory=factory) == 0

    saved = services.store.load_profile("validation-server")
    assert saved == managed_profile()


def test_inspect_reads_store_without_password_or_network(tmp_path, capsys):
    root = marked_root(tmp_path)
    services = make_services(root)
    services.store.save_profile(managed_profile())

    result = run(
        state_args("inspect", root) + ["--name", "validation-server"],
        password_reader=fail_password,
        service_factory=lambda value: services,
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "PROFILE_LOADED" in output
    assert KEY_ID in output
    assert services.bootstrap.handshakes == []


def test_normal_state_root_is_rejected(monkeypatch, tmp_path, capsys):
    normal = tmp_path / "normal"
    monkeypatch.setattr(
        "tools.validation.phase2_3_setup_validation.default_state_root",
        lambda: normal,
    )

    result = run(state_args("discover", normal), service_factory=lambda root: pytest.fail("no services"))

    assert result == 2
    assert "VALIDATION_STATE_ROOT_UNSAFE" in capsys.readouterr().err


def test_nonempty_unmarked_state_root_is_rejected(tmp_path, capsys):
    root = tmp_path / "unsafe"
    root.mkdir()
    (root / "unrelated.txt").write_text("keep", encoding="utf-8")

    result = run(state_args("discover", root), service_factory=lambda value: pytest.fail("no services"))

    assert result == 2
    assert (root / "unrelated.txt").read_text(encoding="utf-8") == "keep"
    assert "VALIDATION_STATE_ROOT_UNSAFE" in capsys.readouterr().err


def test_cleanup_profile_identity_mismatch_preserves_all_evidence(tmp_path, capsys):
    root = marked_root(tmp_path)
    services = make_services(root)
    services.store.save_profile(managed_profile())

    result = run(
        cleanup_args(root, host="other.example"),
        password_reader=fail_password,
        service_factory=lambda value: services,
    )

    assert result == 2
    assert services.store.load_profile("validation-server") == managed_profile()
    assert services.ssh_keys.loaded == []
    assert services.bootstrap.handshakes == []
    assert "VALIDATION_PROFILE_IDENTITY_MISMATCH" in capsys.readouterr().err


def test_cleanup_missing_key_evidence_stops_before_remote_or_local_delete(tmp_path):
    root = marked_root(tmp_path)
    keys = FakeKeys(load_error=RABError("SSH_KEY_NOT_FOUND", "missing key"))
    services = make_services(root, keys=keys)
    services.store.save_profile(managed_profile())

    result = run(
        cleanup_args(root),
        password_reader=fail_password,
        service_factory=lambda value: services,
    )

    assert result == 2
    assert services.store.load_profile("validation-server") == managed_profile()
    assert keys.removed == []
    assert services.bootstrap.handshakes == []


def test_cleanup_missing_remote_exact_key_preserves_local_evidence(tmp_path, capsys):
    root = marked_root(tmp_path)
    bootstrap = FakeBootstrap(remove_result=False)
    services = make_services(root, bootstrap=bootstrap)
    services.store.save_profile(managed_profile())

    result = run(
        cleanup_args(root),
        password_reader=lambda prompt: "secret",
        service_factory=lambda value: services,
    )

    assert result == 2
    assert services.store.load_profile("validation-server") == managed_profile()
    assert services.ssh_keys.removed == []
    assert services.host_keys.removed == []
    assert "VALIDATION_PUBLIC_KEY_NOT_FOUND" in capsys.readouterr().err


def test_cleanup_success_removes_exact_owned_resources_and_preserves_unrelated_file(tmp_path):
    root = marked_root(tmp_path)
    unrelated = root / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    services = make_services(root)
    services.store.save_profile(managed_profile())
    runtime_dir = root / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "validation-server.lock").write_bytes(b"0")

    result = run(
        cleanup_args(root),
        password_reader=lambda prompt: "secret",
        service_factory=lambda value: services,
    )

    assert result == 0
    assert services.bootstrap.removed == [PUBLIC_KEY]
    assert services.ssh_keys.removed == [(KEY_ID, PUBLIC_KEY)]
    assert len(services.host_keys.removed) == 1
    with pytest.raises(ProfileNotFoundError):
        services.store.load_profile("validation-server")
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert (root / VALIDATION_MARKER).is_file()


def test_wrapper_composes_production_modules_without_reimplementing_protocols():
    source = Path(__file__).with_name("phase2_3_setup_validation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint({"paramiko", "socket", "subprocess"})
    for required in (
        "HostKeyStore", "SSHKeyStore", "SSHBootstrapAdapter", "SetupService",
        "LocalProxyService", "ProcessRunner", "RemoteProbeService", "RemotePortSelector",
        "ProfileStore", "ProfileService", "TunnelManager", "locate_ssh",
    ):
        assert required in source
