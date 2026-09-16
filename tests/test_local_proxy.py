import io
import urllib.error
from unittest.mock import MagicMock, patch

from app.domain.health import CheckStatus
from app.services.local_proxy import LocalProxyService


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
