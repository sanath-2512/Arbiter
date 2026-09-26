"""Helpers for scripted policies (dev only). A policy is stateless: it derives its position from
the conversation it is shown (a new attempt starts a new conversation)."""


def tc(name, **arguments):
    return {"name": name, "arguments": arguments}


def step(messages):
    return sum(1 for m in messages if m.get("role") == "assistant")


def last_user(messages):
    users = [m for m in messages if m.get("role") == "user"]
    return str(users[-1].get("content") or "") if users else ""


def first_user(messages):
    users = [m for m in messages if m.get("role") == "user"]
    return str(users[0].get("content") or "") if users else ""


def tool_results(messages):
    return [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]


def script(turns):
    """A fixed sequence of tool calls, one per model turn; submits when exhausted."""
    def respond(messages, tools):
        i = step(messages)
        if i < len(turns):
            return {"content": f"step {i + 1}", "tool_calls": [turns[i]]}
        return {"content": "done", "tool_calls": [tc("submit", summary="done")]}
    return respond
