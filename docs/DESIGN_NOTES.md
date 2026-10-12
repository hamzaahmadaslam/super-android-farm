# Design notes

For contributors and curious operators who want to know why Super Farm does things the way it does.

Each note has three parts: the problem as it showed up, its cause, and what the project does about it. Most of them come from running a farm of this kind before this code was written, and the rest were found by tests while it was written. [ARCHITECTURE.md](ARCHITECTURE.md) describes the result; this page gives the reasons.

## Networking

### Android rewrites the routing of the namespace it joins

Problem: an Android that shares a network namespace with a VPN client loses the tunnel's routes, and an Android recreated next to a running tunnel drops that tunnel.

Cause: Android's network daemon routes by policy rules into a table of its own per interface (table number = 1000 + the interface index of `eth0`). It removes rules it did not create and refills that table whenever it reconfigures the interface.

What Super Farm does: each Android runs in the namespace of a sidecar container, and a loop in the sidecar (`routesync.sh`) puts the needed state back every 2 seconds. It keeps a rule that sends the sidecar's own programs to the main table, and it copies every main route except the default one into Android's tables. The interface index is read, never assumed. The two containers are always removed and created together, sidecar first. The sidecar also never exits because the tunnel failed, since a restarted sidecar would leave the Android attached to a dead namespace; the tunnel is restarted in place.

### Traffic leaked whenever the tunnel was down

Problem: an Android behind a VPN sent traffic with the host's real address before the first connect and during every reconnect.

Cause: a plain default route sat next to the tunnel routes. With the tunnel down, the default route was the only one left.

What Super Farm does: a kill switch. The sidecar's firewall is default-deny for output, input and forwarding, and the order of its start is the security property:

1. load the default-deny rules for IPv4 and IPv6
2. resolve the VPN server names while only DNS to the configured resolvers is open, and pin the addresses in the firewall, the routes and `/etc/hosts`
3. close DNS on the uplink
4. start the tunnel

The default route of Android's own table is left in place on purpose. When the tunnel is down, traffic follows it to the uplink, where the firewall drops it. A live test proves the test itself: a control run with the kill switch off does leak.

### A reconnect needed DNS that the firewall had closed

Problem: in an earlier design DNS stayed open on the uplink while the tunnel was down, and a VPN host name that later resolved to a new address could not reconnect.

Cause: the firewall allowed only the address resolved at start, and lookups were either always open (a leak) or always closed (no reconnect).

What Super Farm does: the server addresses are pinned at start, so a reconnect needs no DNS. Only when every pinned address has failed does the sidecar open DNS to the configured resolvers for the few seconds of a new lookup, then pins the new addresses and closes it again.

### A rule set everyone assumed was there

Problem: after a rebuild, a set of sidecars ran old code and came up without the kill switch. Nothing reported it.

Cause: the firewall was set once and never checked, and a stale image could be reused without anyone noticing.

What Super Farm does: three things. A start is not reported ready until the live rules have been read back and checked against the property the farm relies on. The sidecar checks its own rules about every 30 seconds and loads them again when they are missing. The sidecar image's tag contains a hash of its build inputs, so editing a script changes the tag, the start plan changes, and sidecars are recreated with the new code.

A related bug was found in review: the IPv6 rules were skipped when IPv6 "looked absent", and it looked absent because the farm itself had disabled it by sysctl. The IPv6 default-deny is now loaded unless the kernel has no IPv6 at all, and a failure to load it fails the start.

### Installing packages at every container start

Problem: sidecars were slow to start, needed unprotected internet access before the VPN was up, and broke whenever a package mirror was unreachable.

Cause: the sidecar installed its tools at every start.

What Super Farm does: everything is installed when the sidecar image is built. The helper image used for backups follows the same rule.

### ADB reachable from the network

Problem: ADB, which has no authentication, was published on every interface, through a separate proxy container whose target address drifted.

Cause: the Android container has no network of its own to publish a port on, so a second container forwarded the port.

What Super Farm does: the sidecar owns the namespace, so it publishes the ADB port itself, bound to `network.adb_bind` (default `127.0.0.1`). With a loopback bind the sidecar's firewall accepts ADB connections only from the host, so other containers on the bridge cannot connect either.

### Docker facts that shaped the shared gateway

