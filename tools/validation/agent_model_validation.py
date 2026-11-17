"""Manual, locally invoked DeepSeek Agent validation entry point."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
from collections.abc import Sequence

from app.bootstrap import create_services
from app.infrastructure.ai.openai_compatible_agent_model import OpenAICompatibleResponsesAgentModel
from app.redaction import redact
from app.services.agent.agent_contract import AgentRequest
from app.services.agent.agent_runner import AgentRunner


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MAX_VALIDATION_STEPS = 12
MAX_SUMMARY_LENGTH = 500


def _safe_text(value: str, key: str, limit: int) -> str:
    # Redact before truncation so a truncated secret cannot evade a pattern.
    value = value.replace(key, "[REDACTED]")
    value = redact(value)
    value = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED]", value)
    value = re.sub(r"https?://[^\s]+", "[REDACTED_URL]", value, flags=re.IGNORECASE)
    value = re.sub(r"(?<![\w])(?:[A-Za-z]:\\|/home/|/Users/)[^\s]*", "[REDACTED_PATH]", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[REDACTED_IP]", value)
    value = "".join(character if character.isprintable() else " " for character in value)
    return value[:limit]


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
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.model):
            raise ValueError("model must not be empty")
        print(f"Model: {args.model}")
        print("This request may incur charges.")
        print("Your question, tool observations and Evidence will be sent to DeepSeek.")
        if input("Type YES to continue: ").strip() != "YES":
            print("Validation cancelled.")
            return 1
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
        allowed_tools = {item.name for item in services.agent_tool_registry.list_definitions()}
        output = {
            "status": result.status,
            "step_count": result.step_count,
            "error_code": result.error_code,
            "diagnosis_present": result.diagnosis is not None,
            "tools": [
                {
                    "name": observation.tool_name if observation.tool_name in allowed_tools else "[unknown]",
                    "ok": observation.ok,
                }
                for observation in result.observations[:MAX_VALIDATION_STEPS]
            ],
        }
        if result.diagnosis is not None:
            output["diagnosis"] = {
                "stage": _safe_text(result.diagnosis.diagnosis_stage, key, 128),
                "evidence_ids": [
                    _safe_text(item, key, 128)
                    for item in result.diagnosis.evidence_ids[:32]
                ],
                "summary": _safe_text(result.diagnosis.summary, key, MAX_SUMMARY_LENGTH),
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
