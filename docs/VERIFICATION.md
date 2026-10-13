# Verification

For anyone who wants to know which parts of Super Farm were run, on what, and how to run the same checks again.

Every claim in the other documents traces back to a line on this page. "Verified live" means the code ran against real Docker containers and real Android instances. "Unit-tested" means it ran against stand-ins for Docker, ADB and the kernel. "Not verified" means the code exists and was never run for real.

## The test machine

All live results come from one machine.

| Part | Value |
| --- | --- |
| CPU | Intel Core i9-14900K, 32 threads |
| RAM | 32 GB |
| GPUs | NVIDIA GeForce RTX 4080 SUPER (16 GB) and Intel UHD 770 |
| OS | Ubuntu 26.04, kernel 7.0, Secure Boot on |
| Software | Docker 29, Python 3.14, NVIDIA driver 595, scrcpy 3.3.4 |
| Android | Android 13 |

The Android image in those runs was a locally built Android 13 image based on redroid, tagged `redroid:13-noflag` on the test machine. That tag is the default of `SF_LIVE_IMAGE` in most live tests. Running the live tests with the stock upstream image (`redroid/redroid:13.0.0-latest`, the configured default) has not been verified.

## Verified live

| Area | What was verified | Test |
| --- | --- | --- |
| Lifecycle | Create, start, boot, stop and start with data kept; per-Android overrides visible in `docker inspect`; network `none`; capacity warn, enforce and force; a restart never leaves an Android stopped; the Android version guard; a renderer change rebuilds both containers and clears shader caches. 10 of 10 scenarios. | `tests/live/test_e2e_farm.py` |
| Boot time | About 11 s with software rendering, 17 s on the Intel integrated GPU, 11 s on NVIDIA | same |
| Balanced rendering | In a throwaway farm with `gpu.mode: balanced` and `gpu.balance.per_igpu: 1`, three Androids started together: one drew on the Intel integrated GPU and two on the CPU. The first run put all three on the integrated GPU, because starts that run at once each counted the cards before the others had picked one. That was fixed and the run repeated. An Android pinned by PCI address (`0000:00:02.0`) started on that card. | by hand with `sf`; the fix has a regression test in `tests/unit/test_core_instances.py` |
| Rendering modes | The renderer string Android reports in each mode (see the next table) | same |
| Backup | tar with zstd; owners, modes and 362 extended attributes identical after restore; restore as new; the restored twin is created offline (network `none`); the automatic safety backup before delete | `tests/live/test_e2e_farm.py`, `tests/live/test_core_lifecycle.py` |
| Networking | All traffic through the tunnel; with the VPN server killed nothing leaves by the plain interface and ADB still works; the tunnel reconnects; a wrong password is reported as an authentication failure; a wiped firewall is detected and restored; IPv6 default-drop holds when IPv6 is re-enabled inside the namespace; a shared gateway with two members; one Android behind a VPN sidecar and one behind a gateway. 14 of 14 tests, against an OpenVPN server the test creates. A control run with the kill switch off did leak. | `tests/live/test_net_direct.py`, `tests/live/test_net_vpn.py` |
| Apps | A split bundle (an `.xapk` with 10 APKs and an OBB file) installs only the matching splits; ensure, update and downgrade rules; grant and revoke; app-data backup and restore; APK metadata parsed without external tools | `tests/live/test_control_live.py` |
| Screen and input | The screenshot stream and the H.264 relay with touch control; all 95 printable ASCII characters and a non-ASCII string typed and read back; broadcast input | `tests/live/test_stream_live.py`, `tests/live/test_api_live.py` |
| Microphone and listening | Sound injected into Android's microphone and recorded back at source level; mute gives silence. "Listen" started and stopped with real scrcpy. | `tests/live/test_media_mic.py`; listening was checked by hand |
| Automation | Record three times, train by consensus, replay; the flows interpreter; the never-tap list; dry run; the needs-attention hand-off | `tests/live/test_automation_live.py` |
| Persistent screen reader | About 20 times faster than the built-in dump on a busy screen | measured by hand in a separate environment with the `u2` extra installed |
| API and daemon | Every route group; cookie login with CSRF; token authentication; SIGTERM shuts the daemon down cleanly and leaves Androids running | `tests/live/test_api_live.py` |
| Double requests | One operator action per Android at a time (the second gets `409 busy`); two deletes give one success and one 404; `Idempotency-Key` on create | `tests/live/test_api_double_requests.py` |
| MCP | stdio and loopback HTTP with a real MCP client library; approve, deny with a reason, timeout, remember for the session; a restart expires pending requests; one real Android driven end to end through MCP tools. The registry had 37 tools when this ran. The two added later, `pilot_start` and `pilot_status`, are covered by unit tests only. The registry now has 39 tools: 17 read, 17 change and 5 destructive. | `tests/live/test_mcp_live.py` |
| Panel | 26 routes at 3 sizes in 2 themes with zero console errors, CSP violations or external requests; 573 control presses; real double and triple clicks send exactly one request; 206 Androids render; H.264 decoded by WebCodecs | headless Chrome driven by a script that is not in the repository |
| Super Control | Against that throwaway farm, in headless Chrome: three real Androids as cards with live stills, the live feed over H.264, keys and text sent from the console, input broadcast to two ticked Androids, one Android stopped from the selection bar, and the timeline showing the real state changes. All 16 pages opened with no console error. With generated data: 60 Androids, at four widths and in both themes. | by hand, with a script that is not in the repository |
| Installer | On Ubuntu 24.04, Debian 12, Fedora 44, Arch (rolling) and openSUSE Tumbleweed, in containers: packages installed, the wheel installed into a fresh virtual environment, `sf` and the doctor ran, and a subset of the unit tests passed (Python 3.11, 3.12, 3.13 and 3.14). The subset was 679 tests on Ubuntu, Debian, Arch and openSUSE with the code that has `balanced` rendering. Fedora's last complete run was with the version before it (603 tests): on the later run its package servers were too slow for the test to finish. On the test machine itself, `install.sh --dry-run` and `install.sh --dev --no-host` ran into a fresh virtual environment, and the farm of the "Balanced rendering" row was started from that install. | `tests/distro/run.sh`; the last part by hand |
| Security review | Every confirmed finding fixed with a regression test: authentication decided from the bound address, safe-mode restore of farm backups with a seal, the audit log split, token rotation ending sessions, API-immutable settings, the IPv6 kill switch, the recorder never storing typed field content, YAML aliases refused, app backups owner-only | `tests/unit/` (for example `test_api_auth.py`, `test_api_audit.py`, `test_core_backup_farm.py`, `test_net_killswitch.py`, `test_api_immutable_settings.py`) |

