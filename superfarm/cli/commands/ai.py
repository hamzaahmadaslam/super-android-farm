"""The in-farm AI from the terminal: `sf ask`, `sf ai chat`, `sf ai models`, `sf ai pilot`.

    sf ask "how many Androids are running?"     one question to the built-in Assistant
    sf ai chat                                  a conversation
    sf ai models [ls | test NAME | add ... | key NAME | rm NAME]
    sf ai pilot "<goal>" <selector> --app PKG   teach a goal with a model, as recordings
    sf ai pilot sessions | show ID | cancel ID | resolve ID ANDROID ACTION | confirm ID ANDROID | train DATASET

What the Assistant wants to change follows ai.policy exactly as for any other AI caller:
under `ask` each changing action is shown here and waits for y or n (or is answered in
the panel). There is deliberately no flag that approves everything in advance.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from ...util.files import safe_filename, write_secret
from ..client import ApiError, CliError
from ..common import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Ctx, one_id, wait_for
from ..render import duration

API = "/api/ai"
PILOT_WORDS = ("sessions", "show", "cancel", "resolve", "confirm", "train")


# --------------------------------------------------------------------------- models

def models_ls(ctx: Ctx) -> int:
    response = ctx.api.get(f"{API}/models")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    rows = [(m["name"] + (" *" if m.get("default") else ""), m["provider"], m["base_url"], m.get("model") or "(first listed)",
             "yes" if m.get("tools") else "no", "yes" if m.get("vision") else "no",
             "-" if m.get("key_set") is None else ("stored" if m["key_set"] else "MISSING"))
            for m in response.get("models") or []]
    ctx.out.table(("PROFILE", "FORMAT", "SERVER", "MODEL", "TOOLS", "VISION", "KEY"), rows,
                  empty="No model is configured. Add one with: sf ai models add local --provider ollama --model <name>")
    if rows:
        ctx.out.note("* = default.  Check one for real with: sf ai models test <profile>")
    return EXIT_OK


def models_test(ctx: Ctx) -> int:
    ctx.out.status(f"testing {ctx.args.name} (a model that is not loaded yet can take a minute)")
    response = ctx.api.request("POST", f"{API}/models/{ctx.args.name}/test", timeout=900)
    ctx.out.clear_status()
    probe = response["probe"]
    if ctx.out.json_mode:
        ctx.out.json(response)
    else:
        ctx.out.table(("CHECK", "RESULT", "TIME", "DETAIL"),
                      [(c["id"], "ok" if c["ok"] else "failed", f"{c['ms'] / 1000:.1f}s", c["detail"]) for c in probe["checks"]],
                      state_columns=(1,))
        ctx.out.print(f"{probe['name']}: " + ("ready" if probe["ok"] else "NOT ready")
                      + f"  ({probe['provider']} {probe['model'] or '?'} at {probe['base_url']})")
    return EXIT_OK if probe["ok"] else EXIT_FAILED


def models_add(ctx: Ctx) -> int:
    a = ctx.args
    profile: dict[str, Any] = {"provider": a.provider, "model": a.model or ""}
    if a.base_url:
        profile["base_url"] = a.base_url
    if a.key_name:
        profile["api_key_secret"] = a.key_name
    if a.vision:
        profile["vision"] = True
    if a.no_tools:
        profile["tools"] = False
    if a.timeout:
        profile["timeout"] = a.timeout
    patch: dict[str, Any] = {"ai": {"models": {a.name: profile}}}
    if a.default:
        patch["ai"]["default_model"] = a.name
    response = ctx.api.patch("/api/config", {"patch": patch})
    if ctx.out.json_mode:
        ctx.out.json(response)
    else:
        ctx.out.note(f"profile {a.name} saved" + (" and made the default" if a.default else "")
                     + (f".  Store its key with: sf ai models key {a.name}" if a.key_name else "")
                     + f".  Test it with: sf ai models test {a.name}")
    return EXIT_OK


def models_rm(ctx: Ctx) -> int:
    response = ctx.api.patch("/api/config", {"patch": {"ai": {"models": {ctx.args.name: None}}}})
    if ctx.out.json_mode:
        ctx.out.json(response)
    else:
        ctx.out.note(f"profile {ctx.args.name} removed")
    return EXIT_OK


def models_key(ctx: Ctx) -> int:
    """Store a profile's API key in <home>/secrets. Local on purpose: the key is typed
    here and written to a file only its owner can read; it never crosses the API."""
    name = ctx.args.name
    profile = ctx.config.ai.models.get(name)
    secret = ctx.config.automation.llm.api_key_secret if name == "automation" else (profile.api_key_secret if profile else None)
    if secret is None:
        raise CliError(f"there is no model profile {name!r} (see: sf ai models)", EXIT_USAGE)
    if not secret:
        raise CliError(f"profile {name!r} names no secret. Set one first, e.g.: "
                       f"sf config set ai.models.{name}.api_key_secret {name}-key", EXIT_USAGE)
    if sys.stdin.isatty():
        key = getpass.getpass(f"API key for {name} (not shown): ")
    else:
        key = sys.stdin.readline()
    key = key.strip()
    if not key:
        raise CliError("no key given; nothing was changed", EXIT_USAGE)
    ctx.paths.ensure()
    write_secret(ctx.paths.secrets / safe_filename(secret), key + "\n")
    ctx.out.note(f"key stored as secret {secret!r} (owner-only file). Test it with: sf ai models test {name}")
    return EXIT_OK


# ------------------------------------------------------------------------ assistant

def _approve_inline(ctx: Ctx, event: dict) -> None:
    """Ask y/n for one pending approval when a person is at the terminal."""
    err = ctx.out.stderr
    approval_id = event.get("approval_id")
    if not sys.stdin.isatty():
        err.write(f"  waiting for approval {approval_id}: answer in the panel or with `sf approvals approve {approval_id}`\n")
        return
    ctx.out.clear_status()
    err.write(f"  The assistant asks to: {event.get('summary')}\n  Approve? [y]es / [n]o / [l]ater: ")
    err.flush()
    answer = sys.stdin.readline().strip().lower()
    try:
        if answer in ("y", "yes"):
            ctx.api.post(f"/api/approvals/{approval_id}/approve", {"via": "terminal"})
        elif answer in ("n", "no"):
            ctx.api.post(f"/api/approvals/{approval_id}/deny", {"via": "terminal", "reason": ""})
        else:
            err.write(f"  left open: answer in the panel or with `sf approvals approve {approval_id}`\n")
    except ApiError as e:
        if e.status not in (404, 409):
            raise
        err.write(f"  no longer open: {e.message}\n")


def _show(ctx: Ctx, event: dict) -> None:
    out, kind = ctx.out, event.get("type")
    if kind == "assistant":
        out.note(f"  {event.get('text')}")
    elif kind == "tool_call":
        out.note(f"  -> {event.get('tool')}: {event.get('summary') or ''}".rstrip(": "))
    elif kind == "tool_result":
        first = str(event.get("text") or "").splitlines()[0:1]
        out.note(f"     {'ok' if event.get('ok') else 'FAILED (' + str(event.get('status')) + ')'}: {(first or [''])[0][:160]}")
    elif kind == "approval":
        _approve_inline(ctx, event)
    elif kind == "notice":
        out.note(f"  ! {event.get('text')}")
    elif kind == "final":
        out.clear_status()
        if event.get("text"):
            out.print(str(event["text"]))
        out.note(str(event.get("record") or ""))


def _exchange(ctx: Ctx, conversation: str, text: str, after: int) -> tuple[int, dict | None]:
    """Send one message and print what happens until the answer. Returns the number of
    the last event seen and the final event."""
    ctx.api.post(f"{API}/conversations/{conversation}/messages", {"text": text})
    final = None
    try:
        while final is None:
            view = ctx.api.get(f"{API}/conversations/{conversation}", after=after)["conversation"]
            for event in view["events"]:
                after = max(after, int(event["n"]))
                if event.get("type") == "user":
                    continue
                if not ctx.out.json_mode:
                    _show(ctx, event)
                if event.get("type") == "final":
                    final = event
            if final is None:
                tool = view.get("running_tool") or {}
                ctx.out.status("waiting for your answer" if view.get("pending_approvals") else
                               f"running {tool.get('tool')}" if tool else "thinking")
                ctx.sleep(0.4)
    except KeyboardInterrupt:
        ctx.out.clear_status()
        ctx.api.post(f"{API}/conversations/{conversation}/cancel")
        raise
    ctx.out.clear_status()
    return after, final


def ask(ctx: Ctx) -> int:
    text = " ".join(ctx.args.request).strip()
    if not text:
        raise CliError("what should the assistant do? e.g.: sf ask \"how many Androids are running?\"", EXIT_USAGE)
    conversation = ctx.api.post(f"{API}/conversations", {"model": ctx.args.model} if ctx.args.model else {})["conversation"]
    _, final = _exchange(ctx, conversation["id"], text, 0)
    if ctx.out.json_mode:
        ctx.out.json({"conversation": conversation["id"], "final": final})
    if not ctx.args.keep:
        ctx.api.delete(f"{API}/conversations/{conversation['id']}")
    return EXIT_FAILED if final is None or (final.get("ended") and not final.get("text")) else EXIT_OK


def chat(ctx: Ctx) -> int:
    if not sys.stdin.isatty():
        raise CliError("sf ai chat is a conversation and needs a terminal; for one question use: sf ask \"...\"", EXIT_USAGE)
    conversation = ctx.api.post(f"{API}/conversations", {"model": ctx.args.model} if ctx.args.model else {})["conversation"]
    ctx.out.note(f"Assistant on model profile {conversation['model']}. An empty line or Ctrl-D ends the conversation.")
    after = 0
    while True:
        ctx.out.stderr.write("\nyou> ")
        ctx.out.stderr.flush()
        line = sys.stdin.readline()
        if not line.strip():
            break
        try:
            after, _ = _exchange(ctx, conversation["id"], line.strip(), after)
        except KeyboardInterrupt:
            ctx.out.note("cancelled")
    return EXIT_OK


def conversations(ctx: Ctx) -> int:
    response = ctx.api.get(f"{API}/conversations")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    ctx.out.table(("ID", "STATE", "MODEL", "EVENTS", "TITLE"),
                  [(c["id"], c["state"], c["model"], c["events"], c["title"]) for c in response.get("conversations") or []],
                  right=(3,), state_columns=(1,), empty="No conversations. Start one with: sf ask \"...\"  or  sf ai chat")
    return EXIT_OK


# ---------------------------------------------------------------------------- pilot

def _device_rows(session: dict) -> list[tuple]:
    rows = []
    for key, device in sorted((session.get("devices") or {}).items(), key=lambda kv: int(kv[0])):
        what = device.get("message") or device.get("error") or device.get("step_label") or ""
        rows.append((key, device.get("state"), f"{device.get('run', 0)}/{device.get('runs', 1)}",
                     f"{device.get('step', 0)}/{device.get('steps', 0)}", device.get("model_calls", 0), what))
    return rows


def _pilot_report(ctx: Ctx, session: dict) -> None:
    out = ctx.out
    out.pairs((("session", session["id"]), ("goal", session["goal"]), ("app", session["package"]),
               ("dataset", session["dataset"]), ("model", session["model"]),
               ("success check", (session.get("success") or {}).get("describe") or "none: runs can only be self-checked"),
               ("state", session["state"])))
    out.table(("ANDROID", "STATE", "RUN", "STEP", "MODEL CALLS", "DETAIL"), _device_rows(session), right=(0, 4),
              state_columns=(1,))
    for key, device in sorted((session.get("devices") or {}).items(), key=lambda kv: int(kv[0])):
        for result in device.get("results") or []:
            out.print(f"  Android {key}: {result.get('state')}  {result.get('step', 0)} steps, "
                      f"{result.get('model_calls', 0)} model calls, {duration(result.get('elapsed') or 0)}"
                      + (f", recording {result['recording']}" if result.get("recording") else "")
                      + (f"  - {result.get('message')}" if result.get("message") else ""))
    if session.get("unverified"):
        out.note("unverified runs are not used for training until you confirm them: "
                 f"sf ai pilot confirm {session['id']} <android>")
    if session.get("waiting"):
        out.note(f"waiting for you on Android {', '.join(str(i) for i in session['waiting'])}: "
                 f"sf ai pilot resolve {session['id']} <android> resume|skip|abort")


def _pilot_start(ctx: Ctx, goal: str, selector: str) -> int:
    a = ctx.args
    if not a.app:
        raise CliError("which app? add --app <package>, e.g. --app com.android.settings", EXIT_USAGE)
    body: dict[str, Any] = {"goal": goal, "package": a.app, "selector": selector, "runs": a.runs}
    for key, value in (("model", a.model), ("success", a.success), ("max_steps", a.max_steps), ("dataset", a.dataset)):
        if value:
            body[key] = value
    values = dict(_pair(v) for v in a.value)
    for name in a.ask_value:
        # Typed here, not on the command line: arguments are visible to every user of the machine.
        values[name] = getpass.getpass(f"value for {name} (not shown): ")
    if values:
        body["values"] = values
    task = ctx.api.post(f"{API}/pilot", body)["task"]
    if not ctx.wait:
        if ctx.out.json_mode:
            ctx.out.json({"task": task})
        else:
            ctx.out.print(str(task["id"]))
            ctx.out.note(f"pilot session {task['id']} started; watch it with: sf ai pilot show {task['id']}")
        return EXIT_OK
    task = wait_for(ctx, task)
    session = ctx.api.get(f"{API}/pilot/sessions/{task['id']}")["session"]
    output = task.get("output") or {}
    if a.train and output.get("verified"):
        trained = wait_for(ctx, ctx.api.post(f"{API}/pilot/{session['dataset']}/train")["task"])
        if ctx.out.json_mode:
            ctx.out.json({"session": session, "train": trained})
            return EXIT_OK if trained.get("state") == "done" else EXIT_FAILED
        _pilot_report(ctx, session)
        _train_report(ctx, trained)
        return EXIT_OK if trained.get("state") == "done" else EXIT_FAILED
    if ctx.out.json_mode:
        ctx.out.json({"session": session, "task": task})
    else:
        _pilot_report(ctx, session)
        if output.get("verified"):
            ctx.out.note(f"train the recordings with: sf ai pilot train {session['dataset']}")
    return EXIT_OK if output.get("verified") else EXIT_FAILED


def _pair(text: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name:
        raise CliError(f"--value takes NAME=VALUE, not {text!r}", EXIT_USAGE)
    return name, value


def _train_report(ctx: Ctx, task: dict) -> None:
    if task.get("state") != "done":
        ctx.out.error(f"training failed: {task.get('error') or task.get('state')}")
        return
    output = task.get("output") or {}
    summary, program = output.get("summary") or {}, output.get("program") or {}
    ctx.out.print(f"program {program.get('slug')}: {program.get('steps')} step(s)")
    for line in summary.get("text") or []:
        ctx.out.print(f"  {line}")
    for warning in summary.get("warnings") or []:
        ctx.out.print(f"  ! {warning}")
    ctx.out.note(f"replay it without any model: sf auto run program {program.get('slug')} <selector>")


def pilot(ctx: Ctx) -> int:
    """One command, two shapes: a goal to start a session, or a word to manage sessions."""
    words = list(ctx.args.words)
    head = words[0]
    if head not in PILOT_WORDS:
        if len(words) != 2:
            raise CliError("usage: sf ai pilot \"<goal>\" <selector> --app <package>   (the goal in quotes)", EXIT_USAGE)
        return _pilot_start(ctx, words[0], words[1])
    rest = words[1:]

    def need(count: int, usage: str) -> None:
        if len(rest) != count:
            raise CliError(f"usage: sf ai pilot {usage}", EXIT_USAGE)

    if head == "sessions":
        need(0, "sessions")
        response = ctx.api.get(f"{API}/pilot/sessions")
        if ctx.out.json_mode:
            ctx.out.json(response)
            return EXIT_OK
        ctx.out.table(("SESSION", "STATE", "DATASET", "RUNS", "VERIFIED", "GOAL"),
                      [(s["id"], s["state"], s["dataset"], sum((s.get("counts") or {}).values()),
                        len(s.get("recordings") or []), s["goal"]) for s in response.get("sessions") or []],
                      right=(3, 4), state_columns=(1,),
                      empty="No pilot sessions since the daemon started. Start one with: sf ai pilot \"<goal>\" <selector> --app <package>")
        return EXIT_OK
    if head == "show":
        need(1, "show <session>")
        session = ctx.api.get(f"{API}/pilot/sessions/{rest[0]}")["session"]
        if ctx.out.json_mode:
            ctx.out.json({"session": session})
            return EXIT_OK
        _pilot_report(ctx, session)
        if ctx.args.steps:
            for key, device in sorted((session.get("devices") or {}).items(), key=lambda kv: int(kv[0])):
                for action in device.get("actions") or []:
                    ctx.out.print(f"  Android {key} step {action['n']}: {action['action']} {action['target']} "
                                  f"-> {action['outcome'] or '...'}   ({action['reason']})")
        return EXIT_OK
    if head == "cancel":
        need(1, "cancel <session>")
        response = ctx.api.post(f"{API}/pilot/sessions/{rest[0]}/cancel")
        if ctx.out.json_mode:
            ctx.out.json(response)
        else:
            ctx.out.note("cancelling" if response.get("cancelled") else "that session had already ended")
        return EXIT_OK
    if head == "resolve":
        need(3, "resolve <session> <android> resume|skip|abort")
        response = ctx.api.post(f"{API}/pilot/sessions/{rest[0]}/devices/{one_id(rest[1])}/resolve", {"action": rest[2]})
        return ctx.finish(response, done=f"Android {rest[1]}: {rest[2]}")
    if head == "confirm":
        need(2, "confirm <session> <android>")
        response = ctx.api.post(f"{API}/pilot/sessions/{rest[0]}/confirm", {"instance": one_id(rest[1])})
        return ctx.finish(response, done=f"recording {response['confirmed']['recording']} now counts for training")
    need(1, "train <dataset>")
    task = ctx.api.post(f"{API}/pilot/{rest[0]}/train")["task"]
    if not ctx.wait:
        return ctx.finish({"task": task})
    task = wait_for(ctx, task)
    if ctx.out.json_mode:
        ctx.out.json({"task": task})
    else:
        _train_report(ctx, task)
    return EXIT_OK if task.get("state") == "done" else EXIT_FAILED


# ------------------------------------------------------------------------- register

def register(b: Any, sub: argparse._SubParsersAction) -> None:
    p = b.add(sub, "ask", ask, "ask the built-in Assistant to look at or operate the farm",
              "sf ask \"how many Androids are running, and how many can this machine run?\"\n"
              "sf ask --model local \"start every Android tagged eu\"",
              description="Send one request to the farm's built-in Assistant and print its steps and its answer. "
                          "Actions that change the farm follow ai.policy: under `ask` each one is shown and waits for "
                          "y or n here (or in the panel). The last line is the farm's own record of what ran.")
    p.add_argument("request", nargs="+", help="what you want, in plain words (quote it)")
    p.add_argument("--model", default=None, help="model profile to use (default: ai.default_model)")
    p.add_argument("--keep", action="store_true", help="keep the conversation afterwards (see: sf ai conversations)")

    ai = b.group(sub, "ai", "the in-farm AI: model profiles, the Assistant, the Pilot",
                 description="Model profiles (which model, where), the built-in Assistant, and the AI Pilot, which "
                             "turns a model operating an app into a program that replays without one. See docs/AI.md.")
    p = b.add(ai, "chat", chat, "talk with the built-in Assistant", "sf ai chat\nsf ai chat --model local")
    p.add_argument("--model", default=None, help="model profile to use (default: ai.default_model)")
    b.add(ai, "conversations", conversations, "list the Assistant's conversations", "sf ai conversations")

    models = b.group(ai, "models", "model profiles: where a model lives and what it can do", default=models_ls)
    b.add(models, "ls", models_ls, "list the configured profiles", "sf ai models ls")
    p = b.add(models, "test", models_test, "really test a profile: server, model, a reply, a tool call",
              "sf ai models test local")
    p.add_argument("name", help="profile name")
    p = b.add(models, "add", models_add, "add or replace a profile",
              "sf ai models add local --provider ollama --model <name> --default\n"
              "sf ai models add hosted --provider openai --base-url https://api.example.com --model <name> --key-name hosted-key")
    p.add_argument("name", help="profile name: lower-case letters, digits, - and _")
    p.add_argument("--provider", required=True, choices=("ollama", "openai"),
                   help="wire format: an Ollama server, or any OpenAI-compatible chat-completions API")
    p.add_argument("--base-url", default=None, help="server address (default: http://127.0.0.1:11434)")
    p.add_argument("--model", default=None, help="model name on that server (default: the first it lists)")
    p.add_argument("--key-name", default=None, help="NAME of the secret that will hold the API key (then: sf ai models key)")
    p.add_argument("--vision", action="store_true", help="the model reads pictures")
    p.add_argument("--no-tools", action="store_true", help="the model cannot call tools")
    p.add_argument("--timeout", type=int, default=None, help="seconds to wait for one answer (default: 120)")
    p.add_argument("--default", action="store_true", help="make this the default profile")
    p = b.add(models, "key", models_key, "store a profile's API key (typed here, kept in secrets, never shown)",
              "sf ai models key hosted")
    p.add_argument("name", help="profile name")
    p = b.add(models, "rm", models_rm, "remove a profile", "sf ai models rm hosted")
    p.add_argument("name", help="profile name")

    p = b.add(ai, "pilot", pilot, "teach the farm a task: a model operates the app, the runs become a program",
              "sf ai pilot \"open the date and time settings\" 1 --app com.android.settings --success \"text:Set time automatically\" --runs 3 --train\n"
              "sf ai pilot sessions\nsf ai pilot show t12 --steps\nsf ai pilot resolve t12 3 resume\n"
              "sf ai pilot confirm t12 3\nsf ai pilot train open-the-date-and-time-settings",
              description="With a goal: start a Pilot session on the selected Androids. A model operates the app one "
                          "action at a time and every action is recorded; a run counts only when the success check "
                          "really holds. Train the recordings (--train, or `sf ai pilot train`) into a program that "
                          "replays with no model: sf auto run program <dataset> <selector>.\n\n"
                          "With a word: sessions | show <session> | cancel <session> | resolve <session> <android> "
                          "resume|skip|abort | confirm <session> <android> | train <dataset>.")
    p.add_argument("words", nargs="+", metavar="GOAL SELECTOR | WORD ...",
                   help="\"<goal>\" <selector> to start a session, or: " + " | ".join(PILOT_WORDS))
    p.add_argument("--app", default=None, metavar="PACKAGE", help="the app to operate, e.g. com.android.settings")
    p.add_argument("--success", default=None, metavar="CHECK",
                   help="what must be on screen when the goal is reached: text:..., id:..., desc:..., package:... or "
                        "activity:... (without it a run can only be checked by the model's own evidence)")
    p.add_argument("--runs", type=int, default=1, help="runs per Android (default: 1; three or more give a real consensus)")
    p.add_argument("--model", default=None, help="model profile to use (default: ai.default_model)")
    p.add_argument("--max-steps", type=int, default=None, help="actions per run (default: ai.pilot.max_steps)")
    p.add_argument("--dataset", default=None, help="dataset to record into (default: named after the goal)")
    p.add_argument("--value", action="append", default=[], metavar="NAME=VALUE",
                   help="a value the Pilot may type by name (kept in memory for this session only)")
    p.add_argument("--ask-value", action="append", default=[], metavar="NAME",
                   help="like --value, but the value is asked for here without being shown (use it for secrets)")
    p.add_argument("--train", action="store_true", help="train the dataset when the session ends with verified runs")
    p.add_argument("--steps", action="store_true", help="with `show`: list every action and the model's reason for it")

