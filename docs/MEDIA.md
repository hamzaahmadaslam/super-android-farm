# Media

For anyone who feeds a video or live stream into the camera and microphone of Androids, listens to an Android, or opens native device windows.

Three things live here:

- the virtual front camera and microphone, which show an Android a video file or a live stream you choose;
- listening to one Android on the speakers of the machine the daemon runs on;
- native scrcpy windows on that machine's desktop.

## Virtual camera and microphone

### What you must build yourself

The camera needs pieces that Super Farm does not distribute. [Install](INSTALL.md#virtual-camera-and-microphone) lists the steps:

1. the `v4l2loopback` kernel module, built from the project's patch and signed if Secure Boot is on;
2. a camera HAL overlay that you extract from a BlissOS (Android 13) image you download yourself;
3. `media.camera.enabled` on, `sf host apply` to create the loopback devices, and the Androids recreated.

This is Android 13 only. The camera HAL and the microphone shims are binary-specific to Android 13's vendor interface, and nothing here is expected to load on 12, 14 or 15. The host must be x86-64.

Not verified: the camera picture end to end. It needs a loopback device created by root, which was not done on the test machine. What was verified: the microphone path (sound injected into an Android was recorded at source level, and mute produced digital silence), and the steps that run inside the container. The pieces for the picture are in place and unit-tested, and [native/camera/README.md](../native/camera/README.md) lists exactly which build steps were checked.

`sf camera status` says whether the camera is ready, and every reason it is not, with the fix. The checks cover: `media.camera.enabled`, the loaded module, missing or foreign loopback devices, device permissions, `ffmpeg`, `ffprobe` and `v4l2-ctl`, and the overlay files for the default Android version and for Android 13.

### How it works

One video playing to fifty Androids costs one decode and one encode:

```text
source     one ffmpeg per feed: decode, scale, encode MJPEG once, tee to a FIFO per Android
copier     one small ffmpeg per Android: copies frames into its /dev/video device, never closing it
forwarder  one small Python process per Android: paces the sound into a microphone FIFO,
           which is bind-mounted into the Android as /dev/farm_mic
```

Changing the video replaces only the source, so an app that has the camera open sees a new picture on the same device. Android N's camera is `/dev/video(100 + N)` on the host (`media.camera.device_base`, for N from 1 to `media.camera.slots`, 32 by default) and appears inside the Android as `/dev/video0`. The container is privileged and inherits every host video node, so those are removed inside the Android and only its own remains. Loopback devices that Super Farm did not create are never written to.

The picture is 640x360 landscape at 20 fps (`media.camera.width`, `height`, `fps`), with the upright portrait picture rotated into the landscape frame the way a phone's front sensor reports it. `media.camera.quality` (default 5) is the MJPEG quantiser: lower is better and larger.

### Sources

A source is a file in the media library or the live RTMP ingest. The library is the folder `<home>/media/`. It takes video (`.mp4`, `.mkv`, `.mov`, `.webm`, `.avi`, `.m4v`, `.ts`, `.flv`) and audio (`.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.flac`) files. Add one in the panel on the Media page, with `POST /api/media`, or by copying a file into the folder.

To test a camera and a microphone, make a clip that answers the questions a test has:

```bash
scripts/camera/make_test_clip.py "$SUPERFARM_HOME/media/test_bars_beeps.mp4"
```

It is portrait 9:16 with a red stripe down the left edge and a running clock with a frame counter, and short beeps that step through 400, 800, 1200 and 1600 Hz. Do not test sound with a steady tone: voice and live-video apps run noise suppression, which removes a stationary tone completely.

### Play, stop and mute

```bash
sf camera status
sf camera play 1-5 intro.mp4                  # picture and sound of the file
sf camera play 3 rtmp --sound none            # live stream, picture only
sf camera play 4 intro.mp4 --sound music.mp3  # the file's picture, another file's sound, looped
sf camera stop 1-5                            # no selector stops every Android
sf camera mic 3 solo
```

`--sound` is `source` (the video's own sound, default), `none` (picture only, the microphone stays silent), or another library file. Whatever an Android was playing before is replaced. Androids that watch the same source with the same sound share one feed.

The microphone commands act within half a second and restart nothing:

| Mode | Effect |
| --- | --- |
| `mute` | The selected Androids record silence, whatever is playing. |
| `unmute` | The selected Androids hear their feed again. `unmute` on an empty selection unmutes every Android. |
| `solo` | The selected Androids are unmuted and every other Android is muted. |

In the panel: Media. Pick the Androids with a selector, a source and a sound, and use the buttons for play, stop and the microphone. A running feed is listed with its Androids.

An Android gets a camera when `camera` is on for it (`sf set 3 camera=true`), or when `media.camera.enabled` is on and the Android has not opted out. The microphone FIFO is part of how the container is created, so recreate an Android after turning the camera on for it. `POST /api/camera/attach` shows the camera to running Androids, and `POST /api/camera/install-hal` streams the overlay into a running Android's `/vendor`; that copy lives in the container and is lost when the container is recreated. Baking the overlay into the image is the lasting way ([native/camera/README.md](../native/camera/README.md)).

### Mirroring

Apps treat a front camera as a mirror. `media.camera.mirror` is on by default because, in a live broadcast test, the viewer and the broadcaster's preview saw the source the right way round only with the flip. Turn it off if text in your source appears reversed.

### RTMP ingest (live sources)

The source `rtmp` plays a live stream, for example from OBS. The ingest listens on `media.camera.rtmp_bind` (default `127.0.0.1`) and `media.camera.rtmp_port` (default 1935). Point OBS at:

```text
Server:      rtmp://127.0.0.1:1935/live
Stream key:  camera
```

The Media page shows the server and key.

The ingest has no authentication. The stream key is not checked, and the listener accepts a publisher whatever application and key it names. The only protection is the address it listens on. Anything that can reach that address becomes the camera picture and microphone of the Androids. That is why `media.camera.rtmp_bind` stays on loopback, and why a non-loopback value is refused unless `media.camera.rtmp_allow_remote` is on, which you should do only on a network where every machine is trusted.

For OBS on another PC, use an SSH tunnel instead:

```bash
ssh -L 1935:127.0.0.1:1935 you@farm-host
```

and point OBS at `rtmp://127.0.0.1:1935/live` on that PC. Only one live ingest can run at a time, because it binds the port; Androids that choose `rtmp` join the same feed.

## Listen to one Android

```bash
sf listen 3        # play Android 3's sound on this machine's speakers
sf listen          # show which Android is being listened to
sf listen off
```

In the panel it is on the Media page. A second scrcpy without window, picture or control carries only that Android's sound, uncompressed, through the audio session of the user the daemon runs as. Only one Android is heard at a time, because two streams on one pair of speakers cannot be told apart, and playback is disabled inside the listened Android. It needs a working `scrcpy` on the daemon's machine ([Install](INSTALL.md#scrcpy)); the code checks no minimum version for it and a daemon that runs inside your desktop session, which is why it runs as a user service.

`audio.listen_buffer_ms` (default 200) is the buffer: larger means fewer stutters and more delay. `audio.listen_volume` (default 15 of 15) is the media volume set while listening, because captured sound passes through a device with its own volume. Listening start and stop were verified with a real scrcpy.

## Native windows

```bash
sf window open 3
sf window grid 1-12
sf window close all
```

Each window is its own scrcpy process on the desktop the daemon runs on, so the picture and input do not pass through the panel. `grid` tiles windows to fill the screen, and overlaps them when there are too many to show side by side at a usable size. `close all` closes every window, including those of deleted Androids.

Windows need a working `scrcpy` (no minimum version is checked; older ones get older option names) and a display. The daemon has to start inside your login session, or as your user service after the desktop published its display ([Install](INSTALL.md#8-start-the-daemon)). When it has no display, `sf window` says so; the panel and every Android are unaffected. Appearance is set by `stream.scrcpy_bitrate` (`2M`), `stream.scrcpy_max_size` (720; 0 is full size), `stream.scrcpy_max_fps` (20) and `stream.scrcpy_extra_args`.

## Next

- [Install](INSTALL.md): build the camera module, the overlay and scrcpy.
- [Security model](SECURITY_MODEL.md): why the RTMP ingest stays on loopback.
- [Configuration reference](CONFIGURATION.md#mediacamera): every `media.*` setting.
- [Command reference](CLI.md#sf-camera): `sf camera`, `sf listen` and `sf window`.