The Assistant and the Pilot have a live test of their own, `tests/live/test_ai_live.py`. [AI.md](AI.md) reports what was measured with it.

Renderer strings reported by Android:

| Mode | Renderer |
| --- | --- |
| software | `ANGLE (… SwiftShader …)` |
| Intel integrated GPU | `Mesa Intel(R) Graphics (RPL-S), OpenGL ES 3.2 Mesa 24.0.8` |
| NVIDIA | `ANGLE (NVIDIA, Vulkan 1.3 (NVIDIA Virtio-GPU Venus (NVIDIA GeForce RTX 4080 SUPER)) …)` |

The NVIDIA run used a prebuilt Venus runtime and relay. The build script that produces them, `native/venus/build.sh`, was not run end to end.

## Measured numbers

These were measured on the test machine. On another machine, or with a different mix of apps, they will differ.

| Measurement | Value |
| --- | --- |
| Capacity estimate with the default `gpu.mode: auto` | about 33 Androids on the Intel integrated GPU, limited by RAM |
| Memory per Android in use | about 0.9 to 1.1 GB; NVIDIA mode about 0.3 GB more |
| Memory with 13 Androids in a video app | about 27 GB |
| CPU per Android, 13 Androids scrolling a settings screen | software 78% of a core, Intel 13%, NVIDIA 15% |
| Disabling 38 unused system packages | about 170 MB less per Android |
| Frame rate 25 to 15 fps | about 4% less CPU in a video app |
| Compressed swap (zram, 16 GB, zstd) | 17 idle Androids healthy on 30 GB; compression ratio 4.2 to 5.1 : 1 |
| Screenshot capture at 480×854 | about 55 ms raw |
| H.264 relay | about 15 ms of device CPU per frame |

## Not verified

