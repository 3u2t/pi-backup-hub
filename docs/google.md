# 2. Connect Google

[Back to installation](install.md) · Next: [first backup](first-backup.md)

Do this part in a browser on your computer, with the backup dashboard already open. You need Python 3 on this computer for the downloaded sign-in helpers.

Your Google account stays yours. Each installation needs its own Google client; the repository does not include a shared client or anyone else's credentials.

## Step 1: Create a project

Open [Google Cloud Console](https://console.cloud.google.com/projectcreate) and sign in with the account you want to use. Create a project named **Pi Backup Hub**. Select it in the project selector at the top.

You are creating an API project, not renting a Google Cloud server. Do not activate a paid trial or add billing just to follow this guide. This app still needs enough storage in your Google account.

## Step 2: Enable the two APIs

Open each link with your new project selected, then click **Enable**:

1. [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
2. [Photos Library API](https://console.cloud.google.com/apis/library/photoslibrary.googleapis.com)

If the button says **Manage**, that API is already enabled.

## Step 3: Set up the consent screen

Open **Google Auth Platform** in the Cloud Console. If it offers **Get started**, use it.

| Field | What to choose |
| --- | --- |
| App name | `Pi Backup Hub` |
| User support email | Your email address |
| Audience | **External**, for a normal personal Google account |
| Contact email | Your email address |

Save the form. Under **Audience → Test users**, add the Google account you will connect. If you plan to use different accounts for Drive and Photos, add both.

Google may change the wording or order of these screens. Its [consent-screen guide](https://developers.google.com/workspace/guides/configure-oauth-consent) covers the current fields.

## Step 4: Download the client file

Open **Clients → Create client** in Google Auth Platform:

1. Choose application type **Desktop app**.
2. Give it a name, such as **Pi Backup Hub sign-in**.
3. Create it and download its JSON file. If needed, open the new client to find the download option.

The type must be **Desktop app** because the helper receives Google's response on your computer. You do not need a public callback on your Pi for this sign-in flow. Keep the JSON file private. See [Google's desktop-app instructions](https://developers.google.com/identity/protocols/oauth2/native-app) if the client screen differs.

## Step 5: Import the file into the dashboard

Return to Pi Backup Hub and open **Google-Konten**. Under **Google-Client-JSON auswählen**, choose the JSON file you just downloaded.

The client status should change to **Eingerichtet**.

## Step 6: Connect Drive

Under **Google Drive verbinden**, click **Anmeldehelfer herunterladen**. Run the downloaded file on this computer, not on the Pi.

On Windows, open a terminal in your Downloads folder and run:

```powershell
py google-drive-anmelden.py
```

On Linux or macOS:

```sh
python3 google-drive-anmelden.py
```

Choose the intended Google account in the browser and review the permissions. The app requests access to Drive files that it creates. When the helper says **Erfolgreich verbunden**, return to the dashboard and check the displayed email address.

## Step 7: Connect Photos

Repeat the same process under **Google Fotos verbinden**, using the Photos helper:

```powershell
py google-photos-anmelden.py
```

Use `python3` instead of `py` on Linux or macOS. Choose the account with the storage you want to use. Allow the requested permissions to add media and read app-created media; otherwise the connection will fail.

After both connections succeed, delete the downloaded helpers. They contain a temporary pairing configuration and client credentials. The reusable `google_anmelden.py` in this repository is a template, not a configured sign-in helper.

## Optional: show API usage in the dashboard

1. In the same Google project, check that **Cloud Monitoring API** and **Service Usage API** are enabled. The dashboard links to both pages under **Einrichtung der Verbrauchsanzeige**.
2. Choose **Live-Anzeige verbinden** in the overview and run its downloaded helper on your computer, just like the earlier sign-in helpers.
3. Use an account that can read the project's monitoring data and quotas. Review the two additional read permissions before accepting them.

This is a separate connection. It asks for `monitoring.read` and `cloud-platform.read-only`; the latter can read other Cloud project data too. The app uses it to fetch quota configuration, and uses Monitoring to fetch quota usage. No billing account or paid resource is created. You can disconnect the read grant without disconnecting Drive or Photos.

The dashboard refreshes at most once per minute while it is open. Google can report metrics several minutes late. An empty metric is shown as unknown rather than zero, and user-specific quotas do not use the project total as a substitute for individual usage.

## Before relying on automatic backups

In Google's external **Testing** mode, refresh tokens for these permissions expire after seven days. A successful sign-in today does not remove that limit. See [Google's token-expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

For longer operation, review the project's publishing status and any requirements shown in Google Auth Platform. Publishing the GitHub repository does **not** publish the Google OAuth app or complete Google's checks. Do not fill public branding fields with invented details or assume a warning can be ignored.

## If something goes wrong

| What you see | What to check |
| --- | --- |
| Account is not allowed / access denied | Correct Google project, intended account added as a test user |
| Client JSON rejected | Client type is **Desktop app**, and you selected the downloaded JSON |
| Helper cannot reach the dashboard | `HUB_PUBLIC_URL` is the Pi's reachable LAN URL, not your computer's localhost |
| Missing permissions | Start a new helper from the dashboard and review all requested permissions |
| Authorization expired | Reconnect using a newly downloaded helper; check the testing-mode limit |

**You are done when both account cards show the intended email addresses.** Continue with [the first backup](first-backup.md).
