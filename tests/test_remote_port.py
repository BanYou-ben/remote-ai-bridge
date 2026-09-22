from unittest.mock import MagicMock

import pytest

from app.domain.errors import RABError
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.remote_port import RemotePortSelector
from app.services.remote_probe import RemoteProbeService


def managed_profile(**changes):
    values = {
        "schema_version": 2,
        "name": "managed-server",
        "ssh_target": "tester@server.example",
        "local_proxy_port": 7897,
        "remote_port": 17890,
        "profile_type": "managed",
        "host": "server.example",
        "username": "tester",
        "ssh_port": 22,
        "key_id": "1" * 32,
        "host_key_type": "ssh-ed25519",
        "host_key_fingerprint": "SHA256:c2VydmVyLWZpbmdlcnByaW50",
    }
    values.update(changes)
    return Profile(**values)


def absent(port):
    return CheckResult("Remote listener", CheckStatus.FAIL, f"no listener on 127.0.0.1:{port}", "LISTENER_ABSENT")


def occupied(port):
    return CheckResult("Remote listener", CheckStatus.PASS, f"127.0.0.1:{port} is listening")


def test_remote_port_selector_returns_start_port_when_free():
    probe = MagicMock(spec=RemoteProbeService)
    probe.check_listener.return_value = absent(17890)

    selected = RemotePortSelector(probe).select(managed_profile())

    assert selected == 17890
    probe.check_listener.assert_called_once_with(managed_profile(), 17890)


def test_remote_port_selector_skips_occupied_port():
    probe = MagicMock(spec=RemoteProbeService)
    probe.check_listener.side_effect = [occupied(17890), absent(17891)]

    selected = RemotePortSelector(probe).select(managed_profile(), max_attempts=2)

    assert selected == 17891
    assert [call.args[1] for call in probe.check_listener.call_args_list] == [17890, 17891]


def test_remote_port_selector_skips_multiple_occupied_ports_with_bounded_scan():
    probe = MagicMock(spec=RemoteProbeService)
    probe.check_listener.side_effect = [occupied(17890), occupied(17891), absent(17892)]

    selected = RemotePortSelector(probe).select(managed_profile(), max_attempts=3)

    assert selected == 17892
    assert probe.check_listener.call_count == 3


def test_remote_port_selector_reports_range_exhaustion_without_process_data():
    probe = MagicMock(spec=RemoteProbeService)
    probe.check_listener.side_effect = [occupied(17890), occupied(17891)]

    with pytest.raises(RABError) as raised:
        RemotePortSelector(probe).select(managed_profile(), max_attempts=2)

    assert raised.value.code == "REMOTE_PORT_RANGE_EXHAUSTED"
    assert raised.value.details == {"start_port": 17890, "max_attempts": 2, "attempt_count": 2}


def test_remote_port_selector_never_terminates_unknown_owner():
    probe = MagicMock(spec=RemoteProbeService)
    probe.check_listener.side_effect = [occupied(17890), absent(17891)]

    assert RemotePortSelector(probe).select(managed_profile(), max_attempts=2) == 17891

    probe.terminate_verified_stale_session.assert_not_called()
    probe.verify_stale_session_ownership.assert_not_called()


def test_remote_port_transport_error_is_not_treated_as_occupied():
    probe = MagicMock(spec=RemoteProbeService)
    probe.check_listener.return_value = CheckResult(
        "Remote listener", CheckStatus.FAIL, "transport failed", "REMOTE_CHECK_FAILED"
    )

    with pytest.raises(RABError) as raised:
        RemotePortSelector(probe).select(managed_profile(), max_attempts=5)

    assert raised.value.code == "REMOTE_PORT_CHECK_FAILED"
    assert probe.check_listener.call_count == 1


def test_remote_port_selector_requires_managed_loopback_profile():
    probe = MagicMock(spec=RemoteProbeService)
    selector = RemotePortSelector(probe)

    with pytest.raises(RABError) as legacy:
        selector.select(Profile(1, "legacy", "server"))
    with pytest.raises(RABError) as unsafe:
        selector.select(managed_profile(remote_bind_host="0.0.0.0"))

    assert legacy.value.code == "REMOTE_PORT_SELECTION_INVALID"
    assert unsafe.value.code == "UNSAFE_REMOTE_BINDING"
    probe.check_listener.assert_not_called()
