# Pi Backup Hub

A small Docker app for two jobs on a Raspberry Pi: scheduled disk backups to Google Drive and automatic uploads of new Immich photos and videos to Google Photos.

The two run independently. Drive backups use restic and rclone, with encryption, incremental snapshots and optional retention rules. Photo uploads keep a queue on disk and resume interrupted transfers. The web interface is in German.

## Setup

This needs a 64-bit Linux host, Docker Compose, an existing Immich installation and access to its PostgreSQL database. The image includes PostgreSQL 16 tools. Check that they match your database before using it. The scanner reads Immich's database schema directly; it needs the `asset` and `asset_exif` columns used in `hub/worker.py`.

```sh
cp .env.example .env
```

Set the paths, database host and Docker network in `.env`. The supplied values are examples. Find the actual Immich mounts in your deployment instead of guessing Docker volume paths. Remove any unused extra media mount from Compose and its path mapping from the bootstrap file.

The app binds to localhost by default. For access from another computer, set `HUB_BIND_ADDRESS` to the Pi's LAN address, update `HUB_PUBLIC_URL` and add the address or hostname to `HUB_ALLOWED_HOSTS`. Keep this dashboard on a trusted network.

Create the private app data directory with owner-only permissions. It must be on a different filesystem from the source disk. Copy `examples/bootstrap.example.json` there as `bootstrap.json` and edit it:

- `source_uuid` is the UUID of the source filesystem, available from `lsblk -f`.
- `host_source` is the disk's path on the host.
- `path_mappings` map paths stored by Immich to paths visible inside this container. For example, `/usr/src/app/upload` maps to `/immich-extra` when the upload directory is mounted there. If uploads live within the main disk mount, use the matching path under `/source` instead.
- `immich_version` records the installed version for a future restore.

Copy `examples/source-marker.example.json` to the root of the mounted source disk as `.pi-backup-hub-source.json`. Give it the same UUID as `bootstrap.json`. Both files must describe the real mounted disk. The app refuses to use a missing marker, a mismatched marker or a source on the app's own filesystem.

Use a dedicated PostgreSQL login with read access, `default_transaction_read_only=on`, and no table write or schema creation rights. It needs to read the asset tables and create a dump of the Immich database. Store its connection password in `postgres.pgpass` inside the private app data directory, with mode `0600`. Use the standard PostgreSQL password-file format, matching the `PGHOST`, `PGPORT`, `PGDATABASE` and `PGUSER` values in `.env`.

```sh
docker compose config --quiet
docker compose up -d --build
```

On first start, the app writes `admin-password.txt` to its private data directory. Sign in as `admin` with that password. The dashboard has a password-change form under recovery.

## Google accounts

In your own Google Cloud project, enable the Drive API and Photos Library API, configure the OAuth consent screen and create a **Desktop app** OAuth client. Import its JSON file under Google accounts in the dashboard.

Download and run each sign-in helper on the computer where you use the browser. Python 3 is enough; the helper uses the standard library. It opens Google's sign-in page, receives the response on localhost and sends the result back to your dashboard. Delete the downloaded helper afterwards: it contains a temporary pairing configuration and the client credentials.

Drive and Photos can use the same account or different accounts. Drive uses `drive.file`, so access is limited to files created by this app. Photos uses `photoslibrary.appendonly` and `photoslibrary.readonly.appcreateddata`, plus account identification scopes.

Google's external OAuth testing mode gives these refresh tokens a seven-day lifetime. Set up the project's publishing status according to [Google's OAuth requirements](https://developers.google.com/identity/protocols/oauth2#expiration) before relying on unattended operation.

Download the recovery key, keep a copy outside the Pi, and confirm that it is saved. Prepare the Drive repository in the dashboard, run the first backup and choose a schedule. Photo uploads have a separate switch and polling interval.

## Existing photos

Without a baseline, the scanner queues the existing Immich library as well as new files. If those files are already in Google Photos, prepare `baseline-assets.json` in the private data directory **before starting the app**. It is a JSON array with one object per asset: `id`, `checksum` as hexadecimal text, `type` as `IMAGE` or `VIDEO`, `name`, `path`, and optionally `created` and `updated`. Obtain these from the actual Immich database. The baseline is imported once.

Only mark files as baseline if you have checked that they were uploaded. The [Photos Library API can list only app-created media](https://developers.google.com/photos/support/updates); this app cannot verify a manual import or deduplicate against the entire Google Photos library.

An unclear import response puts the file on hold for reconciliation rather than creating it again. Deletes in Immich are not forwarded to Google Photos. The app uploads the original file bytes, including embedded metadata. Immich albums, people and edits stored only in its database are not recreated in Google Photos.

## What a disk backup contains

Each run creates a custom-format PostgreSQL dump, then backs up the source disk, configured extra media mounts, Immich deployment configuration and the app's recovery state. Media and configuration mounts are read-only, and the container has no Docker socket.

This is a file backup, not a bootable image of the Pi. Files can still change while a backup is running. A run with unreadable files is reported as incomplete.

Retention is off by default. If enabled, pruning runs after a successful backup. Do not edit or delete individual objects inside the Drive repository.

The repository includes the backup app's credentials inside the encrypted snapshot. Save the recovery key separately. Local vault encryption does not protect against someone who can read both the vault and its key in the app data directory.

The dashboard shows storage in decimal units and upload rates in megabytes per second. Resource limits depend on host kernel support; the Go memory limit is a soft limit.

## Restore

Keep the recovery key and access to the Google account. On another computer, install restic and rclone and configure a remote named `drive`. Use the same OAuth client when restoring with the `drive.file` scope; a different client may not see the repository.

```sh
export RESTIC_REPOSITORY=rclone:drive:Pi-Backup-Hub
export RESTIC_PASSWORD_FILE=/path/to/restic-password-only.txt
restic snapshots
restic restore latest --target /path/to/new-restore-directory
```

Put only the restic password from the recovery-key document in the password file and restrict access to it. Change the repository path if you chose a different Drive folder.

In the restored files, `/source` is the source disk and `/data/staging/immich/database.dump` is the database archive. `restore-mapping.json` records the configured paths. Restore into a fresh directory and follow the [Immich restore documentation](https://docs.immich.app/administration/backup-and-restore/) for a compatible installation before importing the dump.

The dashboard's repository check checks structure and references. Its restore test retrieves the database dump and checks the archive listing. Neither is a complete restore of Immich or a read-through of every stored data block.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
node --check static/app.js
```

The unit tests use temporary directories and mocked Google responses. `tests/pi_integration.py` is a separate test for a configured Linux container: it checks the read-only mounts and database role, creates a local encrypted repository, takes a real database dump and restores a test file. It does not upload to Google. It writes only inside a new temporary directory under `/data` and removes that directory when done.

Keep `.env`, OAuth downloads, app state, database dumps and recovery keys out of version control. `.gitignore` and `.dockerignore` cover these files; the repository contains only source and setup examples.
