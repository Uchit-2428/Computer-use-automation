# Agent-facing invocation (stretch goal)

`evidence/catalog.json` is the tool catalog an agent sees (`python -m cua catalog`): approved capabilities only,
typed arguments, declared outputs and business outcomes. No steps, locators or UI details.

An agent invokes a capability by tool name with typed args:

```
python -m cua invoke member__get_savings_balance --args '{"tenant":"acme","member_id":"100871"}'
```

returned `status: succeeded` with `outputs.primary_savings_balance` (value returned to the caller only; the persisted
evidence stores a fingerprint). Run evidence: [`runs/replay-20260924-162142-a92b`](runs/replay-20260924-162142-a92b/).

`python -m cua ask "..."` wraps the same catalog in a model tool-use loop (Anthropic or Gemini). It was not run for this
evidence set because Google's API is unreachable from the environment that produced the replay evidence; the code path is
`cua/catalog.py::ask`.
