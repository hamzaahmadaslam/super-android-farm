"""The parser: every command parses, explains itself with an example, and completes."""
import argparse
import shutil
import subprocess

import pytest

from superfarm.cli import completion
from superfarm.cli.main import build_parser, main, split_rest

# One realistic invocation per command, and one fact about how it parsed.
SAMPLES: dict[str, tuple[str, dict]] = {
    "ls": ("ls -w --interval 5", {"watch": True, "interval": 5.0}),
    "create": ("create -n 5 --name shop --android 13 --gpu igpu --net vpn --vpn nl.ovpn --cpus 2.5 --memory 2048 "
               "--width 720 --height 1280 --dpi 320 --fps 30 --tag eu --tag shop --image custom:1",
               {"count": 5, "android": "13", "gpu": "igpu", "tag": ["eu", "shop"], "memory": 2048, "cpus": 2.5}),
    "start": ("start 1-10 --force", {"selector": "1-10", "force": True}),
    "restart": ("restart tag:eu", {"selector": "tag:eu", "force": False}),
    "recreate": ("recreate 3", {"selector": "3"}),
    "stop": ("stop all,!tag:keep", {"selector": "all,!tag:keep"}),
    "rm": ("rm 7 --keep-data --force --yes", {"selector": "7", "keep_data": True, "force": True, "yes": True}),
    "set": ("set 1-5 fps=30 network.mode=vpn", {"selector": "1-5", "assignments": ["fps=30", "network.mode=vpn"]}),
    "info": ("info 3 --device", {"id": "3", "device": True}),
    "logs": ("logs 3 -n 50 --logcat", {"id": "3", "lines": 50, "logcat": True}),
    "gpu ls": ("gpu ls", {}),
    "gpu set": ("gpu set 1-8 nvidia --node /dev/dri/renderD129", {"mode": "nvidia", "node": "/dev/dri/renderD129"}),
    "image ls": ("image ls", {}),
    "image pull": ("image pull 13", {"version": "13"}),
    "image build": ("image build 12", {"version": "12"}),
    "tasks": ("tasks t12 --cancel", {"id": "t12", "cancel": True}),
    "top": ("top --once --interval 1", {"once": True, "interval": 1.0}),
    "open": ("open", {}),
    "shell": ("shell all --root --timeout 5 --force", {"selector": "all", "root": True, "timeout": 5.0, "force": True}),
    "screenshot": ("screenshot 3 -o shot.png", {"id": "3", "output": "shot.png"}),
    "apps ls": ("apps ls", {}),
    "apps add": ("apps add a.apk b.xapk", {"files": ["a.apk", "b.xapk"]}),
    "apps rm": ("apps rm com.example.shop-42", {"app_id": "com.example.shop-42"}),
    "app install": ("app install 1-10 com.example.shop --no-grant --downgrade",
                    {"app": "com.example.shop", "no_grant": True, "downgrade": True}),
    "app ensure": ("app ensure all com.example.shop", {"selector": "all", "no_grant": False}),
    "app uninstall": ("app uninstall tag:t com.example.shop --keep-data", {"package": "com.example.shop", "keep_data": True}),
    "app launch": ("app launch all com.example.shop", {"package": "com.example.shop"}),
    "app stop": ("app stop all com.example.shop", {"package": "com.example.shop"}),
    "app clear": ("app clear 3 com.example.shop", {"selector": "3"}),
    "app matrix": ("app matrix", {"selector": "all"}),
    "push": ("push all notes.txt", {"local": "notes.txt", "remote": "/sdcard/Download/"}),
    "pull": ("pull 3 /sdcard/Download/r.pdf -o out/", {"id": "3", "remote": "/sdcard/Download/r.pdf", "output": "out/"}),
    "gallery": ("gallery 1-5 photo.jpg", {"selector": "1-5", "local": "photo.jpg"}),
    "input tap": ("input tap 1-10 0.5 0.9", {"x": 0.5, "y": 0.9, "pixels": False}),
    "input text": ("input text 3 hello", {"text": "hello"}),
    "input key": ("input key all HOME", {"key": "HOME"}),
    "window open": ("window open 3", {"selector": "3"}),
    "window grid": ("window grid 1-12", {"selector": "1-12"}),
    "window close": ("window close all", {"selector": "all"}),
    "vpn profiles": ("vpn profiles", {}),
    "vpn import": ("vpn import a.ovpn b.conf", {"files": ["a.ovpn", "b.conf"]}),
    "vpn rm": ("vpn rm a.ovpn", {"name": "a.ovpn"}),
    "vpn credentials": ("vpn credentials default --username someone --password-stdin",
                        {"name": "default", "username": "someone", "password_stdin": True, "delete": False}),
    "vpn status": ("vpn status --refresh", {"selector": "running", "refresh": True}),
    "gateway ls": ("gateway ls", {}),
    "gateway add": ("gateway add eu --profile nl.ovpn --provider wireguard --credentials main --subnet 10.213.5.0/24",
                    {"name": "eu", "profile": "nl.ovpn", "provider": "wireguard", "credentials": "main"}),
    "gateway rm": ("gateway rm eu --yes --force", {"name": "eu", "yes": True, "force": True}),
    "gateway up": ("gateway up eu", {"name": "eu"}),
    "gateway down": ("gateway down eu --force", {"name": "eu", "force": True}),
    "camera status": ("camera status", {}),
    "camera play": ("camera play 1-5 intro.mp4 --sound none", {"source": "intro.mp4", "sound": "none"}),
    "camera stop": ("camera stop", {"selector": None}),
    "camera mic": ("camera mic 3 solo", {"mode": "solo"}),
    "listen": ("listen off", {"target": "off"}),
    "auto datasets": ("auto datasets", {}),
    "auto train": ("auto train open-settings", {"dataset": "open-settings"}),
    "auto programs": ("auto programs", {}),
    "auto flows ls": ("auto flows ls", {}),
    "auto flows show": ("auto flows show examples/open-app", {"name": "examples/open-app"}),
    "auto flows validate": ("auto flows validate login.yaml", {"file": "login.yaml"}),
    "auto flows save": ("auto flows save login login.yaml", {"name": "login", "file": "login.yaml"}),
    "auto flows rm": ("auto flows rm login", {"name": "login"}),
    "auto run": ("auto run flow examples/open-app 1-10 --var package=com.android.settings --secret pin --dry-run",
                 {"kind": "flow", "var": ["package=com.android.settings"], "secret": ["pin"], "dry_run": True}),
    "auto runs": ("auto runs t12 --active", {"task": "t12", "active": True}),
    "auto resolve": ("auto resolve t12 4 skip", {"task": "t12", "id": "4", "action": "skip"}),
    "auto values ls": ("auto values ls --instance 3", {"instance": "3"}),
    "auto values set": ("auto values set city Berlin", {"name": "city", "value": "Berlin"}),
    "auto values rm": ("auto values rm city", {"name": "city"}),
    "auto values pool-add": ("auto values pool-add codes codes.txt", {"name": "codes", "file": "codes.txt"}),
    "auto values pool-rm": ("auto values pool-rm codes", {"name": "codes"}),
    "auto schedule ls": ("auto schedule ls", {}),
    "auto schedule add": ("auto schedule add nightly flow examples/open-app all --cron '0 3 * * *'",
                          {"name": "nightly", "kind": "flow", "cron": "0 3 * * *", "every": None}),
    "auto schedule rm": ("auto schedule rm s3", {"id": "s3"}),
    "auto schedule enable": ("auto schedule enable s3", {"id": "s3"}),
    "auto schedule disable": ("auto schedule disable s3", {"id": "s3"}),
    "backup create": ("backup create 1-5", {"selector": "1-5"}),
    "backup list": ("backup list --kind farm", {"kind": "farm"}),
    "backup verify": ("backup verify android-3/20250101-040000 --deep", {"deep": True}),
    "backup restore": ("backup restore android-3/20250101-040000 --over 3 --yes", {"over": "3", "yes": True}),
    "backup prune": ("backup prune", {}),
    "backup farm": ("backup farm --include-secrets", {"include_secrets": True}),
    "mcp config": ("mcp config --http --port 7712", {"http": True, "port": 7712}),
    "mcp tools": ("mcp tools --markdown", {"markdown": True}),
    "approvals ls": ("approvals ls --state pending -n 5", {"state": "pending", "limit": 5}),
    "approvals approve": ("approvals approve 3f9a1c2e --remember", {"id": "3f9a1c2e", "remember": True}),
    "approvals deny": ("approvals deny 3f9a1c2e --reason later", {"id": "3f9a1c2e", "reason": "later"}),
    "approvals watch": ("approvals watch", {}),
    "ask": ("ask --model local --keep how many are running", {"request": ["how", "many", "are", "running"],
                                                              "model": "local", "keep": True}),
    "ai chat": ("ai chat --model local", {"model": "local"}),
    "ai conversations": ("ai conversations", {}),
    "ai models ls": ("ai models ls", {}),
    "ai models test": ("ai models test local", {"name": "local"}),
    "ai models add": ("ai models add hosted --provider openai --base-url https://api.example.com --model big "
                      "--key-name hosted-key --vision --no-tools --timeout 60 --default",
                      {"name": "hosted", "provider": "openai", "base_url": "https://api.example.com", "model": "big",
                       "key_name": "hosted-key", "vision": True, "no_tools": True, "timeout": 60, "default": True}),
    "ai models key": ("ai models key hosted", {"name": "hosted"}),
    "ai models rm": ("ai models rm hosted", {"name": "hosted"}),
    "ai pilot": ("ai pilot goal 1-3 --app com.example.app --success text:Done --runs 3 --max-steps 12 --dataset d "
                 "--value city=Berlin --ask-value pin --model local --train --steps",
                 {"words": ["goal", "1-3"], "app": "com.example.app", "success": "text:Done", "runs": 3, "max_steps": 12,
                  "dataset": "d", "value": ["city=Berlin"], "ask_value": ["pin"], "model": "local", "train": True,
                  "steps": True}),
    "doctor": ("doctor", {}),
    "host plan": ("host plan --boot", {"boot": True}),
    "host apply": ("host apply --dry-run --boot", {"dry_run": True, "boot": True}),
    "host capacity": ("host capacity --workload light", {"workload": "light"}),
    "host gpus": ("host gpus", {}),
    "host sign-module": ("host sign-module", {}),
    "config get": ("config get android.fps", {"key": "android.fps"}),
    "config set": ("config set android.fps 25", {"key": "android.fps", "value": "25"}),
    "config unset": ("config unset android.fps", {"key": "android.fps"}),
    "config show": ("config show --changed", {"changed": True}),
    "config path": ("config path", {}),
    "config docs": ("config docs -o docs/CONFIGURATION.md", {"output": "docs/CONFIGURATION.md", "check": None}),
    "token": ("token --rotate", {"rotate": True}),
    "serve": ("serve --host 127.0.0.1 --port 7701", {"host": "127.0.0.1", "port": 7701}),
    "service install": ("service install", {}),
    "service start": ("service start", {}),
    "service stop": ("service stop", {}),
    "service status": ("service status", {}),
    "service logs": ("service logs -n 20 -f", {"lines": 20, "follow": True}),
    "version": ("version", {}),
    "completion": ("completion zsh", {"shell": "zsh"}),
}

