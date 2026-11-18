# Agent tool-call reliability

**Question:** When an agent's tools fail, does the failure show up in telemetry?

Standard tracing marks a span as failed when code throws. Most tool failures in agent systems do not throw. A timeout gets swallowed into an empty list, a rate limit comes back as ordinary text, a page size changes, a unit changes, a search ranks the wrong record first. The tool call "succeeds", the span is green, and the agent confidently returns a wrong answer.

This lab injects six realistic tool failures into a simple agent, runs it against a real model on Amazon Bedrock, and checks every answer against ground truth. It compares two tool designs over the same backend:

- **v1 (naive):** patterns common in real agent code. Errors swallowed or returned as text, only the top search match, pagination and units dropped.
- **v2 (contract):** every result carries a status; errors have a type, message and retryable flag and are flagged to both the model and the trace; search refuses to guess on ambiguity; pagination and units are explicit.

## The task

> What is the total value in US dollars of all open orders for Acme Corp?

Ground truth is **$7,028.00** (seven open orders). The evaluation layer parses the agent's `FINAL:` line and scores each run as `correct`, `wrong`, `honest_failure` (the agent said it could not tell) or `no_answer`.

## Injected faults

| Fault | What the backend does | Wrong answer it invites |
|---|---|---|
| `timeout` | First orders call times out | $0.00 |
| `rate_limited` | First orders call returns 429 | $0.00 or a guess |
| `truncation` | Page size drops from 50 to 5 | $5,078.50 |
| `unit_drift` | Amounts switch to cents, flagged only in the response envelope | $702,800.00 |
| `ambiguous_match` | Search ranks "Acme Corporation (Canada)" first | $900.25 |
| `crash` | First orders call raises an unexpected exception | depends on retry |

Each fault is also run against a no-fault baseline.

## Telemetry

Spans follow the OpenTelemetry GenAI semantic conventions:

~~
invoke_agent orders-assistant   lab.fault, lab.tool_version, lab.eval.outcome
  chat <model>                  gen_ai.usage.input_tokens / output_tokens
  execute_tool <tool>           gen_ai.tool.name, error.type
~~

- **standard** tracing (paired with v1): span status is ERROR only when a tool raises.
- **contract** tracing (paired with v2): span status is also ERROR when a tool returns a flagged error.

The evaluation result is written onto the root span as `lab.eval.outcome`, so you can search traces for runs where the outcome was wrong but no span failed.

**Silent failure** = the agent returned a wrong number and every span in the trace had status OK.

## Results

Model: Claude Sonnet 4.6 on Amazon Bedrock (`us.anthropic.claude-sonnet-4-6`), 3 runs per cell, 42 runs total. Raw data: [`results/20261118T210121Z-bedrock`](results/20261118T210121Z-bedrock).

**Five of six injected faults produced fully healthy traces with v1 tools. Four of them led to a confidently wrong answer in every run (12 of 12), one of them off by 100x. With v2 contract tools, all 18 fault runs were correct.**

| Fault | v1 answer (all 3 runs) | v1 ERROR span | v2 result | v2 ERROR span |
|---|---|---|---|---|
| none (baseline) | $7,028.00 correct | no | 3/3 correct | no |
| timeout | $0.00 | no | 3/3 correct (retried) | yes |
| rate_limited | $7,028.00 correct (model read the error text and retried) | no | 3/3 correct | yes |
| truncation | $5,078.50 | no | 3/3 correct (followed next_cursor) | no, handled by contract |
| unit_drift | $702,800.00 | no | 3/3 correct (units normalized) | no, handled by contract |
| ambiguous_match | $900.25 (wrong customer) | no | 3/3 correct (exact match) | no, handled by contract |
| crash | said it could not tell | yes | 3/3 correct (retried) | yes |

Fault runs correct: **v1 3 of 18, v2 18 of 18.** Silent failures (wrong answer, every span OK): **v1 12, v2 0.**

What the data says beyond the headline:

1. **A healthy span says nothing about the answer, in either direction.** v1 recovered from the rate limit only because the model happened to read the error text; the trace looked identical to the four runs that returned wrong numbers.
2. **Making a failure visible did not fix it.** The v1 crash was the one fault standard tracing caught, and the agent still gave up, because nothing told it the error was retryable. v2's typed `retryable: true` turned the same fault into a correct answer.
3. **The model was not the constraint.** The same model went from 3 of 18 to 18 of 18 with no prompt change. Only the tool contracts changed.

The evaluation layer is what exposed all of this: `lab.eval.outcome` on the root span is the only signal that separates a silent failure from a success.

## Run it yourself

Requirements: Python 3.10+, AWS credentials with Bedrock access to an Anthropic model, Docker (optional, for Jaeger).

~~bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# optional: trace viewer at http://localhost:16686
docker run -d --name jaeger -e COLLECTOR_OTLP_ENABLED=true \
  -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

# harness check, no API calls (mock results are not findings)
python run_lab.py --provider mock --runs 1

# the lab
export AWS_REGION=us-east-1
python run_lab.py --provider bedrock --model us.anthropic.claude-sonnet-4-6 --runs 3
~~

Each run writes `results/<timestamp>-<provider>/` containing `runs.jsonl` (every run with full transcript), `spans.jsonl` (every span), `summary.json` and `summary.md`.

Useful flags: `--faults timeout,truncation`, `--versions v1`, `--instrumentation standard` (run v2 tools with standard tracing to separate the two effects), `--sleep 1` to slow down for rate limits, `LAB_TEMPERATURE=0` for less variance.

## Layout

~~
lab/backend.py     fake orders service, data, faults, ground truth
lab/tools.py       v1 naive and v2 contract tool designs
lab/agent.py       agent loop, GenAI spans, evaluation layer
lab/providers.py   Bedrock, Anthropic API, and mock providers
lab/telemetry.py   OpenTelemetry setup and span capture
run_lab.py         runs the matrix and writes results
~~

## Limits

One task, one domain, small run counts. Model behavior varies between runs and model versions, so rerun before relying on any number here. The v2 tools change several things at once (error flags, pagination, units, ambiguity handling); use `--instrumentation` and `--faults` to isolate effects.
