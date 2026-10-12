"""What an AI model may see and do on the farm, independent of how the model is connected.

    tools.py       the tool registry (name, description, JSON Schema, class, summary, call),
                   `invoke()` (the one gate every caller goes through) and `to_openai_tools()`
    policy.py      the operator's policy (ai.* settings) and the decision for one tool
    approvals.py   the daemon's queue of Yes/No requests
    safety.py      redaction, and how text that came from a device is marked
    schema.py      the small JSON Schema subset the tools' inputs are checked against

`superfarm.mcp` serves this registry to MCP clients; an assistant running inside the farm
calls `tools.invoke()` directly. Both are gated, approved and audited the same way.
"""
