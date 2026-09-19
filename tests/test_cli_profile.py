import argparse
import json
from unittest.mock import MagicMock

from app import cli
from app.domain.health import CheckResult, CheckStatus
from app.infrastructure.profile_store import ProfileStore
from app.services.local_proxy import LocalProxyReport
from app.services.profile_service import ProfileService


def passed(name):
    return CheckResult(name, CheckStatus.PASS, "ok")


def services(tmp_path):
    store = ProfileStore(tmp_path)
    tunnel = MagicMock()
    profiles = ProfileService(store, tunnel)
    local = MagicMock()
    local.inspect.return_value = LocalProxyReport(
        passed("TCP reachable"),
        passed("HTTP proxy handshake"),
        passed("AI endpoint probe"),
    )
    return profiles, local


def test_existing_profile_add_and_list_use_profile_service(tmp_path, capsys):
    profiles, local = services(tmp_path)
    args = argparse.Namespace(
        name="server",
        ssh_target="user@server",
        local_proxy_port=7897,
        remote_port=17890,
        endpoint_probe_url="https://api.openai.com/v1/models",
        auto_reconnect=True,
    )

    assert cli.command_profile_add(args, profiles, local) == 0
    assert cli.command_profile_list(profiles) == 0

    output = capsys.readouterr().out
    assert "Saved profile 'server'" in output
    assert "user@server" in output
    assert profiles.get("server").local_proxy_port == 7897


def test_profile_get_outputs_current_profile_as_json(tmp_path, capsys):
    profiles, local = services(tmp_path)
    args = argparse.Namespace(
        name="server",
        ssh_target="server",
        local_proxy_port=7897,
        remote_port=17890,
        endpoint_probe_url="https://api.openai.com/v1/models",
        auto_reconnect=True,
    )
    cli.command_profile_add(args, profiles, local)
    capsys.readouterr()

    assert cli.command_profile_get("server", profiles) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "server"
    assert payload["schema_version"] == 1


def test_profile_update_and_delete_commands_delegate_to_service(tmp_path, capsys):
    profiles, local = services(tmp_path)
    add_args = argparse.Namespace(
        name="server",
        ssh_target="server",
        local_proxy_port=7897,
        remote_port=17890,
        endpoint_probe_url="https://api.openai.com/v1/models",
        auto_reconnect=True,
    )
    cli.command_profile_add(add_args, profiles, local)
    update_args = argparse.Namespace(
        name="server",
        ssh_target=None,
        local_proxy_port=7890,
        remote_port=None,
        endpoint_probe_url=None,
        auto_reconnect=False,
    )

    assert cli.command_profile_update(update_args, profiles) == 0
    assert profiles.get("server").local_proxy_port == 7890
    assert profiles.get("server").auto_reconnect is False
    assert cli.command_profile_delete("server", profiles) == 0
    assert profiles.list() == []

    output = capsys.readouterr().out
    assert "Updated profile 'server'" in output
    assert "Deleted profile 'server'" in output


def test_profile_parser_keeps_existing_commands_and_adds_management_commands():
    parser = cli.build_parser()

    assert parser.parse_args(["profile", "add", "server"]).profile_command == "add"
    assert parser.parse_args(["profile", "list"]).profile_command == "list"
    assert parser.parse_args(["profile", "get", "server"]).profile_command == "get"
    assert parser.parse_args(["profile", "update", "server", "--remote-port", "17891"]).remote_port == 17891
    assert parser.parse_args(["profile", "delete", "server"]).profile_command == "delete"
