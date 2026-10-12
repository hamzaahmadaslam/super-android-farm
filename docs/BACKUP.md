# Backup

For anyone who needs to keep, verify, restore or move Android data and the farm's settings.

There are two kinds of backup. An Android backup holds one Android's `/data` and its settings. A farm backup holds the farm's settings and no Android data. Both are folders under the backup directory, `backup.dir` (default `<home>/backups`). Put it on a disk with room: an Android's data grows to several GB once apps are installed.

```text
<backup dir>/android-<id>/<UTC time>/
    data.tar.zst     the /data volume (data.tar.gz or data.tar with other backup.compression)
    instance.yaml    the Android's settings, including its device identity
    manifest.json    Android version, image, size, and the sha256 of every file
<backup dir>/farm/<UTC time>/
    farm.tar.gz      config.yaml, every Android's settings and state, automation data
    manifest.json    the archive's checksum, sealed with this farm's own key
```

A backup is named by its folder, and `android-3/20250101-040000` is enough for one in the backup directory. The manifest is written last, so a folder without one is an interrupted backup and is ignored.

## Back up Androids

```bash
sf backup create 1-5
sf backup list
sf backup list --kind farm
sf backup verify android-3/20250101-040000
sf backup verify --deep android-3/20250101-040000
```

In the panel: Backups, type a selector, Back up now.

By default a running Android is stopped while its data is copied and started again afterwards (`backup.stop_for_backup`, on). That gives a consistent copy. With it off the Android keeps running and the backup is marked "taken while running" in the list. Backups of Androids that were never started fail with a message to start them once, because there is no data yet.

The copy runs in a helper container, because `/data` is owned by Android's users and is unreadable for the farm's own user. The archive is streamed to a file the farm user owns and never passes through memory. It keeps owners, modes, extended attributes and ACLs (GNU `tar --xattrs --acls --numeric-owner --sparse`), because Android keys file access on numeric owners and extended attributes, and a `/data` restored without them boots but apps lose their data or crash. On the test machine the owners, modes and 362 extended attributes were identical after a restore.

Compression is `backup.compression` (`zstd`, `gzip` or `none`) at `backup.level` (default 3).

`verify` checks every file against the checksums in the manifest. `--deep` also reads the whole archive to check that it can be unpacked. Run `verify` on a backup you plan to depend on. Panel: Verify on the row.

The backup folder is yours to move, copy and delete. The panel also offers Download for a backup that holds no secrets.

## Back up the farm's settings

```bash
sf backup farm
sf backup farm --include-secrets
```

Panel: Backups, Back up settings.

A farm backup holds `config.yaml`, every Android's `instance.yaml` and `state.json`, the automation data (datasets, programs, flows, profiles, schedules, values) and the names of the VPN profiles. It never holds Android data.

By default it leaves out the VPN profiles, VPN credentials and tokens, so the archive can be stored or shared without leaking them. `--include-secrets` (or `backup.include_secrets`) adds them. The archive is owner-only either way, and the list marks one that holds secrets; keep it private. Values whose names look like secrets are never included, even then, and the farm's sealing key is never included.

## Restore

A backup is never restored into a running Android, and never replaces existing data without `--over`.

```bash
sf backup restore android-3/20250101-040000              # as a new Android
sf backup restore android-3/20250101-040000 --over 3     # replace Android 3's data
```

Restoring as new creates a new Android with the settings the backup saved (as far as this farm allows) and the saved device identity. The original is not touched. If the original still exists here, the copy is created with networking off (`network.mode` `none`), because two Androids with one identity must not be online together. Change its network once only one of them is in use, or pass `--keep-network` to skip this. A saved image that is not on this machine is not taken over: a backup's manifest must not be able to make the farm fetch and run a privileged image it names.

Restoring over an Android needs it to be stopped and needs `--over ID`; the CLI asks first, and `-y` answers for you. Before the old data is replaced the farm backs it up (reason `before-restore`, below). The restore checks the backup against its checksums first and refuses a damaged one. If a restore is interrupted, the data is incomplete and the message says to run it again.

In the panel: Restore… on the backup's row. Choose "As a new Android" or "Over Android N". Over needs Force.

The restored data carries its device identity, since apps in `/data` know the device they were set up on. What the farm remembered about the old data (renderer, trim, proxy) is cleared and re-read at the next start.

### Restore a farm backup

```bash
sf backup restore farm/20250101-050000
```

A farm restore merges settings into this farm. It never overwrites what exists: an Android whose id is taken, a file already there and `config.yaml` are skipped and listed. No Android data is touched. Reload or restart the daemon after a restore that brought `config.yaml` back.

What it takes depends on who made the archive. A backup this farm made carries the farm's seal (an HMAC of the archive's checksum with the key in `<home>/secrets/farm-key`) and is restored in full. An archive from anywhere else is somebody else's word about what should run on this machine, so a restore takes only what cannot do harm:

