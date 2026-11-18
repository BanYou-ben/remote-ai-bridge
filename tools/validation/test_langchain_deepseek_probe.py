from types import SimpleNamespace

from tools.validation import langchain_deepseek_probe as probe


def test_cancel_never_reads_key_or_constructs_model(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "NO")
    monkeypatch.setattr(probe.getpass, "getpass", lambda _: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(probe, "create_deepseek_model", lambda _: (_ for _ in ()).throw(AssertionError()))
    assert probe.main() == 1


def test_probe_uses_fixtures_and_never_prints_key(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "YES")
    monkeypatch.setattr(probe.getpass, "getpass", lambda _: "fixture-secret")
    monkeypatch.setattr(probe, "create_deepseek_model", lambda _: object())
    def run(registry, model, profile, question, secret, *, max_model_calls):
        assert profile == probe.FIXTURE_PROFILE
        assert max_model_calls == 4
        assert {x.name for x in registry.list_definitions()} == {"run_doctor", "get_runtime_status"}
        return SimpleNamespace(status="completed", error_code=None,
            observations=[SimpleNamespace(tool_name="run_doctor", ok=True)],
            diagnosis=SimpleNamespace(evidence_ids=(probe.EVIDENCE_ID,)))
    monkeypatch.setattr(probe, "run_web_diagnostics", run)
    assert probe.main() == 0
    assert "fixture-secret" not in capsys.readouterr().out
