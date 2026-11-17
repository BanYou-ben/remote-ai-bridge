"""Manual, locally invoked DeepSeek Agent validation entry point."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from collections.abc import Sequence

from app.bootstrap import create_services
from app.infrastructure.ai.openai_compatible_agent_model import OpenAICompatibleResponsesAgentModel
from app.services.agent.agent_contract import AgentRequest
from app.services.agent.agent_runner import AgentRunner


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MAX_VALIDATION_STEPS = 12


def _steps(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max steps must be an integer") from exc
    if not 1 <= number <= MAX_VALIDATION_STEPS:
        raise argparse.ArgumentTypeError("max steps must be between 1 and 12")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one read-only DeepSeek Agent validation")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--max-steps", type=_steps, default=6)
    args = parser.parse_args(argv)

    try:
        request = AgentRequest(args.profile, args.question)
        if not args.model.strip():
            raise ValueError("model must not be empty")
        print("This validation sends diagnostic data to DeepSeek and may incur charges.")
        key = os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("DeepSeek API key: ")
        if not key or not key.strip():
            raise ValueError("API key is required")
        services = create_services()
        services.profiles.get(request.profile_name)
        model = OpenAICompatibleResponsesAgentModel(
            model=args.model,
            base_url=DEEPSEEK_BASE_URL,
            api_key=key,
        )
        result = AgentRunner(
            services.agent_tool_registry, model, max_steps=args.max_steps
        ).run(request)
        # Do not print model- or tool-supplied text, which may contain sensitive data.
        output = {
            "status": result.status,
            "step_count": result.step_count,
            "error_code": result.error_code,
            "diagnosis_present": result.diagnosis is not None,
        }
        print(json.dumps(output, ensure_ascii=True))
        return 0 if result.status == "completed" else 1
    except (KeyboardInterrupt, EOFError):
        print("Validation cancelled.")
        return 1
    except Exception:
        # Exceptions can contain provider responses, paths, or secrets.
        print("Validation could not be started.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
