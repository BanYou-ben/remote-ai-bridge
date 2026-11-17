"""Opt-in DeepSeek compatibility probe using only isolated fixture tools."""
from __future__ import annotations

import getpass
import json

from app.infrastructure.ai.openai_compatible_agent_model import OpenAICompatibleResponsesAgentModel
from app.services.agent.agent_contract import AgentRequest
from app.services.agent.agent_runner import AgentRunner
from app.services.agent.tool_contract import ProfileNameArguments, Tool, ToolResult, profile_tool_definition
from app.services.agent.tool_registry import ToolRegistry


FIXTURE_PROFILE = "fixture-server"
EVIDENCE_ID = "connection.ssh.config"
PROMPT = (
    "For this isolated test, call get_runtime_status for fixture-server, then call "
    "run_doctor for fixture-server, then call submit_diagnosis citing the collected "
    "connection evidence. Call exactly one function per decision."
)


def _fixture_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def status(arguments: ProfileNameArguments) -> ToolResult:
        return ToolResult("get_runtime_status", True, {"state": "STOPPED"})

    def doctor(arguments: ProfileNameArguments) -> ToolResult:
        return ToolResult("run_doctor", True, {"evidence": [{
            "id": EVIDENCE_ID,
            "category": "connection",
            "name": "Fixture SSH configuration",
            "status": "PASS",
            "detail": "fixture configuration available",
            "error_code": None,
            "http_status": None,
        }]})

    registry.register(Tool(profile_tool_definition("get_runtime_status", "Read fixture runtime status."), ProfileNameArguments, status))
    registry.register(Tool(profile_tool_definition("run_doctor", "Read fixture connection evidence."), ProfileNameArguments, doctor))
    return registry


def main() -> int:
    print("DeepSeek isolated probe: deepseek-flash, up to 3 requests; charges may apply.")
    print("Only fixed fixture question, mock tool observations and mock Evidence are sent to DeepSeek.")
    print("No real Profile, SSH connection or server is used.")
    try:
        if input("Type YES to authorize these paid API requests: ").strip() != "YES":
            print("Probe cancelled.")
            return 1
        key = getpass.getpass("DeepSeek API key: ")
        if not key or not key.strip():
            print("No API key was entered; probe cancelled.")
            return 1
        print("API key received (hidden). Validity will be checked by the API.")
        model = OpenAICompatibleResponsesAgentModel(
            model="deepseek-flash",
            base_url="https://api.deepseek.com",
            api_key=key,
            timeout=30,
            max_retries=0,
            strict_tools=False,
            tool_choice="auto",
            diagnostic_errors=True,
        )
        result = AgentRunner(_fixture_registry(), model, max_steps=3).run(
            AgentRequest(FIXTURE_PROFILE, PROMPT)
        )
        names = [item.tool_name for item in result.observations]
        expected = ["get_runtime_status", "run_doctor"]
        allowed = set(expected)
        grounded = result.diagnosis is not None and EVIDENCE_ID in result.diagnosis.evidence_ids
        output = {
            "status": result.status,
            "error_code": result.error_code,
            "tool_calls": [name if name in allowed else "[unknown]" for name in names],
            "tool_success": [item.ok for item in result.observations],
            "grounded_diagnosis": grounded,
            "expected_sequence": names == expected,
        }
        print(json.dumps(output))
        return 0 if result.status == "completed" and names == expected and grounded and all(item.ok for item in result.observations) else 1
    except (EOFError, KeyboardInterrupt):
        print("Probe cancelled.")
        return 1
    except Exception:
        print("Probe could not be started.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