Problem: the obvious design for a shared VPN gateway, an `--internal` Docker network next to the default bridge, does not work.

Cause: Docker's built-in `bridge` network cannot be combined with another network on `docker run`. An `--internal` network drops every packet whose destination is outside its subnet, including what a member sends to the gateway for forwarding. Docker's interface names inside the container are not predictable, and Android runs DHCP on a second `eth<N>`.

What Super Farm does: the farm has its own uplink network. The gateway network is a bridge with NAT switched off and no address on the host, so the gateway container is the only router on it. Inside a member, the sidecar tells the two networks apart by address and renames the gateway interface so that Android leaves it alone.

## Host and kernel

### Kernel module auto-loading was off

Problem: on Ubuntu 26.04, Docker could not create a network or publish a port.

Cause: `/proc/sys/kernel/modprobe` was empty, so nothing was loaded on demand. `veth`, `xt_tcpudp` and `xt_nat` had to be loaded by name.

What Super Farm does: it never relies on auto-loading. Every module it needs is listed in `superfarm/host/modules.py`, loaded explicitly by `sf host apply` and again at every boot, and checked by `sf doctor`. The kill switch uses only the firewall matches Docker itself depends on (interface, address, port, connection state, masquerade), because anything else may not be loadable on such a host.

### Secure Boot rejects a module you built yourself

Problem: the virtual camera needs the `v4l2loopback` kernel module, and with Secure Boot on the kernel refuses to load it.

Cause: the kernel loads only modules signed by a key the firmware trusts. A module built on the machine is unsigned, and it has to be rebuilt and signed again after every kernel update. Recent kernels (6.18 and later) also changed an interface the upstream module used, so it did not compile.

What Super Farm does: `native/v4l2loopback/` holds a patch for the kernel change and a build script, and `sf host sign-module` wraps a script that creates and enrols a machine owner key and signs the module. Only the virtual camera is affected; nothing else in the farm needs an out-of-tree module. The signing path needs root and has not been run as part of this project's tests.

### A daemon running as root has no desktop

Problem: native device windows did not open and a device's sound could not be played.

Cause: the control panel ran as root, outside the desktop session of the person at the machine.

What Super Farm does: the daemon runs as your user, as a systemd user service that inherits the session's display and sound. Root is used only for one-time host setup and for a boot unit that reloads modules and recreates devices.

## Graphics

### An NVIDIA card cannot be used the way Intel and AMD cards are

Problem: pointing Android's normal GPU mode at an NVIDIA render node fails at boot: the allocator cannot create its device and no EGL configuration is found.

Cause: Android's graphics stack in these images is Mesa, and Mesa has no driver for NVIDIA's proprietary stack.

What Super Farm does: an NVIDIA Android draws through a relay. GLES goes through ANGLE to Vulkan, the Mesa Venus driver sends Vulkan over a Unix socket, and a patched `virgl_test_server --venus` on the host hands it to the NVIDIA driver. A custom allocator gives out buffers the card can draw into. `native/venus/README.md` documents the build and these findings, each of which was a failing test first:

- The relay could create buffers but not import one, which made sharing a buffer between two Android processes impossible. A new relay command adds the import.
- NVIDIA rejects a non-dedicated import of such a buffer for most sizes, so the driver always uses a dedicated one.
- The card renders only into its tiled layouts. Every RGB buffer is tiled, and the allocator converts to and from plain rows when the CPU locks one.
- Video and camera formats are offered only for sampling, in a linear layout, which is what they need.
- The 32-bit allocator exists because Android 13's camera service is a 32-bit program.

### Restarting the relay takes every NVIDIA Android down

Problem: after a relay restart, every NVIDIA Android lost its picture until it was restarted too. A script that stops the relay with `pkill -f` also matches its own shell.

Cause: each NVIDIA Android holds connections to the relay, and `pkill -f` matches the whole command line, including the caller's.

What Super Farm does: the relay is started before the first NVIDIA Android and then left alone. The daemon's unit stops only the daemon process (`KillMode=process`). Relay processes are recognised by their exact process name and their socket path.

### Apps crashed after a change of renderer

Problem: after an Android was switched from one rendering mode to another, apps crashed at start.

Cause: apps keep compiled shaders in a cache built for one renderer.

