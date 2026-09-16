from __future__ import annotations

from dataclasses import dataclass
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile


DEFAULT_CANDIDATE_PORTS = (7890, 7892, 7897, 10808, 10809)


@dataclass(frozen=True)
class LocalProxyReport:
    tcp: CheckResult
    handshake: CheckResult
    endpoint: CheckResult

    @property
    def passed(self) -> bool:
        return self.tcp.passed and self.handshake.passed and self.endpoint.passed


class LocalProxyService:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def inspect(self, profile: Profile) -> LocalProxyReport:
        tcp = self.check_tcp(profile.local_proxy_host, profile.local_proxy_port)
        if not tcp.passed:
            skipped = CheckResult("HTTP proxy handshake", CheckStatus.SKIP, "TCP check failed")
            return LocalProxyReport(tcp, skipped, CheckResult("AI endpoint probe", CheckStatus.SKIP, "TCP check failed"))
        endpoint_host = urlsplit(profile.endpoint_probe_url).hostname or "api.openai.com"
        handshake = self.check_http_proxy(profile.local_proxy_host, profile.local_proxy_port, endpoint_host)
        if not handshake.passed:
            return LocalProxyReport(
                tcp,
                handshake,
                CheckResult("AI endpoint probe", CheckStatus.SKIP, "proxy handshake failed"),
            )
        endpoint = self.check_endpoint(
            profile.local_proxy_host,
            profile.local_proxy_port,
            profile.endpoint_probe_url,
        )
        return LocalProxyReport(tcp, handshake, endpoint)

    def check_tcp(self, host: str, port: int) -> CheckResult:
        try:
            with socket.create_connection((host, port), timeout=self.timeout):
                return CheckResult("TCP reachable", CheckStatus.PASS, f"{host}:{port} accepted a connection")
        except ConnectionRefusedError:
            return CheckResult("TCP reachable", CheckStatus.FAIL, "connection refused", "LOCAL_PROXY_REFUSED")
        except TimeoutError:
            return CheckResult("TCP reachable", CheckStatus.FAIL, "connection timed out", "LOCAL_PROXY_TIMEOUT")
        except OSError as exc:
            return CheckResult("TCP reachable", CheckStatus.FAIL, str(exc), "LOCAL_PROXY_UNREACHABLE")

    def check_http_proxy(self, host: str, port: int, endpoint_host: str = "api.openai.com") -> CheckResult:
        authority = f"{endpoint_host}:443"
        request = (
            f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        try:
            with socket.create_connection((host, port), timeout=self.timeout) as connection:
                connection.settimeout(self.timeout)
                connection.sendall(request)
                first_line = connection.recv(512).split(b"\r\n", 1)[0]
        except TimeoutError:
            return CheckResult("HTTP proxy handshake", CheckStatus.FAIL, "proxy did not answer in time", "PROXY_HANDSHAKE_TIMEOUT")
        except OSError as exc:
            return CheckResult("HTTP proxy handshake", CheckStatus.FAIL, str(exc), "PROXY_HANDSHAKE_IO_ERROR")
        if not first_line.startswith(b"HTTP/"):
            return CheckResult("HTTP proxy handshake", CheckStatus.FAIL, "listener did not return an HTTP response", "NOT_HTTP_PROXY")
        safe_line = first_line.decode("ascii", errors="replace")[:160]
        parts = safe_line.split()
        if len(parts) < 2 or parts[1] != "200":
            return CheckResult(
                "HTTP proxy handshake",
                CheckStatus.FAIL,
                f"proxy CONNECT was not established: {safe_line}",
                "PROXY_CONNECT_REJECTED",
            )
        return CheckResult("HTTP proxy handshake", CheckStatus.PASS, safe_line)

    def check_endpoint(self, host: str, port: int, url: str) -> CheckResult:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://{host}:{port}", "https": f"http://{host}:{port}"})
        )
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "remote-ai-bridge/phase1"})
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except urllib.error.URLError as exc:
            reason = exc.reason
            code = "ENDPOINT_TIMEOUT" if isinstance(reason, TimeoutError) else "ENDPOINT_UNREACHABLE"
            return CheckResult("AI endpoint probe", CheckStatus.FAIL, str(reason), code)
        except (TimeoutError, OSError) as exc:
            return CheckResult("AI endpoint probe", CheckStatus.FAIL, str(exc), "ENDPOINT_UNREACHABLE")
        if status in (401, 403) or 200 <= status < 300:
            return CheckResult(
                "AI endpoint probe",
                CheckStatus.PASS,
                f"endpoint returned HTTP {status}; network path is healthy",
                http_status=status,
            )
        return CheckResult(
            "AI endpoint probe",
            CheckStatus.FAIL,
            f"unexpected endpoint response HTTP {status}",
            "ENDPOINT_UNEXPECTED_STATUS",
            status,
        )
