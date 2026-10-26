# Backup contents and maintenance

[Back to the README](../README.md)

## Disk backups

Each run first creates a PostgreSQL custom-format dump of Immich. It then backs up the source disk, the configured extra media mounts, Immich's deployment configuration and the app's recovery state.

The source disk and media/configuration mounts are read-only. The app has no Docker socket. The private app data folder is writable and must be separate from the source filesystem. A source marker and configured UUID must match before a backup or media scan starts.

This is not a filesystem snapshot. Files can change or disappear while the run is reading them. A run with unreadable files is reported as incomplete. Mounted filesystems nested beneath the main source are not followed by the main restic scan; configure additional media mounts explicitly when needed.

Identical blocks are deduplicated, including data in a second export folder. Keeping extra exports still means those files must be inspected and, on the first run, read.

## Retention

No old snapshot is removed by default. Enable retention only when you have decided how much history to keep and tested a restore. Cleanup runs after a successful backup.

Do not edit or delete individual objects inside the encrypted Drive repository.

## Credentials and keys

The encrypted backup includes the backup app's settings and credentials so it can be recovered. Keep the separately downloaded recovery key outside the Pi.

The local vault is encrypted, but its key lives in the same private app data directory. Someone who can read both can decrypt it. Restrict access to the directory and do not expose the dashboard publicly.

`.gitignore` and `.dockerignore` exclude local settings, OAuth downloads, databases, dumps and keys. Still inspect staged files before pushing changes; ignore rules do not remove files already committed.

## Photo uploads

Uploads keep original file bytes and embedded metadata. Immich albums, people and database-only edits are not recreated in Google Photos. Deleting an Immich asset does not delete its Google Photos copy.

Interrupted transfers can resume. An unclear import response puts the item on hold for reconciliation rather than creating it again. **Google-Status abgleichen** looks among media created by this app. It cannot inspect the full manually uploaded library.

## Limits and checks

Storage values use decimal units and upload fields use megabytes per second. The existing internal upload setting is converted for display without changing an unchanged saved limit.

The overview lists Google's published API limits, checked on October 26, 2026. Photos Library API allows 10,000 requests per project per day. Drive's current defaults are 1,000,000 quota units per minute per project and 325,000 per minute per user. Older projects may still use request-based quotas. See [Photos limits](https://developers.google.com/photos/overview/api-limits-quotas) and [Drive limits](https://developers.google.com/workspace/drive/api/guides/limits).

These are reference values, not a live quota meter. The dashboard links to the connected project's quota pages for actual usage and any custom limits. It does not request additional Google Cloud permissions. API requests, upload bytes and account storage are separate limits; one uploaded photo can require several requests. Google's documented 750 GB daily Drive upload/copy restriction specifically names Workspace users.

Docker resource limits depend on the host kernel. `GOMEMLIMIT=384MiB` is a soft per-process Go runtime limit, not a hard container memory cap.

The dashboard's repository check checks structure and references, not every stored block. The restore test retrieves only the database dump and checks its archive listing. See [the restore guide](restore.md) for a separate real-file restore.

The live upload rate reads the container's network transmit counters. It includes both disk and photo uploads and other outbound app traffic. Processed source bytes include reused and deduplicated data, so they are shown separately.

Pausing asks the backup process to stop cleanly and leaves the job paused until it is resumed or cancelled. Resume starts another pass with the same job and repository. Restic reuses indexed blocks from previous passes; see its [interrupted-backup explanation](https://restic.readthedocs.io/en/stable/faq.html#will-restic-resume-an-interrupted-backup). This may require reading files again and retransferring a small amount of recent work. Keep the repository and local app state intact.

## Updating

Keep your private data directory, `.env` and any locally adapted mounts. Review release changes and database compatibility first. Wait for active backups and photo transfers to finish before restarting the app.

From the project directory:

```sh
git pull --ff-only
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail 100
```

Review logs privately. They may contain file paths or other personal information even though the app redacts known credentials.
