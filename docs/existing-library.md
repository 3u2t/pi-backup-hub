# If your photos are already in Google Photos

[Back to installation](install.md#step-6-check-existing-google-photos-uploads)

This step is only for a **new installation before its first start**, when you have already uploaded the existing Immich library manually. It tells the app which files to skip. It does not upload, verify or delete anything in Google Photos.

If you want the app to upload the entire library, do not create a baseline.

## Mark the existing library as already uploaded

Only do this if you have checked that **all current, non-deleted, online Immich assets** are already in the intended Google Photos account. The query below marks that entire set as uploaded. If only some are uploaded, do not use it as-is; you need a baseline containing just that subset.

Run this on the Pi, replacing the uppercase container, administrator and database names. Use your chosen private app data directory if it differs from `/srv/pi-backup-hub`:

```sh
docker exec -i YOUR_POSTGRES_CONTAINER psql -X -v ON_ERROR_STOP=1 -U YOUR_DATABASE_ADMIN -d YOUR_IMMICH_DATABASE -At <<'SQL' | sudo tee /srv/pi-backup-hub/baseline-assets.json >/dev/null
SELECT COALESCE(json_agg(json_build_object(
  'id', id::text,
  'checksum', encode(checksum, 'hex'),
  'type', type,
  'name', "originalFileName",
  'path', "originalPath",
  'created', "createdAt",
  'updated', "updatedAt"
)), '[]'::json)
FROM asset
WHERE "deletedAt" IS NULL AND NOT "isOffline";
SQL
sudo chmod 600 /srv/pi-backup-hub/baseline-assets.json
sudo python3 -c 'import json; from pathlib import Path; items=json.loads(Path("/srv/pi-backup-hub/baseline-assets.json").read_text()); assert isinstance(items,list); print(len(items), "assets marked as already uploaded")'
```

The query only reads Immich. The file contains private asset IDs, names and paths; keep it out of GitHub. The final command must print the expected asset count. If any command fails, stop and fix it before starting the app.

On first start the app imports this file once. The dashboard should show the expected count under **Bereits manuell importiert**. New assets found afterwards are queued normally.

## What this cannot check

The [Google Photos Library API](https://developers.google.com/photos/support/updates) can list media created by this app, not your entire manual library. A baseline records your confirmation; it is not a comparison with Google Photos.

Do not replace the app database or re-run an installation to reset an existing queue. An existing installation needs its own migration plan if its baseline is wrong.
