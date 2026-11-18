"""Model providers.

Messages are kept in one neutral format and converted per provider:
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "input"}]}
  {"role": "tool_results", "results": [{"id", "content", "is_error"}]}
"""
import json
import os
import re
from dataclasses import dataclass, field


@dataclass
class ModelResponse:
    text: str
    tool_calls: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


def _temperature():
    t = os.getenv("LAB_TEMPERATURE")
    return float(t) if t else None


# ---------------------------------------------------------------- Bedrock

class BedrockProvider:
    system_name = "aws.bedrock"

    def __init__(self, model):
        import boto3
        if not model:
            raise SystemExit("Set --model or LAB_MODEL_ID to a Bedrock model or inference profile id.")
        self.model = model
        self.client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))

    def reset(self):
        pass

    def _messages(self, messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": [{"text": m["text"]}]})
            elif m["role"] == "assistant":
                content = [{"text": m["text"]}] if m.get("text") else []
                for c in m.get("tool_calls", []):
                    content.append({"toolUse": {"toolUseId": c["id"], "name": c["name"], "input": c["input"]}})
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "user", "content": [
                    {"toolResult": {
                        "toolUseId": r["id"],
                        "content": [{"text": r["content"]}],
                        "status": "error" if r["is_error"] else "success",
                    }} for r in m["results"]
                ]})
        return out

    def complete(self, system, messages, tools):
        inference = {"maxTokens": 1024}
        if _temperature() is not None:
            inference["temperature"] = _temperature()
        resp = self.client.converse(
            modelId=self.model,
            system=[{"text": system}],
            messages=self._messages(messages),
            toolConfig={"tools": [
                {"toolSpec": {"name": t.name, "description": t.description, "inputSchema": {"json": t.input_schema}}}
                for t in tools
            ]},
            inferenceConfig=inference,
        )
        text, calls = [], []
        for block in resp["output"]["message"]["content"]:
            if "text" in block:
                text.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                calls.append({"id": tu["toolUseId"], "name": tu["name"], "input": tu.get("input", {})})
        usage = resp.get("usage", {})
        return ModelResponse("\n".join(text), calls, usage.get("inputTokens", 0),
                             usage.get("outputTokens", 0), resp.get("stopReason", ""))


# ---------------------------------------------------------------- Anthropic API (optional)

class AnthropicProvider:
    system_name = "anthropic"

    def __init__(self, model):
        import anthropic
        self.model = model or "claude-haiku-5-5"
        self.client = anthropic.Anthropic()

    def reset(self):
        pass

    def _messages(self, messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                content = [{"type": "text", "text": m["text"]}] if m.get("text") else []
                for c in m.get("tool_calls", []):
                    content.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]})
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], "is_error": r["is_error"]}
                    for r in m["results"]
                ]})
        return out

    def complete(self, system, messages, tools):
        kwargs = {}
        if _temperature() is not None:
            kwargs["temperature"] = _temperature()
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=self._messages(messages),
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            **kwargs,
        )
        text, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                calls.append({"id": block.id, "name": block.name, "input": block.input})
        return ModelResponse("\n".join(text), calls, resp.usage.input_tokens,
                             resp.usage.output_tokens, resp.stop_reason or "")


# ---------------------------------------------------------------- Mock

class MockProvider:
    """Scripted, deterministic agent for testing the harness without an API.

    It trusts whatever the tools return, follows a cursor if one is visible,
    and retries a flagged retryable error. Mock results are NOT findings.
    """
    system_name = "mock"

    def __init__(self, model=None):
        self.model = "mock-trusting-agent"
        self.reset()

    def reset(self):
        self.n = 0
        self.customer_id = None
        self.amounts = []
        self.retries = 0
        self.give_up = False

    def _call(self, name, inp):
        self.n += 1
        return ModelResponse("", [{"id": f"mock-{self.n}", "name": name, "input": inp}], stop_reason="tool_use")

    def _final(self):
        if self.give_up:
            return ModelResponse("I could not get reliable data.\nFINAL: UNKNOWN", stop_reason="end_turn")
        total = round(sum(self.amounts), 2)
        return ModelResponse(f"The total of open orders is ${total:,.2f}.\nFINAL: ${total:,.2f}", stop_reason="end_turn")

    def complete(self, system, messages, tools):
        last = messages[-1]
        if last["role"] == "user":
            m = re.search(r"for (.+?)\?", last["text"])
            return self._call("lookup_customer", {"name": m.group(1) if m else ""})

        prev_call = messages[-2]["tool_calls"][0]
        res = last["results"][0]
        try:
            body = json.loads(res["content"])
        except ValueError:
            body = res["content"]

        if prev_call["name"] == "lookup_customer":
            if isinstance(body, dict) and body.get("status") == "error":
                self.give_up = True
                return self._final()
            cust = body.get("customer", body) if isinstance(body, dict) else None
            if not cust or "id" not in cust:
                self.give_up = True
                return self._final()
            self.customer_id = cust["id"]
            return self._call("list_open_orders", {"customer_id": self.customer_id})

        if res["is_error"]:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            if (err.get("retryable") or not err) and self.retries < 2:
                self.retries += 1
                return self._call("list_open_orders", dict(prev_call["input"]))
            self.give_up = True
            return self._final()
        if isinstance(body, list):  # v1 shape
            self.amounts += [o["amount"] for o in body]
            return self._final()
        if isinstance(body, dict):  # v2 shape
            self.amounts += [o["amount_usd"] for o in body["orders"]]
            if body.get("has_more"):
                return self._call("list_open_orders", {"customer_id": self.customer_id, "cursor": body["next_cursor"]})
            return self._final()
        return self._final()  # unflagged plain text such as "Error: 429 ..."


PROVIDERS = {"bedrock": BedrockProvider, "anthropic": AnthropicProvider, "mock": MockProvider}
