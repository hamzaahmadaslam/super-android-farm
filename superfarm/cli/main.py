"""`sf`: the command line of Super Farm.

    local commands    doctor, host, config, token, serve, service, version, completion
                      work with no daemon: they call the modules directly
    client commands   everything else: HTTP to the daemon (see client.py)

argparse only, so the CLI starts from a bare install. Every command carries one realistic
example in its --help; `sf completion bash|zsh` prints a completion script generated from
this same parser.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence

from .. import __version__
from . import completion, local
from .client import ApiError, CliError
from .commands import ai, assistant, automation, backups, device, instances, media, network
from .common import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, Ctx, explain_capacity
from .render import Output

PROG = "sf"
DESCRIPTION = """\
Super Farm: run and control a farm of Android containers.

Start here:
  sf doctor             is this machine ready?
  sf host apply         one-time host setup (asks for sudo)
  sf serve              run the daemon (panel + API) in the foreground
  sf create -n 3        add three Androids, then:  sf start all

A <selector> picks Androids:  all | running | stopped | tag:NAME | 5 | 1-10
Combine with commas, exclude with !:  1-10,tag:eu,!tag:spare
"""


class Builder:
    """Adds commands to the parser tree with the same options, help layout and example
    everywhere, so no command can be added without them."""

    def __init__(self) -> None:
        # The global options are accepted before *and* after the command (`sf --json ls`,
        # `sf ls --json`). In the per-command copy they default to SUPPRESS: a value given
        # before the command must not be overwritten by a default from after it.
        self.common = argparse.ArgumentParser(add_help=False)
        self._options(self.common, suppress=True)

    @staticmethod
    def _options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
        def default(value: object) -> object:
            return argparse.SUPPRESS if suppress else value
        group = parser.add_argument_group("global options")
        group.add_argument("--home", metavar="DIR", default=default(None),
                           help="farm home to work on (default: $SUPERFARM_HOME or ~/.local/share/superfarm)")
        group.add_argument("--json", action="store_true", default=default(False),
                           help="print the result as JSON (for scripts)")
        group.add_argument("--wait", action=argparse.BooleanOptionalAction, default=default(None),
                           help="follow a started task to its end and show per-device results "
                                "(default: yes on a terminal, no in a script)")
        group.add_argument("-q", "--quiet", action="store_true", default=default(False),
                           help="no progress line and no hints, only the result")

    def root(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION,
                                         formatter_class=argparse.RawDescriptionHelpFormatter,
                                         epilog="Each command explains itself:  sf <command> --help")
        self._options(parser, suppress=False)
        parser.add_argument("--version", action="version", version=f"superfarm {__version__}")
        parser.set_defaults(func=_help_of(parser), parser=parser)
        return parser

    def add(self, sub: argparse._SubParsersAction, name: str, func: Callable[[Ctx], int], summary: str,
            example: str, *, description: str = "", aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
        """One runnable command. `example` is mandatory and shown under its --help."""
        examples = "\n".join(f"  {line}" for line in example.strip().splitlines())
        parser = sub.add_parser(name, help=summary, description=description or summary[:1].upper() + summary[1:] + ".",
                                epilog=f"example:\n{examples}", aliases=list(aliases), parents=[self.common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.set_defaults(func=func, parser=parser)
        return parser

    def group(self, sub: argparse._SubParsersAction, name: str, summary: str, *, description: str = "",
              default: Callable[[Ctx], int] | None = None) -> argparse._SubParsersAction:
        """A command that holds sub-commands (`sf host ...`). Alone, it prints its help, or
        runs `default` where one reading is obvious (`sf auto flows` lists the flows)."""
        parser = sub.add_parser(name, help=summary, description=description or summary[:1].upper() + summary[1:] + ".",
                                parents=[self.common], formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.set_defaults(func=default or _help_of(parser), parser=parser)
        return parser.add_subparsers(title="commands", metavar="<command>")


def _help_of(parser: argparse.ArgumentParser) -> Callable[[Ctx], int]:
    def show(ctx: Ctx) -> int:
        parser.print_help(ctx.out.stderr)
        return EXIT_USAGE
    return show


def _local(b: Builder, sub: argparse._SubParsersAction) -> None:
    b.add(sub, "doctor", local.doctor, "check that this machine can run Androids, with the fix for each problem",
          "sf doctor")

    host = b.group(sub, "host", "prepare and inspect this machine (kernel modules, limits, GPUs)")
    p = b.add(host, "plan", local.host_plan, "show every command host setup would run, changing nothing",
              "sf host plan")
    p.add_argument("--boot", action="store_true", help="only what must be redone after every reboot")
    p = b.add(host, "apply", local.host_apply, "carry out host setup (re-runs itself under sudo)",
              "sf host apply\nsf host apply --dry-run",
              description="Install packages, load kernel modules, raise kernel limits, create runtime folders "
                          "and install the systemd units. Needs root: started as a normal user it continues "
                          "under sudo, keeping your farm home. Safe to repeat.")
    p.add_argument("--dry-run", action="store_true", help="print every command and file, change nothing")
    p.add_argument("--boot", action="store_true", help="only what must be redone after every reboot")
    p = b.add(host, "capacity", local.host_capacity, "estimate how many Androids this machine can run at once",
              "sf host capacity")
    p.add_argument("--workload", choices=("video", "light"), default="video",
                   help="video: apps playing video, the heaviest case measured (default). light: mostly idle screens")
    b.add(host, "gpus", local.host_gpus, "list the GPUs found and the rendering modes they allow", "sf host gpus")
    b.add(host, "sign-module", local.host_sign_module,
          "sign a self-built kernel module for Secure Boot (virtual camera)",
          "sf host sign-module                              (status report)\n"
          "sf host sign-module -- --create-key --enroll v4l2loopback",
          description="Run scripts/host/sign-module.sh. Without arguments it reports the Secure Boot state and "
                      "the signing key in use. Arguments after `--` go to the script (see its --help); anything "
                      "but a status report runs under sudo.")

    config = b.group(sub, "config", "read and change the farm's settings (config.yaml)")
    p = b.add(config, "get", local.config_get, "print one setting", "sf config get android.fps")
    p.add_argument("key", help="dotted key, e.g. network.vpn.selection")
    p = b.add(config, "set", local.config_set, "change one setting (validated before it is saved)",
              "sf config set android.fps 25\nsf config set network.dns 1.1.1.1,9.9.9.9")
    p.add_argument("key", help="dotted key, e.g. android.fps")
    p.add_argument("value", help="new value; lists are comma separated")
    p = b.add(config, "unset", local.config_unset, "put one setting back to its default",
              "sf config unset android.fps")
    p.add_argument("key", help="dotted key")
    p = b.add(config, "show", local.config_show, "print all settings in effect", "sf config show --changed")
    p.add_argument("--changed", action="store_true", help="only what differs from the defaults (the file's content)")
    b.add(config, "path", local.config_path, "print where config.yaml is", "sf config path")
    p = b.add(config, "docs", local.config_docs, "render the settings reference (docs/CONFIGURATION.md)",
              "sf config docs -o docs/CONFIGURATION.md\nsf config docs --check docs/CONFIGURATION.md")
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of printing")
    p.add_argument("--check", metavar="FILE", help="exit 1 when FILE differs from what would be generated (for CI)")

    p = b.add(sub, "token", local.token, "print the API token the panel and scripts log in with",
              "sf token\nsf token --rotate")
    p.add_argument("--rotate", action="store_true", help="replace the token; the old one stops working at once")

    p = b.add(sub, "serve", local.serve, "run the daemon (panel + API) in the foreground",
              "sf serve\nsf serve --port 7701",
              description="Run the daemon in the foreground until Ctrl-C. Androids keep running when it stops.")
    p.add_argument("--host", default=None, help="listen address (default: server.host, 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="listen port (default: server.port, 7700)")

    service = b.group(sub, "service", "run the daemon as a systemd user service")
    b.add(service, "install", local.service_install, "write and enable the user service", "sf service install")
    b.add(service, "start", local.service_start, "start the daemon in the background", "sf service start")
    b.add(service, "stop", local.service_stop, "stop the daemon (Androids keep running)", "sf service stop")
    b.add(service, "status", local.service_status, "show whether the daemon runs", "sf service status")
    p = b.add(service, "logs", local.service_logs, "show the daemon's log", "sf service logs -f")
    p.add_argument("-n", "--lines", type=int, default=100, help="lines to show (default: 100)")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")

    b.add(sub, "version", local.version, "print the version", "sf version")

    p = b.add(sub, "completion", completion.run, "print a shell completion script",
              "sf completion bash > ~/.local/share/bash-completion/completions/sf\n"
              "sf completion zsh > ~/.zfunc/_sf")
    p.add_argument("shell", choices=("bash", "zsh"))


def build_parser() -> argparse.ArgumentParser:
    b = Builder()
    parser = b.root()
    sub = parser.add_subparsers(title="commands", metavar="<command>")
    for module in (instances, device, network, media, automation, backups, assistant, ai):
        module.register(b, sub)
    _local(b, sub)
    return parser


# Commands whose words after `--` are not ours to parse (a device command line, script options).
_PASS_THROUGH = (("shell",), ("host", "sign-module"))
_VALUE_OPTIONS = ("--home",)


def split_rest(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Cut `sf shell 1 -- ls -l` at the `--`. Only for the commands that hand the rest on;
    everywhere else argparse's own meaning of `--` (end of options) is kept."""
    if "--" not in argv:
        return argv, None
    cut = argv.index("--")
    words: list[str] = []
    skip = False
    for word in argv[:cut]:
        if skip:
            skip = False
        elif word in _VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            words.append(word)
    if any(tuple(words[:len(path)]) == path for path in _PASS_THROUGH):
        return argv[:cut], argv[cut + 1:]
    return argv, None


