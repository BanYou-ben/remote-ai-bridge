from app.domain.health import BridgeHealth, CheckResult, CheckStatus, HealthReport


def check(name, status):
    return CheckResult(name, status, "detail")


def report(statuses):
    return HealthReport(*(check(str(index), status) for index, status in enumerate(statuses)))


def test_all_checks_must_pass_for_ready():
    assert report([CheckStatus.PASS] * 5).health is BridgeHealth.READY


def test_live_tunnel_with_failed_probe_is_degraded():
    assert report(
        [CheckStatus.PASS, CheckStatus.PASS, CheckStatus.PASS, CheckStatus.PASS, CheckStatus.FAIL]
    ).health is BridgeHealth.DEGRADED


def test_missing_tunnel_is_disconnected():
    assert report(
        [CheckStatus.PASS, CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.SKIP, CheckStatus.SKIP]
    ).health is BridgeHealth.DISCONNECTED

