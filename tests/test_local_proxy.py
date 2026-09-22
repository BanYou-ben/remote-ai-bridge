import io
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from app.domain.errors import RABError
from app.domain.health import CheckResult
from app.domain.health import CheckStatus
from app.services.local_proxy import DEFAULT_CANDIDATE_PORTS, LocalProxyService


class Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_local_401_is_network_pass():
    opener = MagicMock()
    opener.open.side_effect = urllib.error.HTTPError(
        "https://api.openai.com/v1/models", 401, "Unauthorized", {}, io.BytesIO()
    )
    with patch("app.services.local_proxy.urllib.request.build_opener", return_value=opener):
        result = LocalProxyService().check_endpoint("127.0.0.1", 7897, "https://api.openai.com/v1/models")
    assert result.status is CheckStatus.PASS
    assert result.http_status == 401


def test_local_403_is_network_pass():
    opener = MagicMock()
    opener.open.side_effect = urllib.error.HTTPError(
        "https://api.openai.com/v1/models", 403, "Forbidden", {}, io.BytesIO()
    )
    with patch("app.services.local_proxy.urllib.request.build_opener", return_value=opener):
        result = LocalProxyService().check_endpoint("127.0.0.1", 7897, "https://api.openai.com/v1/models")
    assert result.status is CheckStatus.PASS
    assert result.http_status == 403


def test_unexpected_endpoint_status_is_not_declared_healthy():
    opener = MagicMock()
    opener.open.return_value = Response(502)
    with patch("app.services.local_proxy.urllib.request.build_opener", return_value=opener):
        result = LocalProxyService().check_endpoint("127.0.0.1", 7897, "https://api.openai.com/v1/models")
    assert result.status is CheckStatus.FAIL
    assert result.error_code == "ENDPOINT_UNEXPECTED_STATUS"


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, timeout):
        pass

    def sendall(self, value):
        self.sent = value

    def recv(self, size):
        return self.response