What Super Farm does: the last renderer is remembered in the farm's state and in the data volume. When it changes, the apps' shader caches are deleted before the Android starts (`gpu.clear_shader_cache_on_change`).

### The frame rate setting had no effect on one image

Problem: changing the frame rate did nothing on an image with a patched compositor.

Cause: the stock image reads the rate from the boot argument `androidboot.redroid_fps`. The patched build read it from a property of its own.

What Super Farm does: `android.images.<version>.fps_property` names an extra boot property that carries the rate for such an image. The standard argument is always passed as well.

## Android and devices

### A model name with a space corrupted the device identity

Problem: an Android configured as `Pixel 7` reported its model as `Pixel`.

Cause: Android's init splits its command line on whitespace, so everything after the space became a separate argument.

What Super Farm does: boot property values with spaces are refused, when a preset is loaded and again in the planner, and the shipped device presets contain no model with a space.

### Typing text is harder than it looks

Problem: typed text arrived wrong, or not at all. `100% sure` arrived as `100%% sure`, and text outside ASCII typed nothing.

Cause: Android's `input text` wants a space as `%s`, reads a literal `%` followed by `s` as that escape, and builds key events from a character map that has no entries outside ASCII.

What Super Farm does: ASCII text is split into chunks so that a `%` and a following `s` never travel together. Text with other characters is set as the device clipboard and pasted, through a short control-only scrcpy-server session; this needs scrcpy on the host and replaces the clipboard's content. All 95 printable ASCII characters were typed and read back in a live test. Automation flows type printable ASCII only.

### A restored copy must not appear next to the original

Problem: restoring a backup while the original Android still runs puts two devices with the same identity online.

Cause: the device identity belongs to the data and is restored with it.

What Super Farm does: a backup restored as a new Android is created with network mode `none` when the original still exists, unless you pass `--keep-network`.

### A restored `/data` booted but apps had lost their data

Problem: a data volume copied with ordinary tools boots, and then apps crash or start empty.

Cause: Android keys file access on numeric owners and extended attributes.

What Super Farm does: backups run GNU tar in a helper container with `--xattrs --xattrs-include='*' --acls --numeric-owner --sparse`, and restores use the same options. A live test compared owners, modes and 362 extended attributes after a restore and found them identical.

## Automation

### Reading the screen can hang, or return an old screen

Problem: a run stalled for tens of seconds on a moving screen, or tapped things that were no longer there.

Cause: `uiautomator dump` waits for the screen to go idle. A dump that fails can also exit with success and leave an older file in place, so reading a fixed path returns a screen from minutes ago.

What Super Farm does: every read dumps to its own unique path and deletes it afterwards, so a missing file is a failure and never a stale answer. Every read has a hard time limit enforced from outside, and a cancelled run kills the read. For screens that never go idle there is an optional persistent reader (`automation.hierarchy_reader: u2`) that asks with no idle wait. On a busy screen it was about 20 times faster than the built-in dump.

### Recordings described the wrong screen

Problem: a recorded tap was attributed to an element from the previous screen.

Cause: the recorder sampled the screen on a timer and paired each tap with the newest sample. A tap made right after a screen change raced with the sampling.

What Super Farm does: recording intercepts the tap. When you click on the live view, the recorder reads the element tree, resolves the element under the point, saves a picture of the area, and only then sends the tap. The app has not seen the tap yet, so the tree describes exactly what the tap will hit.

### Five recorded steps trained down to one

Problem: training several recordings of one task produced a program with most steps missing.

Cause: steps were matched exactly across recordings. The same physical tap can be captured with a different element each time, and every disagreement threw the step away.

What Super Farm does: training is program synthesis by consensus. Recordings are aligned by similarity (Needleman-Wunsch), a step is kept when at least half the recordings made it, its element is decided by majority vote and its coordinates are the median. A language model may describe the steps but cannot add, remove or reorder them. Training reports what it left out and why.

### A pop-up rule dismissed a pop-up that was not there

Problem: a run looped forever "closing" an overlay.

Cause: the rule looked for a text only, and that text was also a permanent label on the page behind.

What Super Farm does: a pop-up rule needs an exact guard element and its dismiss control, fires only when both are on screen, and only a couple of times per step. A never-tap list is checked on every tap, including taps a recovery proposes, and a recovery model may press only Back and Escape, so it cannot reach a forbidden control with the keyboard.

