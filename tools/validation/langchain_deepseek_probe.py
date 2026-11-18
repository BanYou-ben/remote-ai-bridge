"""Opt-in, fixture-only probe of the production LangChain DeepSeek path."""
from __future__ import annotations

import getpass
import json

from app.services.agent.web_diagnostics import create_deepseek_model, run_web_diagnostics
from tools.validation.deepseek_isolated_probe import EVIDENCE_ID, FIXTURE_PROFILE, _fixture_registry


def main() -> int:
    print("LangChain DeepSeek fixture probe: deepseek-flash; up to 4 requests, charges may apply.")
    print("Only fixed mock question, mock tool observations and mock evidence are sent to DeepSeek.")
    print("No real profile, SSH connection or server is used.")
    try:
        if input("Type YES to authorize these paid API requests: ").strip() != "YES":
            print("Probe cancelled.")
            return 1
        secret = getpass.getpass("DeepSeek API key: ")
        if not secret.strip():
            print("No API key was entered; probe cancelled.")
            return 1
        result = run_web_diagnostics(
            _fixture_registry(), create_deepseek_model(secret), FIXTURE_PROFILE,
            "Call get_runtime_status, then run_doctor for selected_profile. "
            "Finally provide structured diagnosis citing connection.ssh.config.", secret, max_model_calls=4,
        )
        names = [item.tool_name for item in result.observations]
        grounded = result.diagnosis is not None and EVIDENCE_ID in result.diagnosis.evidence_ids
        output = {"status": result.status, "error_code": result.error_code,
                  "tool_calls": names, "tool_success": [item.ok for item in result.observations],
                  "grounded_diagnosis": grounded}
        print(json.dumps(output))
        return 0 if result.status == "completed" and grounded and all(item.ok for item in result.observations) else 1
    except (EOFError, KeyboardInterrupt):
        print("Probe cancelled.")
        return 1
    except Exception:
        print("Probe could not be started.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
