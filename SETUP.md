# ClipBot — setup guide

ClipBot runs for free on **GitHub Actions**, so your computer doesn't need to be on.

* **Every night at 00:00 (Prague):** it finds the 3 most-viewed clips from the last 24 h for each creator in
  `config.yaml`. It converts them to vertical Shorts and saves them to Google Drive as
  `ClipBot/<Creator>/<date>/<Creator>-<date>-clip1.mp4`. Then it writes a title, description and hashtags for
  each clip and queues them. It also deletes Drive folders older than 3 days.
* **Every hour:** it uploads the clips that are due to YouTube, spread out over the day
  (15 clips = about 1 per hour, fewer clips = bigger gaps).

The one-time setup takes about 30–45 minutes. You'll collect **6 secret values** along the way. Paste them into a
notes file as you go:

| Secret name | Where it comes from |
|---|---|
| `TWITCH_CLIENT_ID` | Step 2 |
| `TWITCH_CLIENT_SECRET` | Step 2 |
| `GOOGLE_CLIENT_ID` | Step 3 |
| `GOOGLE_CLIENT_SECRET` | Step 3 |
| `GOOGLE_REFRESH_TOKEN` | Step 3 |
| `ANTHROPIC_API_KEY` | Step 4 (optional, but needed for good captions) |

---

## 1. Put the code on GitHub

1. Create a free account at <https://github.com> if you don't have one.
2. Click **New repository**, name it `clipbot`, choose **Private**, and click **Create repository**.
3. On the new repo page, click **uploading an existing file**. Unzip `clipbot.zip` and drag **everything inside the
   `clipbot` folder** into the page, including the `.github` folder. Then click **Commit changes**.
   * The `.github` folder is hidden on Mac. In Finder, press `Cmd + Shift + .` to show it.
   * Check afterwards that the repo contains `.github/workflows/fetch.yml`. Without it, nothing runs on a schedule.

## 2. Twitch API key

1. Go to <https://dev.twitch.tv/console/apps> and log in with any Twitch account. You must have two-factor
   authentication turned on.
2. Click **Register Your Application** and fill it in:
   * Name: `clipbot-yourname`. It must be unique.
   * OAuth Redirect URL: `http://localhost`
   * Category: *Application Integration*. Client type: *Confidential*.
3. Click **Manage**. Copy the **Client ID**, then click **New Secret** and copy the **Client Secret**.

## 3. Google (Drive + YouTube) access

### 3a. Create the Google project
1. Go to <https://console.cloud.google.com>. Log in with the Google account **that owns your YouTube channel**.
2. Click the project picker at the top, then **New project**. Name it `ClipBot` and click **Create**, then select it.
3. Go to **APIs & Services → Library**. Search for and **Enable** both of these:
   **Google Drive API** and **YouTube Data API v3**.

### 3b. Consent screen
1. Go to **APIs & Services → OAuth consent screen** (also called *Google Auth Platform*) and click **Get started**.
2. App name `ClipBot`, use your own email for support and contact, and choose Audience **External**. Click **Create**.
3. Under **Audience**, click **Publish app** so the status says **In production**.
   ⚠️ This step matters. If the app stays in *Testing*, Google expires your login after 7 days and the bot stops.
   You don't need Google to verify the app for your own use.

### 3c. OAuth client
1. Go to **Clients** (or *Credentials*), then **Create client** / **Create credentials → OAuth client ID**.
2. Application type: **Web application**. Name: `ClipBot`.
3. Under **Authorized redirect URIs**, add exactly `https://developers.google.com/oauthplayground`
4. Click **Create**. Copy the **Client ID** and **Client secret**.

### 3d. Get the refresh token (no installs needed)
1. Open <https://developers.google.com/oauthplayground>.
2. Click the ⚙️ gear (top right), tick **Use your own OAuth credentials**, and paste your Client ID and Secret.
3. In the box under the list on the left (*Input your own scopes*), paste:
   ```
   https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/youtube.upload
   ```
   Then click **Authorize APIs**.
