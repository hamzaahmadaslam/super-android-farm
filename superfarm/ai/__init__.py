"""What an AI model may see and do on the farm, independent of how the model is connected.

    tools.py       the tool registry (name, description, JSON Schema, class, summary, call),
                   `invoke()` (the one gate every caller goes through) and `to_openai_tools()`
    policy.py      the operator's policy (ai.* settings) and the decision for one tool
    approvals.py   the daemon's queue of Yes/No requests
    safety.py      redaction, and how text that came from a device is marked
    schema.py      the small JSON Schema subset the tools' inputs are checked against
    models.py      model profiles (ai.models) and the two wire formats; the one place a
                   model is called from, with `probe()` to really test a profile
    outbound.py    the last check on anything sent to a model: no stored secret leaves
    assistant.py   the built-in Assistant: a model operating the farm through `invoke()`
    pilot.py       the Pilot: a model operates an app, the runs are recorded and trained
                   into a program that replays without a model
    pilot_tools.py the Pilot as two tools, for the registry

`superfarm.mcp` serves this registry to MCP clients; an assistant running inside the farm
calls `tools.invoke()` directly. Both are gated, approved and audited the same way.
"""
