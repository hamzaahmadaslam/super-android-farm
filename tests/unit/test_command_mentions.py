"""Every `sf ...` command the program tells a user to run must be a command that exists.

Messages are written by hand and drift: a renamed subcommand leaves the old name behind in an
error text, and the user then types something that argparse rejects. This test reads every
string literal in the package, takes each backtick-quoted `sf ...` command in it and parses it
with the real parser. (The doctor's fix lines have their own, runtime version of this check in
test_host_doctor.py.)
"""
import ast
import contextlib
import io
import re
from pathlib import Path

import pytest

from superfarm.cli import main as cli_main
from superfarm.cli import values as cli_values
from superfarm.config import Config

PACKAGE = Path(cli_main.__file__).resolve().parents[1]

# A command in backticks: `sf vpn credentials <name>`. Quoted code that merely starts with "sf " in
# running text (no backticks) is not a command mention and is not checked.
MENTION = re.compile(r"`sf ([^`]+)`")
SHELL_TAIL = re.compile(r"(?:\s+[>|;&]|\n|\\n).*", re.S)   # a redirect, pipe or second command ends the mention
PLACEHOLDER = re.compile(r"<[^<>]*>|\b[A-Z][A-Z_]{2,}\b|\bN\b")
SAMPLES = ("1", "x", "default", "program", "all")           # tried in turn: choices differ per argument

# A mention that stops before its operands is still a correct pointer to the command ("run `sf start`").
INCOMPLETE = ("the following arguments are required", "expected one argument", "expected at least one argument")

# Mentions that are not commands to type, each with the reason. Keep this short.
# Format: {"vpn frobnicate <name>": "why this cannot be parsed"}. Nothing needs it today.
ALLOWED: dict[str, str] = {}


def literals():
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "`sf " in node.value:
                yield path.relative_to(PACKAGE.parent), node.lineno, node.value


def mentions():
    out = []
    for path, line, text in literals():
        for m in MENTION.finditer(text):
            command = SHELL_TAIL.sub("", m.group(1)).strip()
            if command:
                out.append((f"{path}:{line}", command))
    return out


def words_of(command, sample):
    words = []
    for word in command.split():
        if word in ("...", "…") or any(c in word for c in "[]{}"):
            continue                                        # optional parts and choice lists in a synopsis
        word = word.split("|")[0]                           # `bash|zsh`: one of the alternatives
        words.append(PLACEHOLDER.sub(sample, word).rstrip(".,:"))
    return words


def verdict(parser, words):
    """None when the real parser accepts the words (or only lacks operands), else its complaint."""
    head, _ = cli_main.split_rest(words)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            parser.parse_args(head)
    except SystemExit as e:
        if not e.code:                                      # --help
            return None
        message = err.getvalue().strip()
        return None if any(s in message for s in INCOMPLETE) and "invalid choice" not in message else message
    return None


def test_the_scan_finds_the_mentions_it_is_meant_to_check():
    found = {command for _, command in mentions()}
    assert len(found) > 40, sorted(found)
    assert {"vpn credentials <name>", "host apply", "vpn profiles"} <= found


def test_every_sf_command_in_a_message_is_accepted_by_the_real_cli():
    parser = cli_main.build_parser()
    wrong = []
    for where, command in mentions():
        if command in ALLOWED:
            continue
        problems = []
        for sample in SAMPLES:
            words = words_of(command, sample)
            problem = verdict(parser, words)
            if problem is None and words[:2] in (["config", "set"], ["config", "get"], ["config", "unset"]) \
                    and len(words) > 2 and "<" not in command.split()[2]:
                try:
                    cli_values.locate(Config, words[2])
                except Exception as e:                        # noqa: BLE001 - any complaint means the setting is unknown
                    problem = f"unknown setting {words[2]}: {e}"
            if problem is None:
                break
            problems.append(problem)
        else:
            wrong.append(f"{where}: `sf {command}` -> {problems[0].splitlines()[-1][:160]}")
    assert not wrong, "commands named in messages that the CLI does not have:\n" + "\n".join(wrong)


@pytest.mark.parametrize("command", ["vpn credentials set default", "vpn list", "host setup", "net build", "device profiles"])
def test_the_check_rejects_commands_that_do_not_exist(command):
    parser = cli_main.build_parser()
    assert verdict(parser, words_of(command, "1"))
