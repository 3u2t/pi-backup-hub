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

Connect the optional live quota display to fetch actual limits from Service Usage and usage from Cloud Monitoring. It uses a separate OAuth grant with `monitoring.read` and `cloud-platform.read-only`. The second scope can read other Cloud project data as well; Google does not offer a quota-only OAuth scope for this endpoint. The app only reads Drive and Photos quota configuration and metrics. Disconnecting this grant leaves both upload connections and backup settings intact.

Enable the Cloud Monitoring API and Service Usage API in the same project, then use **Live-Anzeige verbinden** in the dashboard. The account needs `monitoring.timeSeries.list` and `serviceusage.quotas.get` on the project. No billing account is enabled or linked by the app.

Usage is fetched at most once per minute while the dashboard is in use. Google's own metrics can lag several minutes. Minute quotas show the latest reported minute; daily quotas sum the current Pacific Time quota day. Per-user usage is left unknown when Google only supplies a project total. Missing samples are not treated as zero. The app stops quota queries after 750,000 returned monitoring series in a UTC calendar month. Google's monitoring free allowance is shared with other queries; see [Monitoring pricing](https://cloud.google.com/products/observability/pricing).

Cloud Monitoring can reject usage queries on projects without billing. The app detects this response and uses a local counter instead. It keeps reading the project's actual limits through Service Usage, at most once per hour. It does not enable billing or retry the blocked monitoring query automatically.

The local counter starts when installed; it cannot reconstruct earlier usage or count other apps. Photo request attempts are counted at dispatch, including retries. Drive uses rclone's HTTP counters from a metrics-only endpoint bound to the container's loopback interface. No rclone control server, HTTP header dump or request-body logging is enabled. Counters are sampled every two seconds, so short helper processes and activity just before a process exits can be missed. The counter persists across app restarts. It reports requests, not Google's weighted Drive quota units, and labels those units as unavailable.

API requests, upload bytes and account storage are separate limits; one uploaded photo can require several requests. Google's documented 750 GB daily Drive upload/copy restriction specifically names Workspace users.

Docker resource limits depend on the host kernel. `GOMEMLIMIT=384MiB` is a soft per-process Go runtime limit, not a hard container memory cap.

The container's temporary filesystem allows up to 512 MiB. This is a capacity limit, not a reservation. Restic creates several encrypted packs there before uploading them. The earlier 128 MiB limit could fill up during a large backup. A backup now checks for at least 256 MiB of free temporary space before starting. See [restic's temporary-space requirements](https://restic.readthedocs.io/en/stable/047_tuning_parameters.html#pack-size).

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
