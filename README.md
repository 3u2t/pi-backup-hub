# Pi Backup Hub

Back up a Raspberry Pi's disk to Google Drive and send new Immich photos and videos to Google Photos. Both jobs have their own settings in a small web dashboard.

Disk backups are encrypted and incremental. Photo uploads keep a queue and resume interrupted transfers. The dashboard is in German; these instructions are in English.

During a disk backup, the dashboard shows its percentage, the app's current upload rate and an estimated finish time. You can pause the disk backup and continue later.

## Start here

Setup has three parts. Do them in order:

1. **[Install on the Pi](docs/install.md)** — find your folders, configure Docker and open the dashboard.
2. **[Connect Google](docs/google.md)** — create your own Google client and sign in to Drive and Photos.
3. **[Run the first backup](docs/first-backup.md)** — save the recovery key, test a backup and choose a schedule.

Already have the app running? Skip part 1. These are instructions for a new installation; they do not change an existing one.

You need a Pi running 64-bit Linux, Docker Compose, Immich and a Google account with enough free storage. You also need Python 3 on the computer used for the Google sign-in. This is still a manual setup, especially on Google's side. There is no one-click installer yet.

## What it does

| Disk → Google Drive | Immich → Google Photos |
| --- | --- |
| Runs on a schedule you choose | Looks for new files at a regular interval |
| Creates encrypted restic snapshots | Uploads original photos and videos |
| Includes an Immich database dump | Keeps track of completed uploads |
| Keeps old snapshots unless you enable cleanup | Does not forward deletions from Immich |

The disk backup is a file backup, not a bootable image of the Pi. The photo upload does not recreate Immich people, albums or edits stored only in its database.

The current image includes PostgreSQL 16 tools. The Immich scanner reads the database directly, so check the compatibility notes in the install guide before using a different version.

## Existing photos

If you already uploaded your library to Google Photos, read [the existing-library instructions](docs/existing-library.md) **before the first start**. Otherwise the app queues that library too. It cannot check your entire Google Photos library for duplicates.

## More information

- [Restore files and the Immich database](docs/restore.md)
- [Backup contents, limits and maintenance](docs/operations.md)
- [Report a problem or contribute](CONTRIBUTING.md)

## License

You may use, modify and share this app for free, including inside a business. You may not sell the app or modified versions. Keep the license and copyright notices with copies you share.

See [LICENSE](LICENSE) for the full terms. This is a project-specific source-available license, not an [OSI-approved open-source license](https://opensource.org/osd). Third-party dependencies keep their own licenses.
