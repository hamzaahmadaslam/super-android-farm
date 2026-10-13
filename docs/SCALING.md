# Scaling

For anyone sizing a machine for a farm, or finding out why it cannot hold as many Androids as expected.

Super Farm estimates how many Androids fit on the machine it runs on, says what limits that number, and lists what would raise it. The estimate is advice.

## The capacity estimate

```bash
sf host capacity                    # workload: video (default), the heaviest case measured
sf host capacity --workload light   # mostly idle screens
```

On the test machine (32 threads, 32 GB, Intel iGPU and NVIDIA card, NVIDIA runtime not built) it prints:

```text
About 33 Androids fit on this machine in intel mode (video workload); RAM is what runs out first.

LIMIT  MAX  BASIS
RAM     33  830 MB per Android, 31287 MB total, 3072 MB kept for the host
CPU     46  55% of one thread per Android, 32 threads

With the memory free right now, about 17 more would start.
```

followed by advice ordered by effect. The same data is in the panel on System and from `GET /api/system/capacity`.

### How it is computed

The figures are for the rendering mode an Android with the farm's default settings would get if it started now. That uses the same decision as a real start ([GPU](GPU.md#how-auto-chooses)), so an NVIDIA card whose runtime is not built counts as what `auto` falls back to, not as NVIDIA.

| Limit | Formula |
| --- | --- |
| RAM | `(total RAM − performance.reserve_host_mb) / RAM per Android`, rounded down |
| CPU | `threads × 100 × 0.80 / CPU percent per Android`, rounded down |
| Video memory | NVIDIA mode only: for each NVIDIA card, `(card memory − 1536 MB) / 360 MB`, summed |

The estimate is the smallest of the limits that apply, and `binding` names the one that is smallest (RAM, CPU or VRAM).

RAM per Android is 1000 MB, plus 300 MB in NVIDIA mode, minus 170 MB when `performance.trim_packages` is on (the default), so 830 MB in Intel, AMD or software mode and 1130 MB in NVIDIA mode. It is capped by the Android's memory limit (`resources.memory_mb`, 1536 by default), plus the 300 MB in NVIDIA mode because the relay's share lives on the host outside the container. `performance.reserve_host_mb` (default 3072) is what the machine needs for itself: kernel, Docker, the daemon, and a desktop if you run one. It is a setting, not a measurement. The reference machine's full desktop session with browsers used close to 10 GB before any Android started.

CPU percent per Android is a measured figure, in percent of one thread:

| Mode | Light (scrolling a settings screen) | Video (a live stream playing) |
| --- | --- | --- |
| software | 78 | 148 |
| Intel | 13 | 55 |
| NVIDIA | 15 | 66 |
| AMD | 13 (assumed equal to Intel) | 55 (assumed) |

An Android cannot use more than its `resources.cpus` cap, so the percentage is capped at `cpus × 100`. The farm plans for 80% of all threads (`CPU_TARGET_UTILISATION`): above that, frame times and ADB latency degrade for every Android at once, and the video benchmark ran the CPU at 85 to 90 C at 63%. In video, what remains on a GPU mode is decoding and ARM translation, which no GPU removes.

`More now` in the output is `(MemAvailable − performance.memory_guard_mb) / RAM per Android`: what would start given the memory free at this moment. It is lower than the estimate whenever something else on the machine is using memory.

With compressed swap in use or enabled, the output adds a figure for mostly idle Androids. It is computed from the measured compression ratio of 4.2 to 1 and about 500 MB of idle memory per Android that ends up in swap, and it is marked as extrapolated above 17 Androids, the largest fleet that was run that way.

### Memory per Android

An Android in use costs about 0.9 to 1.1 GB. In NVIDIA mode it is about 0.3 GB more, mostly inside NVIDIA's driver. Disabling 38 unused system packages (`performance.trim_packages`) saved about 170 MB per Android (1079 to 908 MB). 13 Androids in the same live video used about 27 GB on NVIDIA against 23 GB on Intel. Per-Android limits come from `resources.memory_mb`, `memory_swap_mb`, `pids_limit` (4096) and `oom_score_adj`; raise `oom_score_adj` to sacrifice Androids before the desktop.

### Advisory, enforce and force

The estimate is advice. By default (`performance.enforce_capacity: false`) starting more Androids than the estimate only warns: the start goes ahead and the task result says "over capacity ... Started anyway".

Two things count as over capacity: the number running has reached the estimate, and the RAM available is below `performance.memory_guard_mb` (default 1500). Androids that are still booting count as promised memory, so a bulk start does not see the same free RAM twice. `memory_guard_mb: 0` turns the RAM guard off.

With `performance.enforce_capacity: true`, a start over capacity is refused with the numbers (HTTP 409 `over_capacity`), unless it is forced:

```bash
sf config set performance.enforce_capacity true
sf start 1-40            # refused past the estimate
sf start 1-40 --force    # start anyway
```

In the panel the refusal comes with a "run anyway" choice.

## Tuning levers, in order of effect

