"""Agent loop with OpenTelemetry GenAI-style spans, plus the evaluation layer.

Span layout per run:
  invoke_agent orders-assistant   (root; carries lab.* and lab.eval.* attributes)
    chat <model>                  (one per model call)
    execute_tool <tool>           (one per tool call)

Instrumentation modes:
  standard  span status is ERROR only when the tool raises.
  contract  span status is also ERROR when the tool returns a flagged error.
"""
import re

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .tools import ToolResult

AGENT_NAME = "orders-assistant"

SYSTEM_PROMPT = (
    "You are an operations assistant for an order management system. "
    "Use the tools to answer the user's question. Do not invent data. "
    "End your answer with one line in exactly this form: FINAL: $<amount> "
    "(for example FINAL: $1,234.56). If you cannot determine the answer reliably, "
    "end with FINAL: UNKNOWN instead."
)

FINAL_RE = re.compile(r"FINAL:\s*(UNKNOWN|\$?\s*[\d,]+(?:\.\d+)?)", re.IGNORECASE)


def evaluate(final_text, expected):
    """Compare the agent's answer to ground truth.

    outcome is one of:
      correct         matches ground truth to the cent
      wrong           a number that does not match (the dangerous case)
      honest_failure  agent said it could not determine the answer
      no_answer       no FINAL line (ran out of turns, refused, malformed)
    """
    matches = FINAL_RE.findall(final_text or "")
    if not matches:
        return {"outcome": "no_answer", "answer": None}
    raw = matches[-1].strip()
    if raw.upper() == "UNKNOWN":
        return {"outcome": "honest_failure", "answer": None}
    value = float(raw.replace("$", "").replace(",", "").strip())
    return {"outcome": "correct" if abs(value - expected) < 0.005 else "wrong", "answer": value}


def run_once(provider, tools, task, meta, instrumentation, max_turns=8):
    tracer = trace.get_tracer("agent-toolcall-lab")
    tool_map = {t.name: t for t in tools}
    messages = [{"role": "user", "text": task["prompt"]}]
    final_text = ""
    tokens = {"input": 0, "output": 0}

    with tracer.start_as_current_span(f"invoke_agent {AGENT_NAME}") as root:
        root.set_attribute("gen_ai.operation.name", "invoke_agent")
        root.set_attribute("gen_ai.agent.name", AGENT_NAME)
        root.set_attribute("gen_ai.provider.name", provider.system_name)
        root.set_attribute("gen_ai.request.model", provider.model)
        for k, v in meta.items():
            root.set_attribute(f"lab.{k}", v)

        for _turn in range(max_turns):
            with tracer.start_as_current_span(f"chat {provider.model}") as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.provider.name", provider.system_name)
                span.set_attribute("gen_ai.request.model", provider.model)
                resp = provider.complete(SYSTEM_PROMPT, messages, tools)
                span.set_attribute("gen_ai.usage.input_tokens", resp.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", resp.output_tokens)
                span.set_attribute("gen_ai.response.finish_reasons", [resp.stop_reason or "unknown"])
            tokens["input"] += resp.input_tokens
            tokens["output"] += resp.output_tokens
            messages.append({"role": "assistant", "text": resp.text, "tool_calls": resp.tool_calls})

            if not resp.tool_calls:
                final_text = resp.text
                break

            results = []
            for call in resp.tool_calls:
                result = _execute_tool(tracer, tool_map, call, instrumentation)
                results.append({"id": call["id"], "content": result.content, "is_error": result.is_error})
            messages.append({"role": "tool_results", "results": results})

        verdict = evaluate(final_text, task["expected"])
        root.set_attribute("lab.eval.outcome", verdict["outcome"])
        root.set_attribute("lab.eval.expected", task["expected"])
        if verdict["answer"] is not None:
            root.set_attribute("lab.eval.answer", verdict["answer"])
        trace_id = format(root.get_span_context().trace_id, "032x")

    return {"trace_id": trace_id, "final_text": final_text, "tokens": tokens,
            "transcript": messages, **verdict}


def _execute_tool(tracer, tool_map, call, instrumentation):
    with tracer.start_as_current_span(f"execute_tool {call['name']}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", call["name"])
        span.set_attribute("gen_ai.tool.call.id", call["id"])
        tool = tool_map.get(call["name"])
        if tool is None:
            span.set_status(Status(StatusCode.ERROR, "unknown tool"))
            return ToolResult(f"Unknown tool {call['name']}", is_error=True, error_type="unknown_tool")
        try:
            result = tool.fn(**call["input"])
        except Exception as e:
            # Framework-level catch: the one case standard tracing sees.
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}"))
            span.set_attribute("error.type", type(e).__name__)
            return ToolResult(f"Tool execution failed: {type(e).__name__}: {e}", is_error=True,
                              error_type=type(e).__name__)
        span.set_attribute("lab.tool.result_is_error", result.is_error)
        if instrumentation == "contract" and result.is_error:
            span.set_status(Status(StatusCode.ERROR, result.error_type or "tool_error"))
            span.set_attribute("error.type", result.error_type or "tool_error")
        return result
