# 1. Install on the Pi

[Back to the README](../README.md) · Next: [connect Google](google.md)

Run the commands on the Pi unless a step says otherwise. This guide is for a new installation. Do not repeat it over an existing app data directory.

## Before you start

You need Immich already running, a mounted source disk, Docker with Compose, Git and a 64-bit Linux system. Check the tools:

```sh
uname -m
docker compose version
git --version
```

On a 64-bit Raspberry Pi, the first command should print `aarch64`. Install missing tools using their official instructions before continuing.

The image uses PostgreSQL 16 clients. Use it with PostgreSQL 16, or adapt the image and test it for your database version first. The scanner also expects the Immich `asset` and `asset_exif` tables and the columns used in `hub/worker.py`; it is not compatible with every Immich release automatically.

## Step 1: Download the project

```sh
git clone https://github.com/3u2t/pi-backup-hub.git
cd pi-backup-hub
cp .env.example .env
```

While the repository is private, cloning requires a GitHub account with access. Keep using this project directory for the remaining commands.

## Step 2: Find the names and folders you need

```sh
docker ps --format 'table {{.Names}}\t{{.Image}}'
lsblk -f
hostname -I
```

Write down the Immich server container, PostgreSQL container, source disk mount point and source disk UUID. The UUID identifies the filesystem; it is not the device name such as `/dev/sda1`.

Replace `YOUR_IMMICH_SERVER` with the server container name, then run:

```sh
docker inspect YOUR_IMMICH_SERVER --format '{{range .Mounts}}{{println .Destination "<-" .Source}}{{end}}'
```

Each line shows an Immich path on the left and the real host folder on the right. Find the mount containing your original uploads. Record both sides. You will use the host folder in `.env` and the Immich path in `bootstrap.json`.

Find the database network and image too:

```sh
docker inspect YOUR_POSTGRES_CONTAINER --format '{{json .NetworkSettings.Networks}}'
docker inspect YOUR_POSTGRES_CONTAINER --format '{{.Config.Image}}'
```

Use a network shared by the Immich server and database. Keep the database name and database administrator username from your existing Immich deployment handy. Do not post its environment file or credentials in an issue.

## Step 3: Fill in `.env`

```sh
nano .env
```

Use this table as a checklist. The sample values in the file are not detected settings.

| Setting | What to enter |
| --- | --- |
| `HUB_BIND_ADDRESS` | The Pi's LAN IP from `hostname -I` |
| `HUB_PUBLIC_URL` | `http://YOUR_PI_IP:8094`, using that same IP |
| `HUB_ALLOWED_HOSTS` | `YOUR_PI_IP,localhost,127.0.0.1`, without `http://` or a port |
| `HUB_DATA_DIRECTORY` | A new private folder on the Pi's system disk, for example `/srv/pi-backup-hub` |
| `HUB_SOURCE_DIRECTORY` | The existing mount point of the disk to back up |
| `IMMICH_CONFIG_DIRECTORY` | The existing folder containing your Immich Compose and environment files |
| `IMMICH_MEDIA_DIRECTORY` | The host folder containing the original uploads, found in step 2 |
| `IMMICH_NETWORK` | The shared Docker network name |
| `PGHOST` | The PostgreSQL container name or its alias on that network |
| `PGDATABASE` | The Immich database name |
| `PGUSER` | Keep `immich_backup_hub` for the account created below |

Keep `PGPORT=5432` unless the database listens on a different port **inside Docker**. A published host port is not the same thing.

If you have an external library outside these folders, set `IMMICH_EXTERNAL_DIRECTORY` to its host folder. Otherwise open `docker-compose.yml` and remove the single volume item whose target is `/immich-external`, from its `- type: bind` line through its `create_host_path: false` line. The unused environment setting can stay.

Do not create empty replacement folders to make Docker start. The mounts must point to the actual data. The app needs read access to these folders but never writes to the media mounts.

In nano, save with **Ctrl+O**, press **Enter**, then exit with **Ctrl+X**.

## Step 4: Create the private app folder

The commands below use `/srv/pi-backup-hub`. If you chose a different app data folder, replace that path in every command below. Do not put it on the source disk.

