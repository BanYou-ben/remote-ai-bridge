from __future__ import annotations

from pathlib import Path

from app import bootstrap


def test_application_target_service_reuses_profile_service_and_state_root(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.bootstrap.locate_ssh", lambda: "ssh")
    services = bootstrap.create_services(tmp_path)
    assert services.application_targets.profiles is services.profiles
    assert services.application_targets.store.root == services.store.root
    assert services.application_targets.store.targets_dir == tmp_path / "application_targets"


def test_agent_registry_remains_fifteen_read_only_diagnostic_tools(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.bootstrap.locate_ssh", lambda: "ssh")
    services = bootstrap.create_services(tmp_path)
    definitions = services.agent_tool_registry.list_definitions()
    assert len(definitions) == 15
    assert {item.risk_level for item in definitions} == {"read_only"}
    names = {item.name for item in definitions}
    assert names.isdisjoint(
        {
            "create_application_target",
            "update_application_target",
            "delete_application_target",
            "get_application_target",
            "check_application",
            "read_application_logs",
            "read_logs",
            "check_service",
            "check_http",
            "run_shell",
        }
    )


def test_phase58_production_code_has_no_remote_execution_or_agent_tool(tmp_path) -> None:
    files = (
        Path("app/domain/application_target.py"),
        Path("app/infrastructure/application_target_store.py"),
        Path("app/services/application_target_service.py"),
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for forbidden in (
        "ProcessRunner",
        "RemoteProbeService",
        "ssh_prefix",
        "journalctl",
        "curl ",
        "subprocess",
        "shell=True",
        "ToolRegistry",
        "Tool(",
    ):
        assert forbidden not in combined