### A loose match hit the wrong element

Problem: a step's element matched a different control and the run carried on from the wrong place.

Cause: a resource id was accepted when it merely contained the recorded one, so `title` matched `rate_title` and reached the threshold alone.

What Super Farm does: evidence is scored (`automation.match_threshold`), and a resource id counts only on an exact match. A device that meets a screen no rule covers stops with `needs attention` and waits for a person while the other devices carry on.

### A consumed value was handed out twice

Problem: a one-time value from a pool, such as an invite code, could be used again after a crash.

Cause: the value was removed from the pool first and recorded as used afterwards.

What Super Farm does: the line is written to a used-journal before it is removed from the pool. A crash between the two can lose one value. It can never reuse one.

## Operations and the API

### A double click did the work twice

Problem: two deletes of one Android both answered "deleted", and a start that had waited for a delete wrote state for an id that no longer existed, so the next Android with that id showed its error.

Cause: nothing prevented two operator requests from working on the same Android at once.

What Super Farm does: one operator request per Android at a time. Work that changes what an Android is doing is submitted as exclusive, and a second request that names a busy Android is refused as a whole with `409 busy` and the id of the task that holds it. It is neither queued nor joined. `POST /api/instances` also accepts an `Idempotency-Key`, so a repeated create returns the first answer.

### A refused restart left the Android stopped

Problem: with the capacity limit enforced, restarting a running Android stopped it and then refused to start it again.

Cause: the restart stopped first and asked afterwards.

What Super Farm does: every refusal (capacity, data version, image, GPU, network) is raised before anything is stopped. A running Android that is restarted or rebuilt in place is already counted and is not a capacity question at all.

### A saved settings file would not load again

Problem: after one field of one Android image entry was changed, `config.yaml` could not be read back.

Cause: `config.yaml` holds only what differs from the defaults. The save wrote only the changed field of the entry, but a free-form map in the file (`android.images`, `network.gateways`) replaces the default map entirely, so the entry in the file lacked its required fields.

What Super Farm does: only settings sections are compared field by field. A free-form map is written whole as soon as anything in it differs. A unit test saves a partial entry and loads it again.

### The daemon died before it had cleaned up

Problem: stopping the daemon with SIGTERM left its helper processes behind.

Cause: the HTTP server handles the signal and, after shutting down, raises it again, which ended the process before the farm's own shutdown ran.

What Super Farm does: the daemon holds the signal handlers itself, so the shutdown runs to its end. A live test checks that SIGTERM exits cleanly and that the Androids keep running.

### One slow device held up the fleet

Problem: a bulk operation stalled because one Android did not answer.

Cause: a busy Android can stall any ADB command, and the operation waited for each device in turn.

What Super Farm does: every ADB and Docker call has a timeout. Bulk work runs with bounded concurrency, each device has its own outcome, and one that fails or stalls never aborts the rest.

## Camera and sound

### Reading the camera service's state killed it

Problem: the camera HAL died during a status check.

Cause: `dumpsys media.camera` was piped into a command that closed the pipe early. The HAL was still writing and received SIGPIPE.

What Super Farm does: the output is always read to the end.

### Files copied into `/vendor` had the wrong owner

Problem: after the camera overlay was copied into a running container, files and folders under `/vendor` belonged to Android's `system` user.

Cause: a plain `docker cp` run by an ordinary user carries that user's numeric id, which inside Android is usually `system`.

What Super Farm does: the overlay is streamed in as a tar archive with every file owned by root.

### Listening to a device was almost silent

Problem: the captured sound of an Android was about 30 dB too quiet.

Cause: captured sound passes through a device with its own volume, which exists only while something captures. Setting the media volume before the capture started changed the wrong device.

What Super Farm does: the volume is raised a few seconds after the capture has started.

## Next

- [ARCHITECTURE.md](ARCHITECTURE.md): the design these notes explain
- [SECURITY_MODEL.md](SECURITY_MODEL.md): what the kill switch and the approvals step guarantee
- [VERIFICATION.md](VERIFICATION.md): which of these behaviours were tested, and how
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): symptoms and fixes
- [DEVELOPMENT.md](DEVELOPMENT.md): working on the code