| What | Why not | What exists instead |
| --- | --- | --- |
| `sf host apply` for real: packages, kernel modules, sysctl values, zram, groups, virtual camera devices | needs root; the tests had none | `sf host apply --dry-run`, unit tests against a fake root, the package step in distribution containers |
| The boot service running; `sf service install` | need a real init system and would change the desktop session | `systemd-analyze verify` accepted both units; unit tests |
| Kernel module signing (`sf host sign-module`) | needs root | the status report only |
| AMD GPUs | no AMD hardware | the code path is the same Mesa path as Intel; unit tests with fake sysfs trees |
| `balanced` across two or more discrete cards, and with an NVIDIA card in the mix | the test machine has one discrete card, and the NVIDIA runtime was not built in the farm the mode was run in | unit tests against described machines: four equal cards, a large and a small card, cards in other PCI domains (`tests/unit/test_host_gpu_balanced.py`) |
| Two or more NVIDIA cards, each relay limited to its own card | no machine with two NVIDIA cards | the environment each relay is started with is unit-tested; whether Mesa's device-select layer hides the other cards from NVIDIA's driver was not tried |
| A headless server | none was available | the server rules of the doctor are unit-tested against a fake root; the rest follows the documented behaviour of udev and the drivers ([GPU](GPU.md#servers)) |
| WireGuard tunnels | the kernel module was not loaded on the test machine | profile sanitising and argument building are unit-tested; the "kernel lacks WireGuard" error was seen live |
| IPv6 enabled (`network.ipv6: true`) | not run | the rules are generated and unit-tested |
| Docker older than 29 | not available | none |
| Android versions other than 13 | no other image was booted | images are configured for 9 to 15; the camera HAL and the NVIDIA runtime are built for Android 13 only |
| The virtual camera picture end to end | needs a loopback device created by root | the microphone path and the in-container steps ran; the HAL overlay must be extracted by you from a BlissOS image |
| Building the NVIDIA runtime with `native/venus/build.sh` | long, and needs the Android NDK | `fetch` and `check-patches` ran: both patches apply to the pinned sources |
| Models larger than 3 billion parameters; a hosted OpenAI-compatible endpoint, vision input and the API-key paths; resolving a Pilot run that needs attention and confirming an unverified one | the live AI test used one small local model; the rest ran only against stub servers or in unit tests | unit tests |
| Browsers other than Chrome; a screen reader | not run | none |
| zsh completion | zsh was not installed | the bash completion is tested |
| Device presets against retail firmware | model codes come from published information | none |
| More than three real Androids at once through Super Farm | the live tests never ran more than two at a time, and three were started together once by hand | fleet behaviour is unit-tested with stand-ins |

## Known limits

- `proxy` network mode has no kill switch.
- A dead VPN server is noticed after `network.vpn.ping_restart_seconds` (120 by default). Traffic is dropped in the meantime.
- A shared gateway holds at most 126 Androids.
- The API token is equivalent to root on the host, because Androids are privileged containers and the daemon controls Docker.
- ADB has no authentication: keep `network.adb_bind` on loopback. The RTMP camera ingest has no authentication either.
- The approvals step guards assistants that work through MCP or the built-in Assistant. A process that holds the API token can call the API directly.
- Automation flows type printable ASCII only. The panel types anything, through a clipboard paste.
- The run status of automations is kept in memory and lost when the daemon restarts.

[SECURITY_MODEL.md](SECURITY_MODEL.md) explains the security limits in detail.

## Run the checks yourself

### The local gate

There is no hosted CI. Every gate runs on your machine with `scripts/check.sh`.

| Command | Runs | Takes or needs |
| --- | --- | --- |
| `scripts/check.sh --quick` | lint, shell and JavaScript syntax, the secrets check, documentation links, the settings reference check | seconds |
| `scripts/check.sh` | the above plus the unit tests (more than 3,000) | about 3 minutes |
| `scripts/check.sh --live` | the above plus the live tests | Docker and an Android image |
| `scripts/check.sh --install-hook` | installs the fast gate as a git pre-push hook | a git checkout |

`make quick`, `make check`, `make live` and `make hook` call the same script. Exit code 0 means every gate passed.

The unit tests need Python and the `dev` extra only:

```bash
./install.sh --dev --no-host
.venv/bin/python -m pytest -o addopts="" -q tests/unit
```

### The live tests

Live tests are skipped unless `SF_LIVE=1`. Each one works in a throwaway farm home or on a throwaway container, names its Docker objects with a test prefix (`sftest` or another name that starts with `sft`), and removes them again, so it can run on a machine that hosts a real farm. They need Docker, the binder module, `adb` and a local Android image.

```bash
SF_LIVE=1 SF_LIVE_IMAGE=redroid/redroid:13.0.0-latest \
  .venv/bin/python -m pytest -o addopts="" -v tests/live/test_api_live.py
```

| Variable | Used by | Meaning |
| --- | --- | --- |
| `SF_LIVE` | all | `1` runs the live tests |
| `SF_LIVE_IMAGE` | most | the Android image to boot. Default `redroid:13-noflag`; `test_control_live.py` and `test_stream_live.py` default to `redroid/redroid:13.0.0-latest`. Images are not pulled by the tests. |
| `SF_LIVE_HELPER` | `test_core_lifecycle.py`, `test_e2e_farm.py`, `test_examples_live.py` | base image of the helper image (default `alpine:latest`) |
| `SF_LIVE_VENUS_RELAY`, `SF_LIVE_VENUS_RUNTIME` | `test_e2e_farm.py` | folders of a built relay and runtime; the NVIDIA scenario is skipped without them |
| `SF_LIVE_FPS_PROPERTY` | `test_e2e_farm.py` | extra boot property that carries the frame rate, for an image that needs one |
| `SF_LIVE_ANDROID` | `test_net_vpn.py` | `1` also boots one real Android behind a VPN sidecar and behind a gateway |
| `SF_LIVE_ANDROID_IMAGE` | `test_net_vpn.py` | the image for those two tests (default `redroid:13-noflag`) |
| `SF_LIVE_ADB_PORT` | `test_control_live.py`, `test_stream_live.py` | host port for the ADB of the throwaway Android |
| `SF_LIVE_CONTAINER` | `test_control_live.py` | name of the throwaway container |
| `SF_LIVE_SERIAL` | `test_stream_live.py` | use this running device and start nothing |
| `SF_LIVE_MEDIA_IMAGE`, `SF_LIVE_MEDIA_ADB_PORT` | `test_media_mic.py` | an Android 13 image with the camera overlay baked in; the Android half is skipped without it |
| `SF_LIVE_MODEL_URL`, `SF_LIVE_MODEL`, `SF_LIVE_MODEL_FORMAT` | `test_ai_live.py` | a running model server, the model, and its wire format |
| `SF_LIVE_OLLAMA_BIN`, `SF_LIVE_OLLAMA_MODELS` | `test_ai_live.py` | let the test start a model server itself |
| `SF_EXAMPLES_TRIM` | `test_examples_live.py` | `off` boots the Android with package trimming off |

To repeat the networking result, including the two Android tests:

```bash
SF_LIVE=1 SF_LIVE_ANDROID=1 \
  .venv/bin/python -m pytest -o addopts="" -v tests/live/test_net_direct.py tests/live/test_net_vpn.py
```

To repeat the lifecycle result, including NVIDIA:

```bash
SF_LIVE=1 SF_LIVE_VENUS_RELAY=<relay folder> SF_LIVE_VENUS_RUNTIME=<runtime folder> \
  .venv/bin/python -m pytest -o addopts="" -v -s tests/live/test_e2e_farm.py
```

### The installer on other distributions

```bash
tests/distro/run.sh                 # all five
tests/distro/run.sh fedora arch     # some of them
```

For each distribution a throwaway container runs `./install.sh --container-test --dev`, resolves every package name the farm knows for that family, runs `sf version`, `sf doctor`, `sf host plan`, `sf host capacity` and `sf host gpus`, builds and installs a wheel, and runs a part of the unit tests. Logs go to the folder named by `SF_DISTRO_OUT`. This check does not cover anything that needs a real kernel or init system.

### The host checks

`sf doctor` is read-only and needs no root. Run it on your own machine to see which of its 46 checks pass there; [TROUBLESHOOTING.md](TROUBLESHOOTING.md) explains each failure.

## Next

- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): when a check or a start fails
- [SECURITY_MODEL.md](SECURITY_MODEL.md): the limits that matter for security
- [DEVELOPMENT.md](DEVELOPMENT.md): writing and running tests
- [SCALING.md](SCALING.md): using the measured numbers
- [../CHANGELOG.md](../CHANGELOG.md): what each release contains
