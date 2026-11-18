# ClipBot — setup guide

ClipBot runs for free on **GitHub Actions**, so your computer doesn't need to be on.

* **Every night at 04:00 (Prague):** it finds the 3 most-viewed clips from the last 24 h for each creator in
  `config.yaml`. It converts them to vertical Shorts (blurred, +20 % saturated background, your logo on top) and
  saves them to Google Drive as `ClipBot/<Creator>/<date>/<Creator>-<date>-clip1.mp4`. Then it writes a title,
  description and hashtags for each clip and queues them. It also deletes Drive folders older than 3 days.
* **Right after the fetch** (`posting.mode: batch`) it uploads **all** new Shorts to YouTube as private, so in the
  morning they're waiting in YouTube Studio and you publish/schedule them yourself. (`mode: spread` = one by one
  between 07:00 and 02:00 instead.) After YouTube's API audit, `youtube.schedule_publish: true` makes YouTube publish
  them by itself at spread-out times.
* **Every night at ~04:37:** it builds a **10–25 minute horizontal compilation** of the best CZ/SK clips of the last
  24 h (see "Daily compilation" below) and uploads it **right away as private** ("… | Klipy dne #N"), so you can check
  it and publish it at 18:00 (or, after the audit, YouTube publishes it at `compilation.publish_hour` by itself).
* **Every hour (backup):** retries any upload that failed and finds live CZ/SK channels for the compilation.

The one-time setup takes about 30–45 minutes. You'll collect **7 secret values** along the way. Paste them into a
notes file as you go:

| Secret name | Where it comes from |
|---|---|
| `TWITCH_CLIENT_ID` | Step 2 |
| `TWITCH_CLIENT_SECRET` | Step 2 |
| `GOOGLE_CLIENT_ID` | Step 3 |
| `GOOGLE_CLIENT_SECRET` | Step 3 |
| `GOOGLE_REFRESH_TOKEN` | Step 3 (Drive) |
| `YOUTUBE_REFRESH_TOKEN` | Step 3 (YouTube) |
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
3. You'll do steps 3–6 **twice**, because Google doesn't allow both permissions in one login.
   In the box under the list on the left (*Input your own scopes*), paste **only**
   `https://www.googleapis.com/auth/drive.file` the first time. The token you get from that is **`GOOGLE_REFRESH_TOKEN`**.
   The second time, paste **only** `https://www.googleapis.com/auth/youtube.upload`. That token is **`YOUTUBE_REFRESH_TOKEN`**.
   Each time, click **Authorize APIs**.
4. Pick your Google account. If you have more than one YouTube channel, pick the **channel** you want to post to.
5. You'll see *"Google hasn't verified this app"*. Click **Advanced → Go to ClipBot (unsafe)**. This is your own app,
   so that's fine. Tick all permissions and click **Continue**.
6. Click **Exchange authorization code for tokens** and copy the **Refresh token**.

> Before the second round, click ⚙️ and make sure your own credentials are still ticked. Then click
> **Step 1** on the left to start again. Drive and YouTube can be on different Google accounts if you want.

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
* **Posting rhythm:** `posting.quiet_hours` (no posts in that range), `start_hour`, `min_interval_minutes`.
* **Logo and look:** `overlay:` (image, position, width, opacity) and `youtube.background_saturation`.
* **Caption language and model:** `captions.language`, `captions.model`.

## Daily compilation

* **Which channels:** every 2 hours ClipBot asks Twitch who is streaming in Czech or Slovak right now and remembers
  those channels in `ClipBot/channels.json` on Drive (no manual list needed). The Shorts creators are always included.
  Kick can't be searched by language, so add extra Kick channels under `compilation.extra_channels`.
* **What counts as popular:** each clip's views divided by that streamer's usual clip views (the average of the last
  14 days). `score_threshold: 2.0` = at least twice as many views as usual. On a thin day the bar is lowered step by step
  until there are at least `min_minutes` of clips; below that, no video is made that day.
* **Order:** the strongest clip opens the video, the other highlights are spread through it, everything else is
  shuffled, and the same streamer never appears twice in a row (max `max_clips_per_streamer` per streamer).
* **Look:** 1080p30, the streamer's name in the corner for the first 5 s of each clip, your logo top-right, even volume.
  Thumbnail = a frame from the opening clip + big text + logo (needs a phone-verified YouTube channel).
* **Run it by hand:** Actions → **Daily compilation** → Run workflow. It uploads at the next 17:00 (or right away
  if it's later than that).

To see the queue, open the `ClipBot/queue.json` file in Drive. Each clip there has its planned time, status
(`pending` / `posted` / `failed`), title and YouTube ID.

## English GTA channel (second YouTube channel, compilations only)

Same code, its own settings in `config_gta.yaml`, its own Drive folder (`ClipBotGTA`) and its own workflow
(`.github/workflows/gta.yml`). It makes:

* **Group videos**: one compilation per streamer group (CORE, Kai Cenat & AMP, Kick Crew, GTA RP), at most every
  8 h, only GTA V / GTA VI clips from Twitch and Kick. Aim ~12 min (max 25). If there isn't enough new material it is
  shorter, and under 3 min there's no video. Title: `<AI title> | CORE GTA Highlights #N` (own numbering per group).
* **Best GTA 6 clips of the day**: once a day (from 06:00 Prague), the most viewed **English** GTA VI clips of the last
  24 h from Twitch and Kick, 15-20 min. Title: `<AI title> | GTA 6 Best Clips of the Day #N`.
* **No repeats**: every used clip is saved in `ClipBotGTA/gta_used.json` (60 days), including the moment it shows, so
  a second clip of the same moment, the same moment on Twitch + Kick (simulcast), or two members' POVs of the same
  scene are skipped too. The best-of only avoids its own earlier clips, so it may contain a clip that was already in a
  group video (`gta.best.reuse_group_clips: false` turns that off).

Setup:
1. Create the new YouTube channel (a brand channel on your Google account is easiest) and verify it by phone
   (youtube.com/verify), otherwise custom thumbnails don't work.
2. OAuth Playground (step 3d), scope `https://www.googleapis.com/auth/youtube.upload` only. When Google asks
   which channel, pick the **GTA channel**. Save the refresh token as the GitHub secret **`YOUTUBE_REFRESH_TOKEN_GTA`**.
   Everything else (Twitch, Google client, Drive token, Anthropic) is shared with the CZ channel.
3. Put the channel logo as `logo_gta.png` (PNG, transparent background) in the repo root. Without it the videos
   work, just without a logo in the static transitions and the thumbnail.
4. cron-job.org: a new job like the CZ ones, URL
   `https://api.github.com/repos/K1tley/cbot_klipa/actions/workflows/gta.yml/dispatches`, body
   `{"ref":"main","inputs":{"job":"all","group":"","force":"false"}}`, every 8 h (e.g. 06:23, 14:23, 22:23).
   With `"force":"false"` each run only makes what is due.
5. Manual run: **Actions → GTA channel → Run workflow** (choose all / groups / best, optionally one group).

Groups and members are in `config_gta.yaml` under `gta.groups.list` (`twitch:` login and/or `kick:` slug).
Without `YOUTUBE_REFRESH_TOKEN_GTA` the videos are still made and stored on Drive; they go up on the first run
after the secret is added (within 3 days, then Drive deletes them).

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
