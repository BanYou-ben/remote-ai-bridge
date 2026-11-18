"""LangChain orchestration with local tool and evidence enforcement."""
from __future__ import annotations

from typing import Literal
import openai

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.structured_output import StructuredOutputError, ToolStrategy
from langchain_core.messages import AIMessage
from langsmith import tracing_context
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, ConfigDict, Field

from app.services.agent.agent_contract import AgentObservation, AgentRunResult
from app.services.agent.langchain_tool_bridge import LangChainToolBridge
from app.services.ai.diagnosis import DiagnosisValidationError, StructuredDiagnosis, validate_diagnosis_against_evidence
from app.services.ai.schemas import DiagnosticEvidence


class DiagnosisOutput(BaseModel):
    """A diagnosis grounded in evidence returned by the diagnostic tools."""
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1, max_length=1000)
    diagnosis_stage: str = Field(description="Evidence category followed by a substage, or unknown")
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    possible_causes: list[str] = Field(max_length=10)
    recommended_actions: list[str] = Field(max_length=10)
    confidence: Literal["low", "medium", "high"]


class _SingleCall(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        response = handler(request)
        for message in response.result:
            if isinstance(message, AIMessage) and (len(message.tool_calls) > 1 or message.invalid_tool_calls):
                raise _InvalidToolCalls()
        return response


class _InvalidToolCalls(Exception):
    pass


def run_langchain_diagnostics(registry, model, profile_name, question, secret, *, max_model_calls=6):
    # Imported lazily so that legacy CLI usage does not depend on this execution path.
    from app.services.agent.web_diagnostics import ALIAS, ALLOWED_TOOLS, _safe_evidence, _safe_metrics, safe_question

    observations = []
    evidence = {}

    def sanitize(name, result):
        raw = result.get("data") if isinstance(result.get("data"), dict) else {}
        data = {}
        if name == "list_application_targets":
            data["targets"] = raw.get("targets", [])
        metrics = _safe_metrics(name, raw)
        if metrics:
            data["metrics"] = metrics
        safe_items = []
        entries = raw.get("evidence", [])
        if isinstance(entries, list):
            for item in entries[:100]:
                if not isinstance(item, dict):
                    continue
                try:
                    candidate = DiagnosticEvidence(**item)
                    safe = _safe_evidence(candidate)
                except (TypeError, ValueError):
                    continue
                if safe:
                    evidence[safe.id] = safe
                    safe_items.append(safe.to_dict())
        if safe_items:
            data["evidence"] = safe_items
        ok = result.get("ok") is True
        observations.append(AgentObservation(name, {"profile_name": ALIAS}, ok, data, None,
                                             "Tool completed" if ok else "Tool failed"))
        return {"ok": ok, "data": data, "error_code": None if ok else "TOOL_FAILED"}

    bridge = LangChainToolBridge(registry, profile_name, sanitize, profile_alias=ALIAS, allowed_names=ALLOWED_TOOLS)
    try:
        graph = create_agent(
            model=model,
            tools=bridge.build_tools(),
            system_prompt=("Diagnose using only the provided read-only tools. Use selected_profile. "
                           "Call at most one tool at a time. Application targets are opaque aliases. "
                           "Cite only evidence IDs returned by tools; if evidence is insufficient, do not invent it."),
            response_format=ToolStrategy(DiagnosisOutput, handle_errors=False),
            middleware=[_SingleCall(), ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="error")],
        )
        with tracing_context(enabled=False):
            output = graph.invoke({"messages": [{"role": "user", "content": safe_question(question, secret).replace(profile_name, "[PROFILE]")}]},
                                  config={"recursion_limit": 16})
        value = output.get("structured_response")
        if not isinstance(value, DiagnosisOutput):
            raise ValueError("missing diagnosis")
        diagnosis = StructuredDiagnosis(summary=safe_question(value.summary, secret).replace(profile_name, "[PROFILE]"),
            diagnosis_stage=value.diagnosis_stage, evidence_ids=tuple(value.evidence_ids),
            possible_causes=tuple(safe_question(x, secret).replace(profile_name, "[PROFILE]") for x in value.possible_causes),
            recommended_actions=tuple(safe_question(x, secret).replace(profile_name, "[PROFILE]") for x in value.recommended_actions),
            confidence=value.confidence)
        validate_diagnosis_against_evidence(diagnosis, evidence.values())
        return AgentRunResult("completed", diagnosis, tuple(observations), len(observations))
    except (openai.APITimeoutError, TimeoutError):
        return AgentRunResult("failed", None, tuple(observations), len(observations),
                              "AGENT_MODEL_TIMEOUT", "AI model request timed out")
    except openai.APIStatusError as exc:
        # Classify using fixed codes and messages; never expose the provider body.
        from app.infrastructure.ai.openai_compatible_agent_model import _classify_status
        code, message, _ = _classify_status(exc.status_code, exc.body)
        return AgentRunResult("failed", None, tuple(observations), len(observations), code, message)
    except openai.APIConnectionError:
        return AgentRunResult("failed", None, tuple(observations), len(observations),
                              "AGENT_MODEL_CONNECTION_FAILED", "AI model connection failed")
    except (ModelCallLimitExceededError, GraphRecursionError):
        return AgentRunResult("failed", None, tuple(observations), len(observations),
                              "AGENT_MAX_STEPS", "AI diagnosis reached its execution limit")
    except _InvalidToolCalls:
        return AgentRunResult("failed", None, tuple(observations), len(observations),
                              "AGENT_MODEL_INVALID_RESPONSE", "AI model returned invalid tool calls")
    except (DiagnosisValidationError, StructuredOutputError, ValueError):
        return AgentRunResult("failed", None, tuple(observations), len(observations),
                              "AGENT_DIAGNOSIS_INVALID", "AI diagnosis is invalid or not grounded")
    except Exception:
        return AgentRunResult("failed", None, tuple(observations), len(observations),
                              "AGENT_DIAGNOSIS_FAILED", "AI diagnosis failed or was not grounded")
