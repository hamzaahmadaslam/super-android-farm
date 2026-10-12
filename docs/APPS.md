# Apps

For anyone who puts apps on Androids and keeps their versions in step across a farm.

You upload an app file once to the app library, then install, update or remove it on any selection of Androids. Nothing in Super Farm knows a particular app. Every operation takes a package name or a library id and a selector, runs on each Android on its own, and reports one result per Android, so a device that is off, full or incompatible is one failed line and never stops the rollout.

You bring the app files. Super Farm does not download or distribute apps.

## The app library

The library holds the files the farm can install, unpacked and described. It lives in `<home>/apps/`.

| Format | What it is |
| --- | --- |
| `.apk` | One file, everything inside. |
| `.xapk` | A zip with `base.apk`, config splits, optional expansion (OBB) files and a `manifest.json`. |
| `.apks` | A zip as `bundletool` builds it (`splits/base-master.apk`, ABI and density splits, `standalones/`). |
| `.apkm` | A zip with `base.apk`, `split_config.*.apk` and an `info.json`. |

Each upload is unpacked once. Every APK's own manifest is read, and the bundle's JSON files are used only as hints. An app is stored under the id `<package>-<versionCode>`, for example `com.example.shop-42`, so several versions of the same package can sit in the library side by side. Adding a package and version that already exist replaces the stored copy.

```bash
sf apps add ~/Downloads/shop-4.2.apk ~/Downloads/video.xapk
sf apps ls
sf apps rm com.example.shop-42
```

In the panel: Apps, Upload an app (or drop files on the box), and Remove file on a row. Removing a file from the library does not touch Androids that already have the app.

Uploads are treated as untrusted. File names are reduced to plain names inside the app's own folder, links in archives are refused, and the member count (at most 4096), the size of each file and the total unpacked size are capped while unpacking, so a path trick or a zip bomb gets nowhere. An upload over `server.max_upload_mb` (4096) is cut off, and an app whose unpacked size is over `control.max_app_mb` (8192) is refused. A file whose name says `.xapk` but is really one APK is stored as one APK, and a bundle uploaded as `.apk` is stored as a bundle. Encrypted `.apkm` files from some sources are not zip archives and must be converted first.

The split bundle path was verified live: an `.xapk` with 10 APKs and an OBB file installed only the splits that match the device. APK metadata is parsed without external tools such as `aapt`.

## Install on a selection

A selector picks Androids: `all`, `running`, `stopped`, `tag:NAME`, an id or a range, joined by commas, with `!` to exclude.

```bash
sf app install 1-10 com.example.shop      # a package name picks its newest version
sf app install tag:eu com.example.shop-42 # an exact library id
sf app ensure all com.example.shop
sf app matrix running
```

In the panel: Apps, type the selector under Selection, then use the buttons on the app's row (Install, Ensure, Launch, Stop, Uninstall). An Android's own page has an Apps tab with the same actions for that Android alone.

### Install, ensure and update

| Command | Behaviour |
| --- | --- |
| `sf app install` | Installs the chosen version on every match. It replaces an installed copy (`replace` is on). Add `--downgrade` to replace a newer installed version. |
| `sf app ensure` | Idempotent: installs only where the app is missing or older. Where the same version is there it reports `already installed`. Where a newer one is there it reports `kept newer version`. Safe to repeat after a partial failure. |
| update | There is no separate command. Add the new version to the library and run `ensure`: Androids with an older version are updated, others are left alone. |

Installs follow `control.install_timeout` (900 seconds per Android) and run `control.concurrency` (8) Androids at a time. An Android that is not booted fails that line only.

When Android refuses an install, the result line gives the reason in words. The ones it explains: a newer version is installed (use `--downgrade` or uninstall first); a different signature is installed (uninstall first, and the data is lost); no matching CPU code (see below); storage full; a missing split (add the complete bundle); the app needs a newer Android version; the APK is test-only; the APK is unsigned or was changed after signing.

### How splits are chosen

For a split app, a device must get the base and exactly the pieces that fit it, in one transaction (`adb install-multiple`). Installing the base alone "succeeds" and then crashes at first start for lack of its native-code split. The farm reads what the device reports about itself (CPU list, screen density, languages, Android API level and whether it has a native bridge) and picks, like an app store does:

- the base and every feature module;
- per module, one ABI split: the first of the device's ABIs that the app has;
- per module, one density split: the smallest that is at least the screen density;
- the splits for the device's languages (`all_locales` in the API takes every language);
- any split of unknown purpose, because Android ignores what it does not need.

OBB expansion files are pushed after the install. The install is refused with an explanation when the app needs a newer API level than the Android runs.

### ARM-only apps and the native bridge

The Android images run on x86-64. An app that has native code only for ARM needs a translation layer in the image. `android.images.<version>.arm_translation` (default on) says the image is expected to have one, and the farm checks the device's `ro.dalvik.vm.native.bridge` property when it plans an install. If the app has no code for the device's CPU and the device has no bridge, the install is refused and the message says to use an image with ARM translation or an x86 build. If the app is ARM-only and the image reports no native bridge, the install goes ahead with a warning that the native code cannot run.

The doctor warns when the host CPU lacks SSE4.2 or SSSE3, because ARM translation will not run there. The live video benchmark in [Scaling](SCALING.md) ran an ARM app translated to x86.

## Permissions

By default an install grants the app's runtime permissions (`--no-grant` skips that). Automation stalls on the first permission dialog nobody taps, so the farm grants up front. Special permissions, the switches in Settings, are set as app operations. Android only grants what the app's manifest asks for, so asking for a permission the app does not request is an error.

Granting and revoking later, enabling and disabling a package, and listing what an app requests are API routes, not CLI commands: `POST /api/apps/grant`, `/api/apps/revoke`, `/api/apps/enable`, `/api/apps/disable` and `GET /api/instances/{id}/apps/{package}/permissions`. See [API reference](API.md#apiapps).

## Run, stop, clear and remove

```bash
sf app launch all com.example.shop     # opens the app's launcher activity
sf app stop all com.example.shop       # force-stop
sf app clear 3 com.example.shop        # erase its data, as after a fresh install
sf app uninstall tag:test com.example.shop --keep-data
```

`launch` starts the app's launcher activity. The API's launch route also takes an explicit `activity`. `clear` and `uninstall` change an Android and are the ones an AI assistant must get your approval for ([AI assistants](AI.md)).

## Version matrix

```bash
sf app matrix            # all Androids
sf app matrix running
```

The matrix lists third-party apps against Androids, with the version installed on each. In the panel it is the Version matrix at the bottom of the Apps page. A cell is highlighted when the Android is behind the newest version in the library. Androids that could not be read are listed with the reason.

## App data

Copying one app's data out of an Android and into another exists in the code as an advanced tool (`Apps.backup_data` and `Apps.restore_data` in `superfarm/control/apps.py`). It archives `/data/data/<package>` with owners preserved, as a checksummed `0600` file under `<backups>/apps/<package>/`, and was verified live. No CLI command, API route or panel page calls it in this version. It does not capture device-encrypted storage, shared storage or account tokens kept by the system, and a restored login can still be rejected by a service that ties sessions to a device. To move an Android, use a whole-device backup ([Backup](BACKUP.md)).

## Next

- [Quickstart](QUICKSTART.md): install a first app and run a flow on it.
- [Automation](AUTOMATION.md): drive the apps once they are installed.
- [Backup](BACKUP.md): back up whole Androids, apps and data included.
- [Command reference](CLI.md#sf-apps): `sf apps` and `sf app`.
