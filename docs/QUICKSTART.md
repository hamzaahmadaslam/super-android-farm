# Quickstart

For someone who has finished [Install](INSTALL.md) and wants to create an Android, run an app on it and take a backup.

Each step gives the panel click path and the CLI command that does the same thing. You need the daemon running (`sf serve` or `sf service start`) and, for the panel, the API token from `sf token`. The panel is at `http://127.0.0.1:7700` unless you changed `server.port`.

Commands that start work, such as `start`, `app install` and `backup create`, follow their task to the end when you run them in a terminal. In a script, add `--wait` to get the same behaviour, or use `sf tasks` to follow it later.

## 1. Check the host

Panel: open the Dashboard. The first-run checklist reads the state of the machine and shows the exact fix for each failing host check. System in the sidebar lists every check.

CLI:

```bash
sf doctor
```

Fix every `fail` before you go on. A warning does not stop you.

## 2. Prepare the Android image

An Android needs an image: the upstream redroid image for the Android version (`redroid/redroid:13.0.0-latest` for the default, version 13) with the farm's overlays on top. The first start does this by itself. Doing it now keeps the first start short.

Panel: System, then Images, then Prepare for Android 13.

CLI:

```bash
sf image pull 13
sf image ls
```

This downloads the base image, so it takes as long as your connection needs.

## 3. Create an Android

Panel: Androids, then New Android. Only the count is required. Everything left empty follows the farm settings. Press Create. Tick "Start them now" if you want to skip step 4.

CLI:

```bash
sf create --name demo
sf ls
```

`sf create` makes the Android and leaves it stopped. `sf ls` shows its id (the first Android is 1), its state and its ADB address. The commands below use id 1.

To create several at once: `sf create -n 5 --tag eu`. Tags work in selectors as `tag:eu`.

## 4. Start it and look at it

Panel: in the Androids list, press Start on the row, then open it (the open icon or its name). The page shows the live screen next to the Android's facts and takes touch and keyboard input.

CLI:

```bash
sf start 1
sf screenshot 1 -o shot.png
```

A start on the test machine took about 11 seconds with software rendering and about 17 seconds with the Intel iGPU, once the image was ready. Starting more Androids than the capacity estimate says will fit only warns by default. With `performance.enforce_capacity` on, `sf start` refuses, explains why and tells you to add `--force`; see [Scaling](SCALING.md).

For a native window on your desktop (needs scrcpy and a desktop session for the daemon):

```bash
sf window open 1
```

`sf top` shows the load of the host and every running Android.

## 5. Install an app

You bring the app file. Super Farm keeps APK, XAPK, APKS and APKM files in an app library and installs from there, so one upload serves any number of Androids.

Panel: Apps. Press Upload an app (or drop files on the box). In the Selection box type which Androids to act on (`running`, `1` or `all`), then press Install on the app's row.

CLI:

```bash
sf apps add ~/Downloads/shop.apk
sf apps ls                               # shows the app id and package name
sf app install 1 com.example.shop        # a package name picks its newest version
sf app launch 1 com.example.shop
```

`sf app ensure` installs only where the app is missing or older, which is what you want in a script that runs more than once. [Apps](APPS.md) covers split bundles, permissions and the version matrix.

## 6. Run an example flow

A flow is a short YAML script that finds screen elements by id, text or description, not by coordinates. The project ships examples. `examples/open-app` opens an app and waits until it is in front. Try it on Android's own Settings app, first as a dry run that reads the screens and touches nothing.

Panel: Automation, then the Flows tab. Choose `examples/open-app` in the list, press Run, set the selector to `1`, enter `package=com.android.settings` under values for this run, and tick the dry run box. Run it again without the box ticked.

CLI:

```bash
sf auto flows ls
sf auto run flow examples/open-app 1 --var package=com.android.settings --dry-run
sf auto run flow examples/open-app 1 --var package=com.android.settings
sf auto runs
```

Other shipped examples are `examples/settings-open-entry`, `examples/scroll-until-visible`, `examples/type-into-focused-field` and `examples/dismiss-permission-dialog`. [Automation](AUTOMATION.md) explains recording, training and writing your own.

## 7. Back it up

Panel: Backups. Type `1` in the selector and press Back up now.

CLI:

```bash
sf backup create 1
sf backup list
sf backup verify android-1/<time>      # use the name from the list
```

By default a running Android is stopped while its data is copied and started again afterwards (`backup.stop_for_backup`). Restoring as a new Android never touches the original:

```bash
sf backup restore android-1/<time>
```

[Backup](BACKUP.md) covers farm backups, restoring over an Android, schedules and moving a farm to another machine.

## 8. Stop

```bash
sf stop 1
```

Stopping keeps the data. `sf rm 1` deletes the Android and its data after taking a safety backup first. The daemon can be stopped without stopping any Android.

## Next

- [Apps](APPS.md): bundle formats, permissions and keeping versions in step.
- [Networking](NETWORKING.md): give an Android a VPN or a proxy before it leaves the machine.
- [GPU](GPU.md): choose how Androids draw, which sets how many fit.
- [Automation](AUTOMATION.md): record, train and schedule.
- [AI assistants](AI.md): let an assistant operate the farm with your approval.
- [Command reference](CLI.md): every command.