4. Pick your Google account. If you have more than one YouTube channel, pick the **channel** you want to post to.
5. You'll see *"Google hasn't verified this app"*. Click **Advanced → Go to ClipBot (unsafe)**. This is your own app,
   so that's fine. Tick all permissions and click **Continue**.
6. Click **Exchange authorization code for tokens** and copy the **Refresh token**.

> Do you want Drive on a different Google account than YouTube? Repeat 3d while logged into the YouTube account,
> and save that second token as an extra secret called `YOUTUBE_REFRESH_TOKEN`.

ClipBot only gets the `drive.file` permission. That means it can see **only the files it creates** (the `ClipBot`
folder), not the rest of your Drive. So let the bot create that folder itself. Don't create it by hand.

## 4. Anthropic API key (for captions)

1. Go to <https://console.anthropic.com>, then **API Keys → Create key**.
2. Add a little credit under **Billing**. About 15 short caption requests a day cost only cents per month.
3. You can skip this step. ClipBot then just uses the original clip title plus some standard hashtags.

## 5. Add the secrets to GitHub

In your repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Add each value from the
table at the top, using the **exact names**.

## 6. First test run

1. In `config.yaml` (click it on GitHub, then the ✏️ pencil), set `privacy: private` for now and commit.
2. Open the **Actions** tab. If asked, click **I understand… enable workflows**.
3. Click **Daily fetch**, then **Run workflow**, then **Run workflow**. Wait a few minutes, then open the run to read the log.
   You should see lines like `Saved Agraelus-2026-11-10-clip1.mp4 -> "…"`, and the files should show up in Drive.
4. Click **Hourly post**, then **Run workflow**. Within a minute or two the first clip should appear in YouTube Studio.
5. When it all looks good, set `privacy: public` again. Everything now runs on its own.

## 7. ⚠️ YouTube "audit": read this before going public

YouTube **locks every video uploaded through the API to *private*** until your Google project passes a free
YouTube API compliance audit. This applies to new projects that haven't been audited.

* To apply, search for **"YouTube API Services – Audit and Quota Extension Form"**, fill it in for the `ClipBot`
  project, and describe it as *"an internal tool that uploads videos to my own channel."* Approval can take from a
  few days to several weeks.
* Until it's approved, uploads land as private. You can make them public by hand in YouTube Studio.
* The upload limit isn't an issue. The default is 100 uploads per day, and you need about 15.

## 8. Changing things later

Everything is in `config.yaml`:

* **Creators:** add or remove entries under `creators:`. For `channel`, use the name from the URL:
  `twitch.tv/<channel>` or `kick.com/<channel>`. Open each one in a browser once to check it's right.
* **Fetch time:** change `fetch_hour`, and **also** the two `cron:` lines in `.github/workflows/fetch.yml`
  (the comment there explains how).
* **Posting rhythm:** `posting.window_hours` (how many hours the posts are spread over), `min_interval_minutes`,
  and `start_hour` (for example `8` means nothing posts before 08:00).
* **Caption language:** `captions.language`.

To see the queue, open the `ClipBot/queue.json` file in Drive. Each clip there has its planned time, status
(`pending` / `posted` / `failed`), title and YouTube ID.

## Troubleshooting

| What you see in the log | What it means |
|---|---|
| `Kick returned HTTP 403` | Kick's Cloudflare protection blocked GitHub's servers. Tell Claude, and it can switch Kick to a paid clip-data service (for example Apify, a few dollars a month) or run that part from your own PC. |
| `invalid_grant` | The Google token expired. Check that step 3b says **In production**, then redo 3d and update `GOOGLE_REFRESH_TOKEN`. |
| `Twitch channel X not found` | There's a typo in `channel:` in `config.yaml`. |
| `uploadLimitExceeded` | YouTube's daily limit was hit. The clip is retried in the next hourly run. |
| The workflow never runs at night | Check that the Actions tab is enabled. GitHub can start scheduled runs 5–30 minutes late, which is normal. |

## A note on rights

The clips belong to the streamers. Every description links back to the streamer and the original clip, but it's still
best to **ask the streamers for permission**. Otherwise videos can be taken down with copyright claims. Most streamers
like clip channels that credit them.
