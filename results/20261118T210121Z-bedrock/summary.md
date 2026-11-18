# Agent tool-call reliability: results

Provider `bedrock`, model `us.anthropic.claude-sonnet-4-6`, 3 runs per cell.

## Headline

- **v1**: 6 faults injected. 5 never produced an ERROR span. 5 caused at least one incorrect run, and 4 of those did so with every span healthy. Fault runs correct: 3 of 18. Silent failures (wrong answer, all spans OK): 12.
- **v2**: 6 faults injected. 3 never produced an ERROR span. 0 caused at least one incorrect run, and 0 of those did so with every span healthy. Fault runs correct: 18 of 18. Silent failures (wrong answer, all spans OK): 0.

## By fault

| Tools | Tracing | Fault | Correct | Wrong | Honest fail | No answer | Runs with ERROR span | Silent failures |
|---|---|---|---|---|---|---|---|---|
| v1 | standard | none | 3/3 | 0 | 0 | 0 | 0/3 | 0 |
| v1 | standard | timeout | 0/3 | 3 | 0 | 0 | 0/3 | 3 |
| v1 | standard | rate_limited | 3/3 | 0 | 0 | 0 | 0/3 | 0 |
| v1 | standard | truncation | 0/3 | 3 | 0 | 0 | 0/3 | 3 |
| v1 | standard | unit_drift | 0/3 | 3 | 0 | 0 | 0/3 | 3 |
| v1 | standard | ambiguous_match | 0/3 | 3 | 0 | 0 | 0/3 | 3 |
| v1 | standard | crash | 0/3 | 0 | 3 | 0 | 3/3 | 0 |
| v2 | contract | none | 3/3 | 0 | 0 | 0 | 0/3 | 0 |
| v2 | contract | timeout | 3/3 | 0 | 0 | 0 | 3/3 | 0 |
| v2 | contract | rate_limited | 3/3 | 0 | 0 | 0 | 3/3 | 0 |
| v2 | contract | truncation | 3/3 | 0 | 0 | 0 | 0/3 | 0 |
| v2 | contract | unit_drift | 3/3 | 0 | 0 | 0 | 0/3 | 0 |
| v2 | contract | ambiguous_match | 3/3 | 0 | 0 | 0 | 0/3 | 0 |
| v2 | contract | crash | 3/3 | 0 | 0 | 0 | 3/3 | 0 |

Silent failure = the agent returned a wrong number and every span in the trace had status OK.
