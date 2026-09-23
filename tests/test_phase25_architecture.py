from __future__ import annotations

import ast
from pathlib import Path

from app import cli
from app.api import app as api_app_module
from app.api import routes
from app.bootstrap import create_services


def test_cli_and_api_share_composition_root():
    cli_source = Path(cli.__file__).read_text(encoding="utf-8")
    api_source = Path(api_app_module.__file__).read_text(encoding="utf-8")

    assert "from app.bootstrap import create_services" in cli_source
    assert "from app.bootstrap import AppServices, create_services" in api_source
    assert "def make_services(" not in cli_source
    assert callable(create_services)


def test_composition_root_wires_one_shared_service_graph(tmp_path, monkeypatch):
    monkeypatch.setattr("app.bootstrap.locate_ssh", lambda: "ssh")

    services = create_services(tmp_path)

    assert services.profiles.store is services.store
    assert services.profiles.tunnel is services.tunnel_manager
    assert services.tunnel_manager.store is services.store
    assert services.tunnel_manager.remote_probe is services.remote_probe
    assert services.doctor.local_proxy is services.local_proxy
    assert services.runtime_manager.profiles is services.profiles
    assert services.runtime_manager.store is services.store
    assert services.runtime_manager.local_proxy is services.local_proxy
    assert services.runtime_manager.tunnel_manager is services.tunnel_manager


def test_api_routes_do_not_implement_tunnel_or_process_operations():
    source = Path(routes.__file__).read_text(encoding="utf-8")
    forbidden = (
        "subprocess",
        "TunnelManager",
        ".disconnect(",
        "ProcessInspector",
        "terminate_owned",
        "shell=True",
        "ssh.exe",
        "StrictHostKeyChecking",
        "while ",
        "sleep(",
        "backoff",
    )

    for marker in forbidden:
        assert marker not in source


def test_api_package_does_not_import_cli_or_construct_ssh_commands():
    api_root = Path(routes.__file__).parent
    combined = "\n".join(path.read_text(encoding="utf-8") for path in api_root.glob("*.py"))

    assert "app.cli" not in combined
    assert "-R" not in combined
    assert "BatchMode" not in combined
    assert "RemoteProbeService" not in combined


def test_runtime_routes_are_plain_sync_functions():
    tree = ast.parse(Path(routes.__file__).read_text(encoding="utf-8"))
    route_names = {"list_runtime", "runtime_status", "connect_runtime", "disconnect_runtime"}
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert route_names <= functions.keys()
    assert all(isinstance(functions[name], ast.FunctionDef) for name in route_names)