def test_handshake_requires_successful_connect_not_just_any_http_server():
    connection = FakeSocket(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    with patch("app.services.local_proxy.socket.create_connection", return_value=connection):
        result = LocalProxyService().check_http_proxy("127.0.0.1", 7897)
    assert result.status is CheckStatus.FAIL
    assert result.error_code == "PROXY_CONNECT_REJECTED"
    assert connection.sent.startswith(b"CONNECT api.openai.com:443 HTTP/1.1")


def test_handshake_accepts_http_connect_200():
    connection = FakeSocket(b"HTTP/1.1 200 Connection established\r\n\r\n")
    with patch("app.services.local_proxy.socket.create_connection", return_value=connection):
        result = LocalProxyService().check_http_proxy("127.0.0.1", 7897)
    assert result.status is CheckStatus.PASS
    assert connection.sent.startswith(b"CONNECT api.openai.com:443 HTTP/1.1")


def test_handshake_uses_explicit_https_endpoint_port():
    connection = FakeSocket(b"HTTP/1.1 200 Connection established\r\n\r\n")
    service = LocalProxyService()
    with patch("app.services.local_proxy.socket.create_connection", return_value=connection):
        result = service.check_http_proxy("127.0.0.1", 7897, "example.com", 8443)

    assert result.status is CheckStatus.PASS
    assert connection.sent.startswith(b"CONNECT example.com:8443 HTTP/1.1")
    assert b"Host: example.com:8443" in connection.sent


def test_handshake_brackets_ipv6_endpoint_authority():
    connection = FakeSocket(b"HTTP/1.1 200 Connection established\r\n\r\n")
    service = LocalProxyService()
    with patch("app.services.local_proxy.socket.create_connection", return_value=connection):
        result = service.check_http_proxy("127.0.0.1", 7897, "2001:db8::1", 8443)

    assert result.status is CheckStatus.PASS
    assert connection.sent.startswith(b"CONNECT [2001:db8::1]:8443 HTTP/1.1")


def test_discovery_parses_explicit_endpoint_port_for_connect():
    service = LocalProxyService()
    passing = CheckResult("check", CheckStatus.PASS, "safe")
    with (
        patch.object(service, "check_tcp", return_value=passing),
        patch.object(service, "check_http_proxy", return_value=passing) as connect,
        patch.object(service, "check_endpoint", return_value=passing),
    ):
        result = service.discover(
            "https://example.com:8443/health",
            candidate_ports=(7897,),
        )

    assert result.selected.port == 7897
    connect.assert_called_once_with("127.0.0.1", 7897, "example.com", 8443)


class DiscoveryProxy(LocalProxyService):
    def __init__(self, states):
        super().__init__()
        self.states = states
        self.calls = []

    def check_tcp(self, host, port):
        self.calls.append(("tcp", port))
        passed = self.states[port][0]
        return CheckResult("TCP", CheckStatus.PASS if passed else CheckStatus.FAIL, "safe", None if passed else "TCP_FAIL")

    def check_http_proxy(self, host, port, endpoint_host="api.openai.com", endpoint_port=443):
        self.calls.append(("connect", port))
        passed = self.states[port][1]
        return CheckResult("CONNECT", CheckStatus.PASS if passed else CheckStatus.FAIL, "safe", None if passed else "CONNECT_FAIL")

    def check_endpoint(self, host, port, url):
        self.calls.append(("endpoint", port))
        passed = self.states[port][2]
        return CheckResult("Endpoint", CheckStatus.PASS if passed else CheckStatus.FAIL, "safe", None if passed else "ENDPOINT_FAIL", 401 if passed else None)


ENDPOINT = "https://api.openai.com/v1/models"


def test_discovery_no_valid_proxy_returns_structured_error():
    service = DiscoveryProxy({7890: (False, False, False), 7897: (True, False, False)})

    with pytest.raises(RABError) as raised:
        service.discover(ENDPOINT, candidate_ports=(7890, 7897))

    assert raised.value.code == "LOCAL_PROXY_NOT_FOUND"
    assert raised.value.retryable is True
    assert [item["port"] for item in raised.value.details["candidates"]] == [7890, 7897]


def test_discovery_one_valid_proxy_is_selected_automatically():
    service = DiscoveryProxy({7890: (False, False, False), 7897: (True, True, True)})

    result = service.discover(ENDPOINT, candidate_ports=(7890, 7897))

    assert result.selected.port == 7897
    assert result.selected.passed


def test_discovery_multiple_valid_proxies_requires_explicit_selection():
    service = DiscoveryProxy({7890: (True, True, True), 7897: (True, True, True)})

    with pytest.raises(RABError) as raised:
        service.discover(ENDPOINT, candidate_ports=(7890, 7897))

    assert raised.value.code == "LOCAL_PROXY_SELECTION_REQUIRED"
    assert raised.value.details == {
        "candidates": [
            {"host": "127.0.0.1", "port": 7890},
            {"host": "127.0.0.1", "port": 7897},
        ]
    }


def test_discovery_tcp_failure_skips_connect_and_endpoint():
    service = DiscoveryProxy({7890: (False, True, True)})

    with pytest.raises(RABError):
        service.discover(ENDPOINT, candidate_ports=(7890,))

    assert service.calls == [("tcp", 7890)]


def test_discovery_connect_failure_skips_endpoint():
    service = DiscoveryProxy({7890: (True, False, True)})

    with pytest.raises(RABError) as raised:
        service.discover(ENDPOINT, candidate_ports=(7890,))

    assert raised.value.details["candidates"][0]["connect_reachable"] is False
    assert service.calls == [("tcp", 7890), ("connect", 7890)]


def test_discovery_endpoint_failure_is_not_a_valid_proxy():
    service = DiscoveryProxy({7890: (True, True, False)})

    with pytest.raises(RABError) as raised:
        service.discover(ENDPOINT, candidate_ports=(7890,))

    candidate = raised.value.details["candidates"][0]
    assert candidate["tcp_reachable"] is True
    assert candidate["connect_reachable"] is True
    assert candidate["endpoint_reachable"] is False
    assert set(candidate) == {
        "host", "port", "tcp_reachable", "connect_reachable", "endpoint_reachable", "error_code"
    }


def test_discovery_explicit_selection_continues_with_requested_healthy_proxy():
    service = DiscoveryProxy({7890: (True, True, True), 7897: (True, True, True)})

    result = service.discover(ENDPOINT, candidate_ports=(7890, 7897), selected_port=7897)

    assert result.selected.port == 7897
    assert service.calls == [("tcp", 7897), ("connect", 7897), ("endpoint", 7897)]
    assert DEFAULT_CANDIDATE_PORTS == (7890, 7892, 7897, 10808, 10809)


@pytest.mark.parametrize("selected_port", [7897.0, True, 0, 65536])
def test_discovery_rejects_non_strict_selected_port_before_network(selected_port):
    service = DiscoveryProxy({7897: (True, True, True)})

    with pytest.raises(RABError) as raised:
        service.discover(
            ENDPOINT,
            candidate_ports=(7897,),
            selected_port=selected_port,
        )

    assert raised.value.code == "LOCAL_PROXY_SELECTION_INVALID"
    assert service.calls == []


def test_discovery_rejects_endpoint_credentials_before_network_checks():
    service = DiscoveryProxy({7897: (True, True, True)})

    with pytest.raises(RABError) as raised:
        service.discover("https://user:secret@example.invalid/v1/models", candidate_ports=(7897,))

    assert raised.value.code == "LOCAL_PROXY_DISCOVERY_INVALID"
    assert "secret" not in repr(raised.value.to_dict())
    assert service.calls == []


def test_discovery_rejects_invalid_endpoint_port_before_network_checks():
    service = DiscoveryProxy({7897: (True, True, True)})

    with pytest.raises(RABError) as raised:
        service.discover("https://example.com:99999/", candidate_ports=(7897,))

    assert raised.value.code == "LOCAL_PROXY_DISCOVERY_INVALID"
    assert "99999" not in repr(raised.value.to_dict())
    assert service.calls == []
