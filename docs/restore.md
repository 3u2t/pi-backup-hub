# Restore files

[Back to the README](../README.md)

Use a separate computer or a new empty directory for a restore. Keep the running Immich installation out of the way until you have checked what you retrieved.

## What you need

- The recovery-key document saved when you set up the app.
- Access to the Google account holding the Drive backup.
- [restic](https://restic.net/) and [rclone](https://rclone.org/) installed.

## Step 1: Connect rclone to Drive

Run `rclone config` and create a Google Drive remote named **drive**. Use the same Google account and your own OAuth client. With the app's `drive.file` scope, a different client may not see files created by the original one. Follow [rclone's Drive setup](https://rclone.org/drive/).

## Step 2: Prepare the restic password

Create a private text file containing **only the value after `Restic-Passwort:`** in your recovery-key document. Do not use the whole recovery document as the password file. Restrict this new file to your user, for example with `chmod 600` on Linux.

## Step 3: List the backups

In a Linux or macOS terminal, set these two paths, then list the snapshots:

```sh
export RESTIC_REPOSITORY=rclone:drive:Pi-Backup-Hub
export RESTIC_PASSWORD_FILE=/path/to/restic-password-only.txt
restic snapshots
```

If you changed the Drive folder in the dashboard, use that folder instead of `Pi-Backup-Hub`.

## Step 4: Restore into a new folder

For a small first check, retrieve one known file by its path in the snapshot:

```sh
restic restore latest --target /path/to/new-file-check --include /source/path/to/a-known-photo.jpg
```

Open that restored file and compare it with the original. To restore a whole snapshot:

```sh
restic restore latest --target /path/to/new-full-restore
```

Use `restic ls latest` if you need to find a file's exact snapshot path. Choose a particular snapshot ID instead of `latest` when appropriate. See the [restic restore guide](https://restic.readthedocs.io/en/stable/050_restore.html) for other options.

## Step 5: Restore Immich separately

Inside the restored tree:

| Path | Contents |
| --- | --- |
| `source/` | The source disk |
| `immich-extra/` and `immich-external/`, if configured | Additional media folders |
| `immich-config/` | Immich deployment configuration |
| `data/staging/immich/database.dump` | Custom-format PostgreSQL dump |
| `data/staging/immich/restore-mapping.json` | Configured paths and recorded version |

Prepare a fresh, compatible Immich deployment and follow [Immich's backup and restore instructions](https://docs.immich.app/administration/backup-and-restore/). This app's dump was made with `pg_dump --format=custom`; it needs `pg_restore`, not a plain SQL import command. Do not assume that a command for a compressed SQL backup accepts this archive format.

An archive listing check proves the dump can be read; a full recovery also needs an actual database restore and working media files. Never test an import by overwriting the live database.
