from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import cli
from app.domain.errors import RABError


def test_serve_defaults_to_loopback_single_process_without_reload(tmp_path):
    application = object()
    with (
        patch.object(cli, "create_services") as create_services,
        patch.object(cli, "create_app", return_value=application) as create_app,
        patch("uvicorn.run") as run,
    ):
        result = cli.main(["--state-dir", str(tmp_path), "serve"])

    assert result == 0
    create_services.assert_not_called()
    create_app.assert_called_once_with(Path(tmp_path))
    run.assert_called_once_with(
        application,
        host="127.0.0.1",
        port=8000,
        workers=1,
        reload=False,
    )


def test_serve_passes_valid_custom_port(tmp_path):
    with patch.object(cli, "create_app", return_value=object()) as create_app, patch("uvicorn.run") as run:
        assert cli.main(["--state-dir", str(tmp_path), "serve", "--port", "8123"]) == 0

    create_app.assert_called_once_with(Path(tmp_path))
    assert run.call_args.kwargs["port"] == 8123


@pytest.mark.parametrize("port", [0, 65536, True])
def test_serve_rejects_out_of_range_or_boolean_port_before_app_creation(port):
    with patch.object(cli, "create_app") as create_app, pytest.raises(RABError) as caught:
        cli.command_serve(None, "127.0.0.1", port)

    assert caught.value.code == "API_PORT_INVALID"
    create_app.assert_not_called()


def test_serve_parser_rejects_bool_like_port():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["serve", "--port", "true"])


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.10.20.30"])
def test_serve_allows_loopback_hosts(host):
    cli.validate_api_bind(host, 8000)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "8.8.8.8", "example.test"])
def test_serve_rejects_non_loopback_hosts(host):
    with pytest.raises(RABError) as caught:
        cli.validate_api_bind(host, 8000)

    assert caught.value.code == "API_BIND_UNSAFE"
    assert "only bind to a loopback address" in caught.value.message


def test_serve_parser_exposes_no_workers_or_reload_options():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["serve", "--workers", "2"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["serve", "--reload"])


def test_serve_does_not_duplicate_runtime_shutdown_logic():
    source = inspect.getsource(cli.command_serve)

    assert ".shutdown(" not in source
    assert "RuntimeManager" not in source
    assert "create_services" not in source
