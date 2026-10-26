# 3. Run the first backup

[Back to Google setup](google.md) · [Back to the README](../README.md)

Both Google account cards should show the intended email addresses. If they do not, finish [connecting Google](google.md) first.

## Step 1: Save the recovery key

1. Open **Wiederherstellung**.
2. Click **Schlüssel herunterladen**.
3. Save a copy somewhere separate from the Pi, such as a password manager or a disconnected drive.
4. Click **Ich habe den Schlüssel separat gespeichert**.

You need this key to open the encrypted Drive backup on another computer. Do not put it in GitHub or keep your only copy on the disk being backed up.

## Step 2: Prepare the Drive folder

1. Open **HDD-Sicherung**.
2. Leave **Ordner in Google Drive** as `Pi-Backup-Hub`, or choose an empty destination you want to keep using.
3. Click **Drive-Verbindung testen** and check the result under **Protokoll**.
4. Click **Sicherungsspeicher vorbereiten**. Wait until the status says **Bereit**.

## Step 3: Start one backup manually

Click **Jetzt sichern**. Watch **Übersicht** for progress and **Protokoll** for the result.

The first run can take hours or days, depending on the amount of data and upload speed. A schedule time is a **start time**, not a deadline or a stop time. Later runs reuse unchanged data, but still need to inspect the source files.

Wait for a successful result. **Noch keine** under the last backup means no successful complete disk backup has been recorded yet. An incomplete or failed run needs attention even if some data reached Drive.

The progress card appears near the top of the overview. Its percentage measures processed source data. **Upload der App** measures this container's network upload, including photo uploads and a small amount of other app traffic. The rate uses a rolling measurement over roughly ten seconds. It is different from the speed at which source files are checked or reused.

Until the file scan finishes, the total and percentage can change. A remaining-time estimate appears after the scan and at least thirty seconds of processing. Treat the finish time as an estimate; compression, reused blocks and connection speed can change it.

### Taking a break

Click **Pausieren** to stop the disk backup safely. Wait for **Pausiert** before assuming that its upload has stopped; the app first lets restic finish saving its current work. Click **Fortsetzen** when you want it to continue.

Continuing checks the source files again and reuses already indexed backup data. Some recent work may need to be transferred again. The percentage and estimate are recalculated. The paused job stays paused across an app restart and reserves the Drive repository, so the scheduler does not start a second job over it.

This pauses the HDD backup. The separate Immich photo automation keeps its own settings and can still upload new photos. Its traffic is included in the app upload-rate display.

## Step 4: Check that you can get data back

Under **Wiederherstellung**, run **Backup prüfen**, then **Testweise zurückholen**. Run them one at a time and check the result after each.

The first checks repository structure. The second retrieves the database dump into a new test folder and checks its archive listing. It does not replace the live database. These checks are useful, but they do not prove that every photo or a whole Immich installation can be restored. Follow [the restore guide](restore.md) to retrieve real files into a separate folder too.

## Step 5: Choose a disk schedule

Open **HDD-Sicherung** and choose:

- **Häufigkeit:** `Täglich` for once a day, or selected weekdays if that is enough.
- **Uhrzeit:** a time when the Pi is on and you usually need less bandwidth, for example `03:00`.
- **Zeitzone:** your local timezone.
- **Maximaler Upload:** a limit in megabytes per second; `0` means unlimited.

Enable **Geplante HDD-Sicherung aktivieren** and click **Zeitplan speichern**. The summary on **Übersicht** should show the schedule you selected.

Leave cleanup of old snapshots off until you have tested recovery and chosen how much history you want to retain.

## Step 6: Enable photo uploads

Open **Immich → Fotos**. Before enabling it, check [the existing-library step](existing-library.md) if your photos are already in Google Photos.

1. Enable **Neue Immich-Dateien automatisch zu Google Fotos hochladen**.
2. Choose how often to look for files, such as **Alle 5 Minuten** or **Alle 10 Minuten**.
3. Set a separate upload limit if needed and choose whether to include videos.
4. Click **Foto-Automatik speichern**.

The photo upload does not wait for the disk schedule. Add one new photo to Immich, wait for it to upload and check that it appears in the intended Google Photos account.

## Final check

You should now have a separately saved recovery key, a successful disk backup, a checked restore into a separate folder, a visible disk schedule and one confirmed new photo upload. The next scheduled start is worth checking too. Keep Google's seven-day testing limit in mind if the OAuth project is still in Testing mode.