- Android definitions lose what decides what runs and when: an image that is not on this machine, an adopted data volume, extra boot arguments and properties, and autostart. Their saved runtime state and the schedules are left out.
- `config.yaml` is taken only with `--restore-config`.
- VPN profiles, credentials and tokens are taken only with `--restore-secrets`.
- `--trust` keeps the Androids exactly as saved (custom image, data volume, boot arguments, autostart) and the schedules. Use it only for a backup you made yourself, because it decides what runs on this machine with full privileges. `--restore-config` is never implied by `--trust`, and neither flag is implied by `-y`.

The result lists what was restored, skipped and adjusted, each with the reason, and which VPN profiles the Androids need that this farm does not have.

## Safety backups

With `backup.auto_before_risky` on (default), the farm takes a backup before an operation that can lose an Android's data, and the backup's reason says which:

| Reason | When |
| --- | --- |
| `before-delete` | Deleting an Android and its data. |
| `before-restore` | Restoring over an existing Android. |
| `before-version-change` | Booting an Android's data on another Android version. |

If that backup cannot be made, the operation is refused and nothing is changed. Repeat it with force (`sf rm --force`, or the Force box in the panel) to go on without the backup, or turn `backup.auto_before_risky` off. `sf rm --keep-data` keeps the volume and skips the data backup, since nothing is deleted.

## Schedules and retention

```bash
sf config set backup.schedule '0 4 * * *'        # every day at 04:00, host time
sf config set backup.schedule_selector 'all'
sf config set backup.keep 7
sf backup prune
```

`backup.schedule` is a cron line `minute hour day-of-month month day-of-week`. Empty is off. The daemon starts the scheduled backup when it is due (it checks about every half minute), runs retention at the end of each scheduled run, and keeps the next due time on disk, so a restart neither repeats nor forgets a run. A run missed while the daemon was down is made up once. A run that comes due while the previous one is still going is skipped and counted. In the panel the Schedule card on the Backups page edits these settings and shows the next and last run.

`backup.keep` (default 7) is how many backups `sf backup prune` and scheduled retention keep for each Android and for the farm backups. 0 keeps everything. An Android is told apart by its id and its Android id, so a new Android that reuses a deleted one's number does not push out the last copies of the old one.

## Move a farm to another machine

This is assembled from the pieces above. It follows from the code and has not been run as a whole.

1. On the old machine, back up every Android and the farm: `sf backup create all`, `sf backup farm`. Add `--include-secrets` only if you will carry the VPN profiles and logins in this archive and keep it private.
2. Verify them: `sf backup verify --deep <name>`.
3. Install Super Farm on the new machine ([Install](INSTALL.md)) and copy the old backup folders into its backup directory (`<home>/backups`, or whatever `backup.dir` is). `sf backup list` should show them.
4. Restore the farm settings from your own archive: `sf backup restore farm/<time> --trust`, adding `--restore-config` and `--restore-secrets` if you want those too. Without `--trust` the Android definitions come back without their custom images, boot arguments and autostart.
5. Restore each Android's data over its definition: `sf backup restore android-<id>/<time> --over <id>` (the Android must be stopped; this needs the confirmation). Or restore each as a new Android, which gets a new id.
6. Start them with `sf start`. Check the GPU mode ([GPU](GPU.md)): the new machine may have different cards, and a mode that the new machine cannot serve fails with a message that lists what it can.

Data written by a newer Android release does not boot on an older one, so keep the Android version of the new Androids at the version that wrote the data. The version guard refuses a change over existing data unless forced.

## Adopt an existing data volume

An Android's `/data` is a Docker volume named `<prefix>-data-<id>` (`sf-data-1`, for example). You can give one Android an existing volume instead of a fresh one, for data created elsewhere or kept at an earlier `sf rm --keep-data`:

```bash
docker volume ls                                 # find the volume
sf create --name old-shop --android 13 --data-volume sf-data-7
```

The volume must exist and must not already belong to another Android, and you can adopt one volume per Android (the count must be 1). The Android keeps the identity already in the data: no new device model and no new `android_id` are generated unless you ask for a device profile. Set the Android version to the one that wrote the data.

The farm never deletes an adopted volume, or any volume that does not carry its label, when it deletes the Android; it only detaches. `sf rm` reports a warning that the volume was left in place. A volume kept by `--keep-data` is skipped when new Androids pick their default volume name, so a new Android never silently comes up with an old Android's data.

If an adopted volume is later removed, starting the Android fails with a message to restore it or clear `data_volume`.

## Next

- [Install](INSTALL.md#uninstall): what stays when you uninstall.
- [Apps](APPS.md#app-data): app data backup is a separate, advanced tool.
- [Scaling](SCALING.md): disk use per Android.
- [Command reference](CLI.md#sf-backup): `sf backup`.
- [Security model](SECURITY_MODEL.md): what a backup with secrets exposes.