1. Rendering mode ([GPU](GPU.md)). Moving from software to a GPU mode takes the CPU per Android from 78% to 13% of a core on UI screens and from 148% to 55% in video, and is the biggest single change. If CPU is the binding limit this is the first thing to fix. If RAM binds, it changes little.
2. More RAM. It is the only lever with no trade-off. Budget the per-Android figure above on top of what the host uses. If the machine has a desktop, a headless host gets the higher figure.
3. `performance.trim_packages` (on by default): about 170 MB less per Android and no Bluetooth crash loop at boot. Add package names to `trim_extra`, or protect some with `trim_keep`.
4. Compressed swap (zram). `sf config set performance.zram.enabled true`, then `sf host apply`. On the 32 GB reference desktop, 17 idle Androids ran healthy on 30 GB with a 16 GB zstd zram at `vm.swappiness=150` (set `performance.swappiness` for that), where about 13 fit before. 10 Androids had 1.9 GB swapped at a 4.2:1 ratio, 14 had 7.2 GB at 4.5:1, and 17 had 11.7 GB at 5.1:1, all healthy with 3.4 GB still available. Live video was not measured with zram and needs more memory per Android than a home screen. The cost is some CPU under memory pressure.
5. Mixing GPU modes: NVIDIA costs about 300 MB more RAM per Android than Intel or AMD for the same CPU load. Keeping idle or background Androids on the integrated GPU (`gpu_mode` per Android) fits more.
6. Frame rate (`android.fps`, default 20). It is a strong lever for UI-heavy screens and a weak one for video: going from 25 to 15 fps saved only about 4 points of CPU in a video app (91% to 87%).
7. Start pacing: `performance.start_concurrency` (default 4) Androids boot at the same time in a bulk start, with `performance.start_stagger_seconds` (1.0) between launches. Booting is the heaviest moment for the CPU. `performance.boot_timeout` (180 s) bounds the wait for `sys.boot_completed` before a start is reported failed.
8. `performance.disable_animations` (on): Android's animation scales set to 0, so fewer frames are drawn and automation is faster.
9. CPU pinning: `resources.cpu_pinning` is `none`, `spread` (each Android on its own cores, round-robin), or `performance` or `efficiency` on hybrid CPUs. Per Android, `resources.cpuset` takes explicit cores. Pinning was not benchmarked.
10. Resolution (`android.width`, `height`, `dpi`). The default is 480x854 at 160 dpi, and every figure on this page was measured there. How resolution moves the numbers was not measured.

Kernel same-page merging (`performance.ksm`) is experimental. Only processes that opt in are merged, so the gain for Androids is unproven.

Frame capture also costs the device: a screenshot at 480x854 takes about 55 ms raw, and the H.264 relay costs about 15 ms per frame of device CPU. `stream.max_viewers` (64) bounds live views.

## Large hosts and several GPUs

The kernel and Docker defaults are sized for a desktop. Host setup raises the kernel limits below, and the doctor checks them and the Docker address space at every run:

| Check | What it is for |
| --- | --- |
| `fs.inotify.max_user_instances` 8192, `max_user_watches` 1048576 | Android opens inotify instances freely, and all Androids' system processes share a few user ids. The default of 128 is exhausted by a handful of Androids. |
| `kernel.pid_max` 4194304 | One Android is roughly 80 processes and well over 1000 threads. The classic limit of 32768 ids is reached at about 30 Androids. |
| `fs.file-max` 2097152 | Open-file ceiling for the machine. |
| `net.ipv4.neigh.default.gc_thresh1..3` 4096, 8192, 16384 (and IPv6) | Every container is a neighbour-table entry. At the defaults the kernel starts dropping entries ("neighbour table overflow") and containers lose connectivity at random. |
| `net.ipv4.ip_forward` 1 | Containers reach the network through the host. |
| Docker address space | The doctor counts bridge networks against Docker's default pools (about 31 networks) and compares the Androids defined against the addresses on the default bridge. It fails when there are more Androids than addresses, and warns at 80% of the networks. The fix it prints is larger pools in `/etc/docker/daemon.json`. |

Values are floors: a machine that already has more keeps its value. `systemd` limits for the daemon include `LimitNOFILE=65536` for ADB connections and live views.

ADB ports are `network.adb_port_base + id` (5555 + id by default), so Android ids must keep the port in range, and the base must stay out of the kernel's outgoing port range, which the doctor checks in its "Ports" row. Ids go up to 9999. A shared VPN gateway holds at most 126 Androids ([Networking](NETWORKING.md)).

With several GPUs, `gpu.assignment` spreads Androids over the cards of one mode, and `gpu.mode: balanced` shares the farm across every card and then the CPU ([GPU](GPU.md#several-gpus)). In `nvidia` mode the video-memory limit sums over all NVIDIA cards. In `balanced` mode video memory is no limit on the whole farm, because what the cards cannot hold goes to the integrated GPU and the CPU, and the estimate counts that mix. On a server, set `gpu.vram_reserve_mb: 0` ([Servers](GPU.md#servers)). The relay is one process per card.

Disk: each Android needs 2 to 6 GB once apps are installed. The doctor warns below 5 GB free for the farm home and 20 GB for Docker's data. Backups go to `backup.dir` and add to it.

## What was run, and what was not

Run on one machine (32 threads, 32 GB, RTX 4080 SUPER plus the Intel iGPU, Android 13):

- The lifecycle through the full stack: create, start, boot, stop and start with data kept, per-Android overrides, capacity warn, enforce and force.
- 13 Androids in the same live video app in software, Intel and NVIDIA modes, for the CPU and memory figures above.
- 17 idle NVIDIA Androids for the video-memory and zram figures. Sixteen Linux containers and then 13 full Androids shared one relay.
- The panel in headless Chrome with 206 Androids rendered (generated data, not 206 real ones).

Not run: more than three real Androids at once through Super Farm itself. Fleet behaviour (bulk operations, the busy rule, per-device failure isolation, automation runs on many devices) is covered by unit tests with fakes, not by a large live fleet. The figures for AMD are assumed, resolution and CPU pinning were not benchmarked, and Android versions other than 13 were not booted.

## Next

- [GPU](GPU.md): the rendering mode is the biggest lever.
- [Install](INSTALL.md#settings-farm-defaults-and-per-android-overrides): how settings and per-Android overrides work.
- [Troubleshooting](TROUBLESHOOTING.md): what to do when starts fail or Androids are killed.
- [Configuration reference](CONFIGURATION.md#performance): every `performance.*` setting.
