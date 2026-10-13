# GPU

For anyone choosing how Androids draw: on the CPU, on an integrated or discrete GPU, on an NVIDIA card through the relay, or shared across all of them.

The rendering mode is the biggest lever on CPU load after the number of Androids. The numbers in the measured sections come from one test machine; [Scaling](SCALING.md) shows how they feed the capacity estimate.

## The modes

| Mode | What it does |
| --- | --- |
| `auto` | The best mode that works now. Never fails: with no usable GPU it draws on the CPU. |
| `balanced` | Shares the farm across every usable card, the emptiest first, and puts what the cards cannot hold on the CPU. For machines with several GPUs. See [Several GPUs](#several-gpus). |
| `software` | Draws on the CPU (ANGLE with SwiftShader). Always works. |
| `igpu` | The integrated GPU, whoever made it. |
| `dgpu` | A discrete card, whoever made it. |
| `intel` | An Intel GPU, through Mesa. |
| `amd` | An AMD GPU, through Mesa. |
| `nvidia` | An NVIDIA card, through the Venus relay. Needs the NVIDIA runtime to be built. |
| exact card | A mode plus a pinned card: a render node such as `/dev/dri/renderD129`, or the card's PCI address such as `0000:01:00.0` (`gpu_node` per Android, `gpu.render_node` for the farm). The card must be one the mode allows. |

Intel and AMD cards work out of the box. Android's graphics stack is Mesa, Mesa has drivers for those cards, and the Android container gets the card's render node. NVIDIA's proprietary driver is not Mesa, so NVIDIA Androids use a different path, described [below](#nvidia-through-the-relay).

A mode you name must work, or the start of that Android fails with a message that says what is missing and lists the choices your machine does offer. `auto` and `balanced` fall back instead.

## How auto chooses

1. `gpu.prefer` decides which kind `auto` tries first when a machine has both: `dgpu` (the default) or `igpu`. Set `igpu` to keep the discrete card free for other work and to draw the least power.
2. Within a kind, an NVIDIA card is tried first, because it is the fastest when its relay is available. A card is passed over, with the reason recorded, when the NVIDIA runtime is not built, the Vulkan driver manifest (`gpu.nvidia.icd`) is missing, or the relay does not start.
3. Otherwise an Intel or AMD card of that kind is used.
4. If the first kind has no usable card, the other kind is tried.
5. If nothing is usable, `auto` draws on the CPU and the reason is shown.

The decision is made again at every start and is deterministic: the same Android gets the same card unless the configuration changed, because a different card means a rebuilt container. On the test machine, which has an Intel iGPU and an NVIDIA card but no NVIDIA runtime in the default home, `auto` skipped the NVIDIA card and used the Intel iGPU. The reason appears in `sf host capacity` as "NVIDIA skipped: the Venus runtime or relay is not built".

## See what this machine offers

```bash
sf host gpus
```

This reads `/sys` only, needs no daemon and no root. On the test machine:

```text
NODE                 VENDOR  KIND        NAME                                  DRIVER  VRAM      MODES   NOTE
/dev/dri/renderD128  intel   integrated  Raptor Lake-S GT1 [UHD Graphics 770]  i915    -         intel   -
/dev/dri/renderD129  nvidia  discrete    AD103 [GeForce RTX 4080 SUPER]        nvidia  16376 MB  nvidia  -

Rendering modes usable now: software, igpu, intel
NVIDIA is not usable yet: the Venus runtime or relay is not built (gpu_config.sh, vulkan.virtio.so, gralloc.venus.so and 4 more missing); build it with `.../native/venus/build.sh all` (see README.md next to it)
```

The `MODES` column is what each card can serve, `KIND` is integrated or discrete, and the last line lists the modes usable right now. `sf gpu ls` shows the same cards together with every rendering choice and why an unavailable one is unavailable (it needs the daemon). In the panel, the Rendering field of the New Android form and the per-Android settings lists the same choices with the reason under each unavailable one. `sf doctor` has the rows "GPUs", "Render node permissions" and, for NVIDIA, "NVIDIA driver", "NVIDIA Vulkan driver", "NVIDIA runtime for Android (Venus)" and "NVIDIA relay".

How a card is classed: sysfs gives the driver and PCI position. An Intel card at PCI address `00:02.0` is the integrated GPU, and Intel Arc cards sit behind a PCIe port. NVIDIA cards are always discrete. For AMD the class is derived from the PCI layout (processor graphics sits on bus 00, or beside the processor's other functions) rather than from model names. That AMD logic was not exercised on AMD hardware.

## Set the mode

For the whole farm (the default Androids follow):

```bash
sf config set gpu.mode igpu
```

For new Androids as you create them:

```bash
sf create -n 4 --gpu intel
```

For one Android or a selection, and in bulk:

```bash
sf gpu set 1-8 igpu                                  # a selection
sf gpu set 9 nvidia --node /dev/dri/renderD129       # pin one card
sf gpu set 9 nvidia --node 0000:01:00.0              # the same card by PCI address
sf gpu set 9 default                                  # follow gpu.mode again
sf gpu set 9 nvidia --node ""                         # unpin the card
sf set 3 gpu_mode=intel                               # the same through the general per-Android command
```

In the panel: Androids, tick the Androids, Change, Rendering…. A single Android's page has the same field under Settings. An emptied field returns the setting to the farm default.

A change reaches an Android at its next restart. When the renderer changes, the container is rebuilt and the apps' compiled-shader caches are cleared (`gpu.clear_shader_cache_on_change`, on by default), because a cache built for another renderer makes apps crash at start. App data is kept.

| Setting | Default | Meaning |
| --- | --- | --- |
| `gpu.mode` | `auto` | Farm default mode. |
| `gpu.prefer` | `dgpu` | Which kind `auto` tries first. |
| `gpu.render_node` | empty | Force one card for intel, amd or nvidia: a `/dev/dri/renderD*` node or a PCI address. Empty detects by driver. |
| `gpu.assignment` | `round-robin` | Which card a new Android gets when several fit one mode. |
| `gpu.balance.per_igpu`, `per_dgpu` | `0` | `balanced`: the most Androids one integrated GPU or one discrete card takes. `0` works it out, see below. |
| `gpu.balance.overflow` | `software` | `balanced`, once every card is full: `software` draws on the CPU, `refuse` fails the start. |
| `gpu.vram_reserve_mb` | `1536` | Video memory of each discrete card left to everything else. Set `0` on a headless server. |
| `gpu.clear_shader_cache_on_change` | `true` | Clear shader caches when the renderer changes. |
| `gpu.nvidia.*` | | Runtime and relay locations, see below. |

The reference for every `gpu.*` setting is [Configuration reference](CONFIGURATION.md#gpu).

## Several GPUs

There are two ways to use more than one card.

**One mode, several cards that fit it.** With `nvidia`, `dgpu`, `amd` or `auto`, only the cards of that kind are used and `gpu.assignment` picks among them:

- `round-robin` (default): by Android id, so Android N lands on card `N mod number-of-cards` and keeps it across restarts.
- `first`: always the first card in node order.
- `least-instances`: the card with the fewest running Androids.

**`balanced`: every card, then the CPU.** `sf config set gpu.mode balanced` shares the farm across the integrated GPU, every discrete card of any vendor, and the CPU:

1. Each discrete card takes as many Androids as its video memory holds: `(memory - gpu.vram_reserve_mb) / 360 MB`, the measured figure per Android at 480x854. A 24 GB card on a headless server takes 68, a 4 GB card 11, a 2 GB card under a desktop one. `gpu.balance.per_dgpu` replaces the figure with yours.
2. An integrated GPU has no limit of its own, because it draws from system RAM. The memory it reports is a reservation made by the firmware, not a ceiling. `gpu.balance.per_igpu` gives it one.
3. A new Android goes to the emptiest card, measured as running Androids against what the card takes. A large card therefore gets proportionally more than a small one, and equal cards get equal shares. A card with no limit is measured against the largest limit among the others. `gpu.prefer` breaks a tie.
4. An Android keeps the card it used last while that card has room, so a restart does not rebuild its container.
5. When every card is full, further Androids draw on the CPU (`gpu.balance.overflow: software`, the default) or their start fails with the reason (`refuse`).
6. A card that cannot be used now is passed over with the reason shown: an NVIDIA card whose runtime is not built, or whose relay does not start.

`sf host capacity` counts a balanced farm Android by Android and prints who gets what, for example "39 on NVIDIA card A, 39 on NVIDIA card B, 24 on the CPU". An Android on the CPU costs more than twice the CPU of one on a card, so the estimate shows how many of those the processor can carry.

To put particular Androids on particular cards, pin them: `sf gpu set 9 nvidia --node 0000:81:00.0`, or use the exact-card entries in the panel's Rendering picker. A mode and a card that do not match (for example `igpu` with the NVIDIA card) fail with a message naming the card and why it cannot serve that mode.

**Pin by PCI address on a machine with several cards.** The kernel hands out `renderD128`, `renderD129` and so on in the order the drivers load, and that order can differ from one boot to the next. A PCI address names the same slot every time. `sf host gpus` prints both. The `/dev/dri/by-path/pci-…-render` links that udev creates are accepted too.

**Several NVIDIA cards.** The relay is one process per card, shared by every Android on that card. Its `--rendernode` option does not choose the Vulkan device: the relay offers Android every device the host's Vulkan loader lists, and Android takes the first. With two or more NVIDIA cards the farm therefore starts each relay limited to its own card, through Mesa's device-select layer on the host (`DRI_PRIME=pci-…!`). That layer comes with the distribution's Mesa Vulkan packages, and the "GPUs" row of `sf doctor` says whether it is installed. `gpu.nvidia.isolate_cards: false` switches this off. On a machine with one NVIDIA card nothing is set. This has not run on a machine with two NVIDIA cards, see [Not verified](#not-verified).

## Servers

A server differs from the desktop the numbers were measured on in four ways the farm knows about:

- **No desktop holds video memory.** Set `gpu.vram_reserve_mb: 0`, or the estimate and `balanced` keep 1.5 GB of every discrete card free for a session that is not there.
- **No login session grants access to the cards.** On a desktop the logged-in user reaches `/dev/dri/renderD*` through the session. Over SSH, and in a user service that keeps running after logout, that grant is absent. Intel and AMD cards are not affected, because Docker passes the node to the container itself. The NVIDIA relay opens the card as your user, so your user must be in the group that owns it: `sudo usermod -aG render $USER`. `sf doctor` warns in its "Render node permissions" row when access rests on the session alone, and `sf host apply` adds the group.
- **Devices that are not GPUs.** A management controller's display (ASPEED, Matrox) has no render node and is never listed. A virtual GPU inside a virtual machine has one, is listed by `sf host gpus` as unusable, and is never chosen.
- **No display or sound.** Native device windows and listening on the host's speakers need a desktop session. `sf doctor` skips that row on a machine with no desktop installed. The panel, live views and everything else work, reached through an SSH tunnel to the panel's port ([FAQ](FAQ.md)).

## NVIDIA through the relay

In plain words: an NVIDIA Android does not talk to the card directly. Inside Android, apps draw with OpenGL ES, which ANGLE (already in the image) turns into Vulkan. The Vulkan driver inside Android is a Mesa driver called Venus. Venus does not touch the card either: it sends the Vulkan calls over a unix socket to a relay program running on the host as your user (a patched `virgl_test_server`), and the relay calls NVIDIA's own Vulkan driver. Window and video buffers are allocated on the card by a small custom allocator and passed between Android processes as dma-bufs.

What this means in practice:

- You build the runtime and the relay yourself, once. [Install](INSTALL.md#nvidia-mode) lists the prerequisites and the command, and [native/venus/README.md](../native/venus/README.md) explains the design, the NVIDIA-specific findings and the rules for running the relay. The full build from source (`native/venus/build.sh all`) has not been run end to end. The test machine used a prebuilt runtime and relay.
- The relay is started by the daemon before the first NVIDIA Android starts and is never restarted while Androids use it. If it stops, every NVIDIA Android on that card loses its graphics until it is restarted. That is why the user service uses `KillMode=process` and why the user service should have lingering enabled on a farm that must stay up ([Install](INSTALL.md#8-start-the-daemon)).
- The runtime is built for Android 13 x86_64 only. 32-bit apps have no NVIDIA driver (the allocator is also built 32-bit, because Android 13's camera service is a 32-bit program). Other Android versions need a rebuild and have not been tried.
- The farm refuses to claim NVIDIA mode where it is not ready: `auto` skips it and a named `nvidia` mode fails with the missing files listed.

NVIDIA settings (the first two are not changeable through the API):

| Setting | Default | Meaning |
| --- | --- | --- |
| `gpu.nvidia.runtime_dir` | `<home>/overlays/venus` | Folder with `vulkan.virtio.so`, `gralloc.venus.so`, `gralloc.venus.x86.so`, `gpu_config.sh`. |
| `gpu.nvidia.relay_dir` | `<home>/overlays/venus-relay` | Install prefix of the relay. API-immutable. |
| `gpu.nvidia.icd` | `/usr/share/vulkan/icd.d/nvidia_icd.json` | The NVIDIA Vulkan driver manifest on the host. API-immutable. |
| `gpu.nvidia.socket_dir` | `/run/superfarm/venus` | One socket folder per card. |
| `gpu.nvidia.debug` | `false` | Verbose driver logging inside Android. Costs CPU. |
| `gpu.nvidia.isolate_cards` | `true` | With several NVIDIA cards: limit each relay to its own card. No effect with one card. |

## Measured numbers

All from the one test machine (Intel Core i9-14900K, 32 threads, 32 GB, RTX 4080 SUPER and the Intel iGPU), Android 13 at 480x854. Treat them as starting points. A different app mix moves them a lot.

Renderer strings that Android reported in each mode:

| Mode | Renderer string |
| --- | --- |
| software | `ANGLE (… SwiftShader …)` |
| Intel | `Mesa Intel(R) Graphics (RPL-S), OpenGL ES 3.2 Mesa 24.0.8` |
| NVIDIA | `ANGLE (NVIDIA, Vulkan 1.3 (NVIDIA Virtio-GPU Venus (NVIDIA GeForce RTX 4080 SUPER)) …)` |

| Measure | Software | Intel iGPU | NVIDIA |
| --- | --- | --- | --- |
| Time to boot | about 11 s | about 17 s | about 11 s |
| CPU per Android, 13 Androids scrolling a settings screen | 78% of a core | 13% | 15% |
| CPU per Android, 13 Androids in a live video app (the capacity estimate uses these) | 148% | 55% | 66% |
| Memory per Android | not measured | about 0.9 to 1.1 GB in use | about 0.3 GB more than Intel |

Other measurements:

- Video memory: 17 NVIDIA Androids idling in an app held 7.6 GB on a 16 GB card, of which a desktop session accounted for about 1.5 GB. That is about 360 MB per Android at 480x854, from one measurement of one app.
- The relay used about 0.6% of one core per Android in a live video.
- Sixteen Linux containers and later 13 full Androids shared one relay with no errors.
- NVIDIA mode saves CPU, not power: 13 Androids on the NVIDIA card drew about what 9 drew in software mode, and the integrated GPU was the cheapest per Android.
- Software rendering costs roughly five times the CPU of a GPU mode on plain UI screens and more than twice in video.

With the default settings the capacity estimate on the test machine is about 33 Androids in Intel mode, limited by RAM. The estimate and how it is computed are in [Scaling](SCALING.md).

## Not verified

- AMD: no AMD hardware was available. The code path is the same Mesa path as Intel, the AMD CPU figures in the capacity estimate are assumed equal to Intel's, and the integrated-versus-discrete detection for AMD has never run on a real AMD machine.
- NVIDIA on Android versions other than 13, and 32-bit apps on NVIDIA.
- Hardware video decoding on the card. Video is decoded on the CPU.
- Only one card model and one driver series (an RTX 40-series card, NVIDIA open kernel module 595) were tested.
- Intel Arc and multi-card machines. The assignment rules and `balanced` are unit-tested against described machines (four equal cards, a large and a small card, cards in other PCI domains), and the test machine has one card of each vendor. On it, `balanced` was run live across the integrated GPU and the CPU only, see [Verification](VERIFICATION.md).
- Two or more NVIDIA cards. Limiting each relay to its card relies on Mesa's device-select layer wrapping NVIDIA's driver; neither that nor NVIDIA's behaviour without a display server on several cards was tried.
- A headless server. The server rules above come from how the kernel, udev and the drivers are documented to behave, not from a run on one.

## Next

- [Install](INSTALL.md): the NVIDIA prerequisites and the build command.
- [Scaling](SCALING.md): how the rendering mode feeds the capacity estimate.
- [Troubleshooting](TROUBLESHOOTING.md): what to do when a mode is unavailable or apps crash after a renderer change.
- [Configuration reference](CONFIGURATION.md#gpu): every `gpu.*` setting.