# Addendum 5's command list, spelled out: nothing in the spec may be missing from the CLI.
SPEC_COMMANDS = """
doctor | host plan | host apply | host capacity | host gpus | host sign-module
config get | config set | config unset | config show | config path | config docs
token | serve | version | service install | service start | service stop | service status | service logs
ls | create | start | stop | restart | recreate | rm | set | info | logs | shell | screenshot
apps add | apps ls | apps rm
app install | app ensure | app uninstall | app launch | app stop | app clear | app matrix
push | pull | gallery
vpn profiles | vpn import | vpn rm | vpn credentials | vpn status
gateway ls | gateway add | gateway rm | gateway up | gateway down
gpu ls | gpu set | image ls | image pull | image build
camera status | camera play | camera stop | camera mic | listen
window open | window grid | window close | input tap | input text | input key
auto datasets | auto train | auto programs | auto flows ls | auto run | auto runs | auto resolve
auto values ls | auto schedule ls
backup create | backup list | backup verify | backup restore | backup prune | backup farm
tasks | top | open | completion
"""


def _sub(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def leaves(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> dict[str, argparse.ArgumentParser]:
    sub = _sub(parser)
    if sub is None:
        return {" ".join(path): parser}
    out: dict[str, argparse.ArgumentParser] = {}
    seen: set[int] = set()
    for name, child in sub.choices.items():
        if id(child) not in seen:                 # aliases point at the same parser
            seen.add(id(child))
            out.update(leaves(child, (*path, name)))
    return out


def groups(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> dict[str, argparse.ArgumentParser]:
    sub = _sub(parser)
    if sub is None:
        return {}
    out = {" ".join(path): parser}
    for name, child in sub.choices.items():
        out.update(groups(child, (*path, name)))
    return out


PARSER = build_parser()
LEAVES = leaves(PARSER)


def test_every_command_has_a_parse_sample():
    assert sorted(LEAVES) == sorted(SAMPLES)


def test_every_command_of_the_spec_exists():
    wanted = {c.strip() for line in SPEC_COMMANDS.strip().splitlines() for c in line.split("|")}
    assert wanted <= set(LEAVES), sorted(wanted - set(LEAVES))


@pytest.mark.parametrize("command", sorted(SAMPLES))
def test_command_parses(command):
    import shlex
    argv, facts = SAMPLES[command]
    args = build_parser().parse_args(shlex.split(argv))
    assert callable(args.func)
    assert args.parser.prog == f"sf {command}"
    for key, value in facts.items():
        assert getattr(args, key) == value, key


@pytest.mark.parametrize("command", sorted(LEAVES))
def test_every_command_explains_itself_with_one_real_example(command):
    parser = LEAVES[command]
    text = parser.format_help()             # also proves no help string has a stray % format
    assert parser.description and len(parser.description) > 10
    assert "example:" in text
    example_lines = [line.strip() for line in (parser.epilog or "").splitlines()[1:] if line.strip()]
    assert example_lines, "no example"
    # The example is for this very command, not a placeholder.
    assert any(f"sf {command}" in line for line in example_lines), example_lines
    for option in ("--home", "--json", "--no-wait", "--quiet"):
        assert option in text


@pytest.mark.parametrize("group", sorted(g for g in groups(PARSER) if g))
def test_group_help_lists_its_commands(group, capsys):
    with pytest.raises(SystemExit) as e:
        main([*group.split(), "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for name in _sub(groups(PARSER)[group]).choices:
        assert name in out


def test_examples_in_help_actually_parse():
    """An example that the parser itself rejects would teach the user a wrong command."""
    import shlex
    for command, parser in LEAVES.items():
        for line in (parser.epilog or "").splitlines()[1:]:
            line = line.split("   ")[0].strip() if line.strip().startswith("sf ") else ""
            if not line or "|" in line or ">" in line:
                continue
            words = shlex.split(line)[1:]
            head, _rest = split_rest(words)
            args = build_parser().parse_args(head)
            assert args.parser.prog == f"sf {command}", line


def test_bare_sf_and_bare_groups_print_help_and_exit_2(capsys):
    assert main([]) == 2
    assert "Start here" in capsys.readouterr().err
    assert main(["host"]) == 2
    assert "plan" in capsys.readouterr().err


def test_unknown_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        main(["frobnicate"])
    assert e.value.code == 2


@pytest.mark.parametrize("argv", [["--json", "-q", "--no-wait", "--home", "/x", "ls"],
                                  ["ls", "--json", "-q", "--no-wait", "--home", "/x"],
                                  ["--home", "/x", "ls", "--json", "--quiet", "--no-wait"]])
def test_global_options_work_before_and_after_the_command(argv):
    args = build_parser().parse_args(argv)
    assert (args.json, args.quiet, args.wait, args.home) == (True, True, False, "/x")


def test_global_option_defaults_and_no_override_by_the_subcommand():
    args = build_parser().parse_args(["ls"])
    assert (args.json, args.quiet, args.wait, args.home) == (False, False, None, None)
    # A value given before the command survives a command that does not repeat it.
    args = build_parser().parse_args(["--wait", "--json", "gpu", "set", "1", "igpu"])
    assert args.wait is True and args.json is True
    assert build_parser().parse_args(["start", "1", "--wait"]).wait is True


def test_aliases():
    assert build_parser().parse_args(["list"]).func is build_parser().parse_args(["ls"]).func
    assert build_parser().parse_args(["delete", "3"]).parser.prog == "sf rm"
    assert build_parser().parse_args(["backup", "ls"]).parser.prog == "sf backup list"


def test_choices_are_enforced():
    for argv in (["create", "--gpu", "fast"], ["create", "--net", "tor"], ["gpu", "set", "1", "fast"],
                 ["camera", "mic", "1", "loud"], ["auto", "run", "macro", "x", "1"], ["completion", "fish"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


def test_selector_with_exclusion_and_tags_is_one_positional():
    assert build_parser().parse_args(["stop", "1-10,tag:eu,!tag:spare"]).selector == "1-10,tag:eu,!tag:spare"


# ------------------------------------------------------------------ words after `--`

def test_split_rest_only_for_pass_through_commands():
    assert split_rest(["shell", "all", "--", "ls", "-l", "--", "x"]) == (["shell", "all"], ["ls", "-l", "--", "x"])
    assert split_rest(["--home", "/h", "--json", "shell", "1", "--root", "--", "id"]) == (
        ["--home", "/h", "--json", "shell", "1", "--root"], ["id"])
    assert split_rest(["host", "sign-module", "--", "--status"]) == (["host", "sign-module"], ["--status"])
    assert split_rest(["shell", "all"]) == (["shell", "all"], None)
    # Elsewhere `--` keeps argparse's own meaning: what follows is positional.
    argv = ["input", "text", "1", "--", "-hello"]
    assert split_rest(argv) == (argv, None)
    assert build_parser().parse_args(argv).text == "-hello"
    # `shell` as the value of --home is not the shell command.
    assert split_rest(["--home", "shell", "ls", "--", "x"])[1] is None


# ------------------------------------------------------------------ completion

def test_completion_tree_matches_the_parser():
    tree = completion.tree(build_parser())
    assert {"ls", "start", "host", "config", "auto", "backup", "--json", "--home", "--no-wait"} <= set(tree[()])
    assert set(tree[("host",)]) >= {"plan", "apply", "capacity", "gpus", "sign-module"}
    assert {"--keep-data", "--force", "--yes"} <= set(tree[("rm",)])
    assert set(tree[("completion",)]) >= {"bash", "zsh"}
    assert {"ls", "save", "validate"} <= set(tree[("auto", "flows")])


def test_completion_scripts_are_static_and_deterministic():
    first, second = completion.bash(build_parser()), completion.bash(build_parser())
    assert first == second
    assert "python" not in first.lower() and "$(sf" not in first        # nothing runs sf at Tab time
    zsh = completion.zsh(build_parser())
    assert zsh.startswith("#compdef sf\n") and "bashcompinit" in zsh and "complete -o default -F _sf_complete sf" in zsh


def test_completion_refuses_words_that_would_need_quoting():
    parser = argparse.ArgumentParser(prog="sf")
    parser.add_subparsers().add_parser("bad$(rm -rf)")
    with pytest.raises(ValueError):
        completion.bash(parser)


def _complete(script: str, *words: str) -> list[str]:
    """Source the script in a real bash and ask it to complete the last word."""
    program = (script + "\nCOMP_WORDS=(\"$@\"); COMP_CWORD=$(( $# - 1 )); _sf_complete; "
               "printf '%s\\n' \"${COMPREPLY[@]}\"")
    result = subprocess.run(["bash", "--norc", "--noprofile", "-c", program, "bash", "sf", *words],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_bash_completion_really_completes():
    script = completion.bash(build_parser())
    assert subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, timeout=30).returncode == 0
    assert "start" in _complete(script, "st") and "stop" in _complete(script, "st")
    assert _complete(script, "host", "ap") == ["apply"]
    assert set(_complete(script, "auto", "flows", "")) >= {"ls", "show", "save", "validate", "rm"}
    assert "--keep-data" in _complete(script, "rm", "3", "--k")
    assert _complete(script, "--json", "host", "sign") == ["sign-module"]
    assert _complete(script, "completion", "z") == ["zsh"]
    # An option value that happens to be a command name does not derail the path...
    assert "plan" in _complete(script, "--home", "/tmp/x", "host", "")
    # ...and an unknown word offers nothing from our lists (bash then falls back to file names).
    assert _complete(script, "host", "zzz") == []


def test_completion_command_prints_the_script(capsys):
    assert main(["completion", "bash"]) == 0
    assert capsys.readouterr().out == completion.bash(build_parser())
    assert main(["completion", "zsh"]) == 0
    assert capsys.readouterr().out == completion.zsh(build_parser())