```sh
sudo install -d -m 700 /srv/pi-backup-hub
sudo install -m 600 examples/bootstrap.example.json /srv/pi-backup-hub/bootstrap.json
sudo nano /srv/pi-backup-hub/bootstrap.json
```

Fill in the source UUID, source label, host source path and installed Immich version. Then set `path_mappings`:

- `container` is the **left-hand Immich path** recorded in step 2.
- `local` is the path where this app can read that folder. Use `/immich-extra` for `IMMICH_MEDIA_DIRECTORY`.
- Keep the second mapping only if you kept the external-library mount. Its `container` must match that library's real Immich path; its `local` is `/immich-external`.
- Add a mapping for each additional Immich media mount you need. If a library is already within the source disk, its local path can be the corresponding subfolder under `/source`.

For example, the supplied first mapping means: when Immich records `/usr/src/app/upload/photo.jpg`, this app reads `/immich-extra/photo.jpg`. Replace the Immich side if your deployment uses a different path.

Now put the source marker on the actual mounted disk. Replace `/mnt/backup-source` below with its mount point:

```sh
findmnt --target /mnt/backup-source
sudo install -m 644 examples/source-marker.example.json /mnt/backup-source/.pi-backup-hub-source.json
sudo nano /mnt/backup-source/.pi-backup-hub-source.json
```

Check that `findmnt` shows the expected disk before copying the marker. Set the marker's `uuid` to exactly the same UUID as in `bootstrap.json`. The marker is a small identity file; it does not move or change your photos.

## Step 5: Give the app read access to the database

This is the only step that needs a database administrator. It creates a separate backup login. It does not give the backup app the administrator's password.

Replace the three uppercase names with your actual container, administrator and database names:

```sh
docker exec -it YOUR_POSTGRES_CONTAINER psql -X -U YOUR_DATABASE_ADMIN -d YOUR_IMMICH_DATABASE
```

At the `psql` prompt, enter these lines one at a time. Replace `YOUR_IMMICH_DATABASE` in the `GRANT CONNECT` line too:

```text
CREATE ROLE immich_backup_hub WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
\password immich_backup_hub
GRANT CONNECT ON DATABASE YOUR_IMMICH_DATABASE TO immich_backup_hub;
GRANT pg_read_all_data TO immich_backup_hub;
ALTER ROLE immich_backup_hub SET default_transaction_read_only=on;
\q
```

The password command asks you to enter a new password twice. Use a password manager to generate and save it. This role can read database data, including personal metadata; keep it private. If a role with this name already exists, stop and inspect it instead of changing an unknown account.

Save that backup account's password for the app:

```sh
sudo install -m 600 /dev/null /srv/pi-backup-hub/postgres.pgpass
sudo nano /srv/pi-backup-hub/postgres.pgpass
```

Enter one line in this format, replacing the uppercase parts:

```text
YOUR_PGHOST:5432:YOUR_IMMICH_DATABASE:immich_backup_hub:YOUR_BACKUP_PASSWORD
```

`YOUR_PGHOST` must match `PGHOST` in `.env`. Use the same port and database as `.env` too. A colon or backslash inside the password must be escaped with a backslash, as described in the [PostgreSQL password-file instructions](https://www.postgresql.org/docs/16/libpq-pgpass.html).

## Step 6: Check existing Google Photos uploads

Already uploaded your Immich library manually? **Do [this step](existing-library.md) before starting the app.** Otherwise the existing files will be queued along with new ones.

If Google Photos is empty and you want to upload everything, continue below.

## Step 7: Start and sign in

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
sudo cat /srv/pi-backup-hub/admin-password.txt
```

The configuration check should finish without an error. After the build, the container should become healthy. Open the `HUB_PUBLIC_URL` from `.env` on your computer. Sign in with username **admin** and the generated password printed by the last command.

If the container fails to start, read the error before retrying. For a missing bind folder, recheck its real host path. For an unavailable disk, recheck the source mount, both UUID values and that the app data is on a separate filesystem. For an unreachable page, check the LAN address in all three URL/host settings from step 3.

**You are done with the Pi installation when the dashboard opens and the disk is shown as available.** Continue with [connecting Google](google.md).
