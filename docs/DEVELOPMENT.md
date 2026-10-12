# Development

For anyone who works on the Super Farm code: setting up a checkout, running the gates and tests, and adding a setting, route, command or tool.

[CONTRIBUTING.md](../CONTRIBUTING.md) is the short list of what the project asks of a contribution. [Architecture](ARCHITECTURE.md) explains how the parts fit together and [Design notes](DESIGN_NOTES.md) why.

## Set up

You need an x86-64 Linux machine, Python 3.10 or newer with `venv`, and for the live tests Docker. The code targets Python 3.10 (`requires-python`, and ruff's `target-version`), so do not use syntax or library features that need a newer one. In particular, avoid reusing the same kind of quote inside an f-string expression.

```bash
git clone https://github.com/hamzaahmadaslam/super-android-farm.git
cd super-android-farm
./install.sh --dev --no-host      # virtual environment in .venv with the test tools; no root, no host changes
.venv/bin/sf version
```

`--dev` installs the project in editable mode with pytest, pytest-asyncio, httpx and ruff. `--no-host` skips host setup (kernel modules, limits, groups), which needs root. You need it only to run Androids, not to work on most of the code. Two optional extras:

```bash
.venv/bin/pip install -e ".[mcp]"     # the MCP SDK; the MCP tests skip without it
.venv/bin/pip install -e ".[u2]"      # the persistent UI-tree reader for automation
```

Point your work at a throwaway farm home so it never touches a real one:

```bash
export SUPERFARM_HOME=$PWD/../sf-dev-home
```

Run the daemon from the checkout with `.venv/bin/sf serve` and the panel is at `http://127.0.0.1:7700`. The panel is static files under `superfarm/web/` with no build step. Reload the page to see a change.

## Layout

| Path | What is in it |
| --- | --- |
| `superfarm/config.py` | Every setting, with its default and meaning. |
| `superfarm/farm.py` | The composition root. It builds the objects and wires them, and classifies settings by what they need to take effect. |
| `superfarm/core/` | Android lifecycle (`instances.py`), the settings model of one Android (`models.py`), tasks, backups, images, Docker and ADB wrappers. |
| `superfarm/host/` | The machine: doctor, host setup, kernel modules, GPU detection and choice, the NVIDIA relay, capacity, the user service. |
| `superfarm/net/` | The network sidecar, VPN profiles, shared gateways, the kill switch. |
| `superfarm/control/` | Apps, the app library, files, device settings, shell. |
| `superfarm/stream/` | Screenshots, input, native windows, the H.264 relay. |
| `superfarm/media/` | Virtual camera and microphone, listening. |
| `superfarm/automation/` | Recording, training, programs, flows, profiles, values, schedules, runs. |
| `superfarm/ai/`, `superfarm/mcp/` | What an AI model may see and do, the policy and approvals, the built-in Assistant and Pilot, and the MCP server. |
| `superfarm/api/` | The HTTP API. `routes/` has one module per area. |
| `superfarm/cli/` | The `sf` command. `commands/` has one module per area. |
| `superfarm/web/`, `superfarm/data/` | The panel; the trim lists, device presets and example flows. |
| `deploy/`, `native/`, `scripts/` | Dockerfiles and unit templates; native sources and build scripts; helper programs. Files directly in `scripts/` are tools for working on a checkout and are not installed. |
| `tests/` | `unit/`, `live/`, `distro/`. |

A module-level docstring at the top of nearly every file says what the module is for and what rules it enforces. Read it before you change the file.

## The gates

`scripts/check.sh` is the quality gate. Nothing runs on a hosted service.

| Command | What runs | Takes |
| --- | --- | --- |
| `scripts/check.sh --quick` | lint, shell scripts, panel JavaScript syntax, secrets check, documentation links, settings reference | seconds |
| `scripts/check.sh` | the above plus the unit tests | about 3 minutes |
| `scripts/check.sh --live` | the above plus the live tests | needs Docker and an Android image |
| `scripts/check.sh --install-hook` | writes `.git/hooks/pre-push` so a push is refused while a gate fails | |

`make check`, `make quick`, `make live`, `make hook` and `make docs` are shortcuts. The steps are:

- `ruff check superfarm tests scripts`. Rules `E`, `F`, `W`, `B`, `S` (security) and `UP`, line length 110, `E501` off. Tests are exempt from `S`.
- `bash -n` on every shell script, and `shellcheck -S error` when it is installed.
- `node --check` on the panel JavaScript, skipped when `node` is missing.
- The secrets gate: private-key blocks, common token shapes, and file types that must not be committed (`.ovpn`, `.pem`, `.key`, `.apk`, `.so`, `.ko` and similar). If the file `.git/info/private-words` exists, one word per line, those words must not appear in tracked files. The file is untracked so the words stay out of the repository.
- The documentation-links gate: every relative Markdown link and heading anchor in tracked `.md` files must resolve.
- The settings reference: `docs/CONFIGURATION.md` must equal what `sf config docs` generates.

Run the gate on tracked files: `git add` a new document before you run the links gate, or it is not checked.

## Tests

Three layers, and the first is the one you run all the time.

| Layer | Where | Needs |
| --- | --- | --- |
| Unit | `tests/unit/` | Python only: no Docker, no network, no devices |
| Live | `tests/live/` | Docker, the binder module, a local Android image; opt in with `SF_LIVE=1` |
| Distribution | `tests/distro/` | Docker and network access; checks `install.sh` in a container per distribution |

Unit tests:

```bash
.venv/bin/python -m pytest -o addopts="" -q tests/unit
.venv/bin/python -m pytest -o addopts="" -q tests/unit/test_cli_parser.py -k parses
```

They use fakes: `tests/unit/core_fakes.py`, `net_fakes.py`, `stream_fakes.py`, `automation_fakes.py`, `ai_fakes.py`, `api_fakes.py`, and `cli_fakes.py`, a small HTTP server standing in for the daemon. The `farm_home` fixture gives each test an empty farm home through `SUPERFARM_HOME`, so no test can read or write a real farm. Test the module that has the rule, with the fake of what is under it.

Live tests drive real containers. Each one creates its objects under a `sftest` prefix (the networking tests use `sftnet` and `sftest-net`) in a throwaway farm home, and removes them. They are skipped unless `SF_LIVE=1`:

```bash
SF_LIVE=1 .venv/bin/python -m pytest -o addopts="" -q tests/live/test_core_lifecycle.py
```

The tests never pull images. Put the image on the machine first, or the test skips with the name it wanted. Variables the live tests read:

| Variable | Meaning |
| --- | --- |
| `SF_LIVE` | `1` runs the live tests. |
| `SF_LIVE_IMAGE` | The Android image for the throwaway Android. Most tests default to `redroid:13-noflag`, and the lifecycle, stream and control tests to `redroid/redroid:13.0.0-latest`. |
| `SF_LIVE_HELPER` | Helper image for the data-volume tools. Default `alpine:latest`. |
| `SF_LIVE_ANDROID` | For the VPN tests: `1` also boots one real Android inside a VPN sidecar. |
| `SF_LIVE_ANDROID_IMAGE` | The image for that Android. Default `redroid:13-noflag`. |
| `SF_LIVE_SERIAL` | Stream tests: use this already running device instead of starting one. |
| `SF_LIVE_ADB_PORT` | Host ADB port for the throwaway Android (stream tests 17301, control tests 17401). |
| `SF_LIVE_CONTAINER` | Control tests: name of the throwaway container. Default `sftest-ctl-android`. |
| `SF_LIVE_MEDIA_IMAGE`, `SF_LIVE_MEDIA_ADB_PORT` | Microphone test: an Android 13 image with the camera overlay baked in, and its ADB port (default 17201). |
| `SF_LIVE_FPS_PROPERTY` | End-to-end test: the boot property a patched test image takes its frame rate from. |
| `SF_LIVE_VENUS_RELAY`, `SF_LIVE_VENUS_RUNTIME` | End-to-end NVIDIA scenario: a built relay prefix and runtime folder. Without them the scenario skips. |
| `SF_LIVE_MODEL_URL`, `SF_LIVE_MODEL`, `SF_LIVE_MODEL_FORMAT` | AI live tests: address, model and wire format (`ollama` or `openai`) of a model server that is already running. |
| `SF_LIVE_OLLAMA_BIN`, `SF_LIVE_OLLAMA_MODELS` | AI live tests: start a local model server from this binary and model folder, instead of using one that is already running. |
| `SF_EXAMPLES_TRIM` | `off` boots the examples test's Android with trimming off, to compare. |

Each live test file says at the top what it needs and what it touches. Read it before you run it on a machine that hosts a real farm.

The distribution test checks the installer in throwaway containers:

```bash
tests/distro/run.sh                 # all five families
tests/distro/run.sh fedora suse
```

It runs `install.sh --container-test` as root inside each container, resolves every package name the farm knows for that family against the real package index, builds and installs a wheel, runs `sf doctor` and the host commands, and runs a fast subset of the unit tests. It does not exercise kernel modules, sysctl, groups or systemd.

## Conventions

- Every module starts with a docstring that says what it is for and the rules it keeps. Comments explain why, not what.
- Anything that starts a program uses an argument list and never a shell string. Values from outside (names, paths, package names, selectors) are validated at the edge and reduced to a plain form before they reach a command, a path or a file name.
- Anything that comes from an uploaded file, a profile, a backup manifest or a device is untrusted input. Existing code shows the pattern: unpack into staging, cap sizes and counts, refuse links, keep only what is known to be inert.
- An error message says what is wrong and what to do about it, and does not echo a value that may be a secret.
- Operations on many Androids return a task with one result per Android, and a failing Android never stops the others.
- Do not run a shell command that changes the host from the daemon. Root work belongs to `superfarm/host/setup.py` and runs only through `sf host apply`.
- Settings carry a `description` and constraints in the model. Defaults are the safe choice.
- Names: the product is Super Farm, the package `superfarm`, the command `sf`, the container prefix `sf`.
- Documentation sentences are plain and direct, in the second person ("you"), without marketing words. Do not put personal data, secrets or a vendor's AI product name in the repository.

## Keeping generated documents current

Five references are generated from the code. Do not edit the generated parts by hand; the gates and tests fail when they drift.

| Document | Generated from | Regenerate with |
| --- | --- | --- |
| `docs/CONFIGURATION.md` | `superfarm/config.py` | `.venv/bin/sf config docs -o docs/CONFIGURATION.md` (or `make docs`) |
| `docs/CLI.md` | the `sf` argument parser | `.venv/bin/python scripts/gen_cli_docs.py -o docs/CLI.md` |
| `docs/API.md`, route reference | the app's OpenAPI schema | `.venv/bin/python scripts/gen_api_docs.py -o docs/API.md` |
| `docs/AUTOMATION.md`, step reference | the flow models in `superfarm/automation/flows.py` | `.venv/bin/python scripts/gen_flow_docs.py` |
| `docs/MCP.md`, tool reference | the tool registry in `superfarm/ai/tools.py` | `.venv/bin/sf mcp tools --markdown`, pasted between the `TOOLS` markers |

The first four generators have a check mode (`--check FILE`) that exits 1 when the file is out of date. The hand-written parts of `docs/API.md` (above its marker line) and `docs/AUTOMATION.md` (outside its markers) are kept as they are. The settings reference and the tool reference are also checked by unit tests.

## Add a setting

1. Add the field to the right model in `superfarm/config.py`: a type, a default, constraints such as `ge` and `le`, and a `description` that says what it does and when to change it. A test fails for a setting without a description.
2. If it names a program the daemon runs, or code that program loads, add it to `API_IMMUTABLE_KEYS` so the API cannot change it.
3. If a change must wait for an Android restart or a daemon restart, add its prefix to `ANDROID_RESTART` or `DAEMON_RESTART` in `superfarm/farm.py`.
4. If it can differ per Android, add the field to `InstanceSpec` in `superfarm/core/models.py` and use it in `resolve()`, which falls back to the farm value.
5. Write a unit test for the validation and for the code that uses it.
6. Regenerate `docs/CONFIGURATION.md`.

## Add an API route

1. Put the function in the module of `superfarm/api/routes/` for its area. Take a request body from `Body` (it refuses unknown fields). Use `instance_id()` and `resolve_targets()` for Androids, raise the modules' own exceptions (the error mapping in `api/errors.py` turns them into HTTP errors), and record the facts worth keeping with `note(request, ...)`.
2. Work that touches several Androids or takes long returns a task: `farm.tasks.submit(...)` and `accepted(request, task)`. Mark it exclusive if it changes what an Android is doing, so the busy rule applies.
3. Add the route to the `CONTRACT` list in `tests/unit/test_api_app.py` and write a test in the matching `tests/unit/test_api_*.py`.
4. Decide whether the CLI and the MCP tools need it too.
5. Regenerate the route reference with `scripts/gen_api_docs.py`.

Authentication, CSRF and error rules are already applied to every route in a router that depends on `require`. Do not write your own.

## Add a CLI command

1. Register it in the module of `superfarm/cli/commands/` for its area with `b.add(sub, name, func, summary, example)`. The example is required and is shown under `--help`; a test checks that every example parses.
2. Write the function as `run(ctx) -> int`. Use `ctx.api` for the daemon, `ctx.select()` for selectors, `ctx.finish()` to follow a task, and `ctx.out` for output, so `--json`, `--quiet` and `--wait` work.
3. Add a sample of the command to `SAMPLES` in `tests/unit/test_cli_parser.py`. The test `test_every_command_has_a_parse_sample` fails without it. Shell completion is generated from the parser, so nothing else needs editing.
4. Regenerate `docs/CLI.md`.

## Add an AI tool

1. Add a `Tool(...)` in `superfarm/ai/tools.py` with a name, a precise input schema, a description written for a model, the routes it calls, and its class (read, change or destructive). The classes decide what the policy asks the operator about. Deletion, uninstall, erasing data, restore and shell commands are destructive.
2. A tool may call only the routes it declares. The tests check that every declared route exists and that the tool calls exactly those.
3. The names in `ai.deny` and `ai.auto_approve` are validated against the registry, so a new tool is usable there at once.
4. Regenerate the tool reference in `docs/MCP.md` with `sf mcp tools --markdown`.

## Add a flow step

Add the argument model and its entry in the step table in `superfarm/automation/flows.py`, implement it in the flow runner, test it in `tests/unit/test_automation_flows.py` and `test_automation_runner.py`, and run `scripts/gen_flow_docs.py`.

## Other places that need a matching change

- A new folder under `deploy/`, `scripts/` or `native/` must be listed in `pyproject.toml` under `[tool.setuptools.data-files]`, or an installed copy would lack it. A test checks this.
- A new package for a distribution goes in the tables of `superfarm/host/distro.py`. The distribution test resolves each name against the real index.
- A new kernel module goes in `superfarm/host/modules.py`, with the reason it is needed.
- A new doctor check is a function in `superfarm/host/doctor.py` that returns a `Check` with the exact fix for the distribution, and goes in the `CHECKS` tuple.

## Next

- [Architecture](ARCHITECTURE.md): how the parts fit together.
- [Design notes](DESIGN_NOTES.md): decisions and the reasons behind them.
- [Verification](VERIFICATION.md): what has been run and how.
- [Contributing](../CONTRIBUTING.md): what the project asks of a change.
- [Command reference](CLI.md), [API reference](API.md) and [Configuration reference](CONFIGURATION.md): the generated references.
