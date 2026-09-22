from __future__ import annotations

from dataclasses import dataclass
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from app.domain.health import CheckResult, CheckStatus
from app.domain.errors import RABError
from app.domain.profile import Profile, ProfileValidationError, validate_endpoint_probe_url


DEFAULT_CANDIDATE_PORTS = (7890, 7892, 7897, 10808, 10809)


@dataclass(frozen=True)
class LocalProxyReport:
    tcp: CheckResult
    handshake: CheckResult
    endpoint: CheckResult

    @property
    def passed(self) -> bool:
        return self.tcp.passed and self.handshake.passed and self.endpoint.passed


@dataclass(frozen=True)
class LocalProxyCandidate:
    host: str
    port: int
    tcp_reachable: bool
    connect_reachable: bool
    endpoint_reachable: bool
    error_code: str | None = None

    @property
    def passed(self) -> bool:
        return self.tcp_reachable and self.connect_reachable and self.endpoint_reachable

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "tcp_reachable": self.tcp_reachable,
            "connect_reachable": self.connect_reachable,
            "endpoint_reachable": self.endpoint_reachable,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class LocalProxyDiscovery:
    selected: LocalProxyCandidate
    candidates: tuple[LocalProxyCandidate, ...]


class LocalProxyService:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def inspect(self, profile: Profile) -> LocalProxyReport:
        tcp = self.check_tcp(profile.local_proxy_host, profile.local_proxy_port)
        if not tcp.passed:
            skipped = CheckResult("HTTP proxy handshake", CheckStatus.SKIP, "TCP check failed")
            return LocalProxyReport(tcp, skipped, CheckResult("AI endpoint probe", CheckStatus.SKIP, "TCP check failed"))
        endpoint_host, endpoint_port = self._endpoint_target(profile.endpoint_probe_url)
        handshake = self.check_http_proxy(
            profile.local_proxy_host,
            profile.local_proxy_port,
            endpoint_host,
            endpoint_port,
        )
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

    def discover(
        self,
        endpoint_probe_url: str,
        *,
        host: str = "127.0.0.1",
        candidate_ports: tuple[int, ...] = DEFAULT_CANDIDATE_PORTS,
        selected_port: int | None = None,
    ) -> LocalProxyDiscovery:
        ports = self.validate_discovery_inputs(
            endpoint_probe_url,
            host=host,
            candidate_ports=candidate_ports,
        )
        if selected_port is not None:
            if (
                isinstance(selected_port, bool)
                or not isinstance(selected_port, int)
                or not 1 <= selected_port <= 65535
                or selected_port not in ports
            ):
                raise RABError(
                    "LOCAL_PROXY_SELECTION_INVALID",
                    "the selected local proxy port is invalid or is not in the candidate set",
                    details={"host": host},
                )

        scan_ports = (selected_port,) if selected_port is not None else ports
        candidates = tuple(self._inspect_candidate(host, port, endpoint_probe_url) for port in scan_ports)
        valid = tuple(candidate for candidate in candidates if candidate.passed)
        if selected_port is not None:
            selected = next((candidate for candidate in valid if candidate.port == selected_port), None)
            if selected is None:
                raise RABError(
                    "LOCAL_PROXY_SELECTION_INVALID",
                    "the selected local proxy is not healthy",
                    retryable=True,
                    details={"host": host, "port": selected_port},
                )
            return LocalProxyDiscovery(selected, candidates)
        if not valid:
            raise RABError(
                "LOCAL_PROXY_NOT_FOUND",
                "no healthy local HTTP proxy was found",
                retryable=True,
                details={"candidates": [candidate.to_dict() for candidate in candidates]},
            )
        if len(valid) > 1:
            raise RABError(
                "LOCAL_PROXY_SELECTION_REQUIRED",
                "multiple healthy local proxies were found; an explicit selection is required",
                retryable=True,
                details={
                    "candidates": [
                        {"host": candidate.host, "port": candidate.port} for candidate in valid
                    ]
                },
            )
        return LocalProxyDiscovery(valid[0], candidates)

    @staticmethod
    def validate_discovery_inputs(
        endpoint_probe_url: str,
        *,
        host: str = "127.0.0.1",
        candidate_ports: tuple[int, ...] = DEFAULT_CANDIDATE_PORTS,
    ) -> tuple[int, ...]:
        if host != "127.0.0.1":
            raise RABError("LOCAL_PROXY_DISCOVERY_INVALID", "local proxy discovery is restricted to 127.0.0.1")
        try:
            validate_endpoint_probe_url(endpoint_probe_url)
        except ProfileValidationError:
            raise RABError(
                "LOCAL_PROXY_DISCOVERY_INVALID",
                "endpoint probe URL must be HTTPS and must not contain credentials, query, or fragment",
            ) from None
        try:
            raw_ports = tuple(candidate_ports)
        except TypeError:
            raise RABError("LOCAL_PROXY_DISCOVERY_INVALID", "local proxy candidate ports are invalid") from None
        if not raw_ports or any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in raw_ports
        ):
            raise RABError("LOCAL_PROXY_DISCOVERY_INVALID", "local proxy candidate ports are invalid")
        return tuple(dict.fromkeys(raw_ports))

    def _inspect_candidate(self, host: str, port: int, endpoint_probe_url: str) -> LocalProxyCandidate:
        tcp = self.check_tcp(host, port)
        if not tcp.passed:
            return LocalProxyCandidate(host, port, False, False, False, tcp.error_code)
        endpoint_host, endpoint_port = self._endpoint_target(endpoint_probe_url)
        handshake = self.check_http_proxy(host, port, endpoint_host, endpoint_port)
        if not handshake.passed:
            return LocalProxyCandidate(host, port, True, False, False, handshake.error_code)
        endpoint = self.check_endpoint(host, port, endpoint_probe_url)
        return LocalProxyCandidate(
            host,
            port,
            True,
            True,
            endpoint.passed,
            None if endpoint.passed else endpoint.error_code,
        )

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

    def check_http_proxy(
        self,
        host: str,
        port: int,
        endpoint_host: str = "api.openai.com",
        endpoint_port: int = 443,
    ) -> CheckResult:
        authority_host = f"[{endpoint_host}]" if ":" in endpoint_host else endpoint_host
        authority = f"{authority_host}:{endpoint_port}"
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

    @staticmethod
    def _endpoint_target(endpoint_probe_url: str) -> tuple[str, int]:
        try:
            parsed = urlsplit(endpoint_probe_url)
            port = parsed.port or 443
        except ValueError:
            raise RABError(
                "LOCAL_PROXY_DISCOVERY_INVALID",
                "endpoint probe URL contains an invalid port",
            ) from None
        if not parsed.hostname:
            raise RABError("LOCAL_PROXY_DISCOVERY_INVALID", "endpoint probe URL has no host")
        return parsed.hostname, port
