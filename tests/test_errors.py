from app.domain.errors import RABError
from app.redaction import redact


def test_structured_error_redacts_message_and_nested_details():
    error = RABError(
        "SETUP_FAILED",
        "password=hunter2 Authorization: Bearer abc.def",
        retryable=True,
        details={
            "password": "hunter2",
            "nested": {"token": "abc.def", "detail": "Cookie: session=hidden"},
        },
    )

    payload = error.to_dict()
    assert payload["code"] == "SETUP_FAILED"
    assert payload["retryable"] is True
    assert "hunter2" not in str(payload)
    assert "abc.def" not in str(payload)
    assert "session=hidden" not in str(payload)
    assert payload["details"]["password"] == "[REDACTED]"


def test_reusable_redaction_covers_password_and_private_key():
    value = "password=secret\n-----BEGIN OPENSSH PRIVATE KEY-----\nmaterial\n-----END OPENSSH PRIVATE KEY-----"

    output = redact(value)

    assert "secret" not in output
    assert "material" not in output


def test_structured_error_redacts_compound_sensitive_keys_only():
    sensitive = {
        "ssh_password": "one",
        "proxyPassword": "two",
        "auth_token": "three",
        "bootstrap_password": "four",
        "apiKey": "five",
        "accessToken": "six",
    }
    ordinary = {
        "username": "tester",
        "host": "server.example",
        "remote_port": 17890,
        "message": "healthy",
    }

    payload = RABError("TEST", "failed", details={**sensitive, **ordinary}).to_dict()["details"]

    for key in sensitive:
        assert payload[key] == "[REDACTED]"
    assert {key: payload[key] for key in ordinary} == ordinary