def report_api_error(out: Output, error: ApiError) -> int:
    if error.status == 409 and error.api_code == "over_capacity":
        for line in explain_capacity(error):
            out.stderr.write(line + "\n")
        return EXIT_FAILED
    out.error(error.message)
    if isinstance(error.detail, list):
        # Validation errors: one line per field (the API never echoes the rejected value).
        for item in error.detail:
            if isinstance(item, dict) and item.get("message"):
                out.stderr.write(f"    {item.get('field') or 'request'}: {item['message']}\n")
    elif isinstance(error.detail, dict) and error.detail.get("reason"):
        out.stderr.write(f"    {error.detail['reason']}\n")
    return EXIT_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    head, rest = split_rest(raw)
    args = build_parser().parse_args(head)
    args.rest = rest or []
    out = Output(json_mode=bool(args.json), quiet=bool(args.quiet))
    try:
        return int(args.func(Ctx(args, out)) or EXIT_OK)
    except ApiError as e:
        return report_api_error(out, e)
    except CliError as e:
        out.error(str(e))
        return e.code
    except KeyboardInterrupt:
        out.clear_status()
        out.stderr.write("\n")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # `sf ls | head`: the reader left. Point stdout at /dev/null so the interpreter's
        # final flush does not print a second error, and leave quietly.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
