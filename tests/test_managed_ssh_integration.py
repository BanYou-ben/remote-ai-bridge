from app.domain.profile import Profile
from app.infrastructure.process_runner import ProcessResult
from app.infrastructure.ssh_key_store import managed_ssh_options
from app.services.remote_probe import RemoteProbeService
from app.services.tunnel import build_tunnel_command


TUNNEL_ID = "11111111-1111-4111-8111-111111111111"


def managed_profile():
    return Profile(
        schema_version=2,
        name="managed-server",
        ssh_target="tester@server.example",
        profile_type="managed",
        host="server.example",
        username="tester",
        ssh_port=2222,
        key_id="1" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:YWJjZGVmZ2hpamtsbW5vcHFyc3Q",
    )


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append((argv, timeout))
        return ProcessResult(tuple(argv), 0, "", "")


def test_managed_options_are_derived_from_state_root_and_key_id(tmp_path):
    options = managed_ssh_options(managed_profile(), tmp_path)

    assert "IdentitiesOnly=yes" in options
    assert f"UserKnownHostsFile={tmp_path / 'ssh' / 'known_hosts'}" in options
    assert str(tmp_path / "ssh" / "keys" / ("id_ed25519_" + "1" * 32)) in options
    assert options[options.index("-p") + 1] == "2222"


def test_managed_tunnel_uses_rab_key_and_independent_known_hosts(tmp_path):
    command = build_tunnel_command("ssh.exe", managed_profile(), TUNNEL_ID, tmp_path)

    assert "BatchMode=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "IdentitiesOnly=yes" in command
    assert f"UserKnownHostsFile={tmp_path / 'ssh' / 'known_hosts'}" in command
    assert command[-2] == "tester@server.example"


def test_managed_remote_probe_uses_same_managed_credentials(tmp_path):
    runner = RecordingRunner()
    service = RemoteProbeService(runner, "ssh.exe", state_root=tmp_path)

    service.check_listener(managed_profile())

    argv, _ = runner.calls[0]
    assert "IdentitiesOnly=yes" in argv
    assert f"UserKnownHostsFile={tmp_path / 'ssh' / 'known_hosts'}" in argv
    assert "tester@server.example" in argv


def test_legacy_profile_keeps_original_ssh_configuration_behavior(tmp_path):
    legacy = Profile(1, "legacy", "ssh-alias")

    assert managed_ssh_options(legacy, tmp_path) == []
    command = build_tunnel_command("ssh.exe", legacy, TUNNEL_ID, tmp_path)
    assert "-F" not in command
    assert "-i" not in command
    assert command[-2] == "ssh-alias"
