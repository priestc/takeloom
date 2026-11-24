# YouTube Streaming Setup

Takeloom's Streaming tab can push every session live to YouTube over RTMP using just a
stream key — no Google sign-in required. This page is only about the *optional* extra
step: connecting a YouTube account so each session's stream gets a real title and
description instead of whatever was last set on that stream key.

## Where the stream comes from

When the phones connect to the vault (takeloom-vault 0.18+, see
[takeloom-camera.md](takeloom-camera.md)), the **vault** makes the stream, not the
recording node: the phones' live feeds are already there, and it's wired to the router.
The node sends only its live mix (about 1.5 Mbps) and what to show: the program camera
(video-ui, the Stream Deck) and the footer. The vault puts each song's working title
(Completed Takes page) in the footer. Only the phones can be shown; a webcam plugged into
the node can't, so with one as the program camera the picture holds on the last phone.
The vault's stream log is the session's `stream.log` on the vault, and its Cameras page
shows a stream while it's live.

To stream somewhere besides YouTube at the same time, list full RTMP(S) URLs, stream key
included, in `streaming_extra_targets` in `studio_config.json` on the node (for example
`["rtmp://live.twitch.tv/app/<key>"]`).

## Why you have to create your own Google Cloud project

RTMP (the stream key) has no metadata channel at all — there's no way to set a title
through it. The only way to set one is Google's YouTube Data API, and that requires
OAuth: an app has to be registered with Google, and the user has to explicitly
authorize it.

Takeloom doesn't ship with a shared, built-in set of Google credentials for this — you
create your own, for free, and paste them into the Streaming tab. That's not a
corner-cutting workaround; it's the only realistic option, for one specific reason:

Google puts every new OAuth app in **Testing** mode. In that mode, an app can only be
used by up to 100 manually-added "test user" accounts, and **refresh tokens expire
every 7 days** for everyone on it. To lift those limits for arbitrary strangers, an app
has to go through Google's **verification** process — and the scope this feature needs
(full YouTube read/write access) is classified as a "restricted" scope, which on top of
the normal review requires an annual third-party **security assessment (CASA)**. Even
at the cheapest tier, that's realistically a few hundred to a couple thousand dollars —
*every year*, not once — before even counting the review turnaround time. (For
reference, that's exactly why an app like OBS can offer one-click YouTube sign-in with
no warnings: OBS Project went through that verification and pays to keep it current.
Takeloom, used by a handful of musicians, has no comparable case for that cost.)

The practical upshot: if you register your own OAuth client under your own Google
account, it stays in Testing mode too — but since *you're* the only test user on *your*
project, the 100-user cap is irrelevant. You'll just need to reconnect roughly once a
week when the refresh token expires (Takeloom will tell you via the streaming status
message if a session couldn't set a title for this reason).

## Step-by-step

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and sign in with
   the Google account that owns (or manages) your YouTube channel.
2. Create a new project (top bar → project selector → **New Project**). Any name is
   fine — this project is just a container for the credentials below.
3. Enable the API: **APIs & Services → Library**, search for **YouTube Data API v3**,
   and click **Enable**.
4. Set up the consent screen: **APIs & Services → OAuth consent screen**.
   - User type: **External** (there's no Google Workspace organization involved here).
   - Fill in the required fields (app name, your email) — the rest can be left blank.
   - Under **Test users** (or **Audience** on newer console layouts), add the same
     Google account you're signed in with. This is the step that actually grants your
     account permission to use the app while it's unverified.
5. Create credentials: **APIs & Services → Credentials → Create Credentials → OAuth
   client ID**.
   - Application type: **Desktop app** (this matters — Takeloom's OAuth flow expects a
     desktop-style client, not a web application).
   - Give it any name and click **Create**.
   - Copy the **Client ID** and **Client Secret** it shows you.
6. In Takeloom, open the **Streaming** tab, paste the Client ID and Client Secret into
   the "YouTube Account" section, and click **Connect YouTube Account**.
7. Your browser opens to a Google consent screen. You'll likely see a warning that says
   **"Google hasn't verified this app"** — this is expected (see above; it's your own
   app, talking to your own account). Click **Advanced**, then **Go to [your app name]
   (unsafe)**, and approve access.
8. Back in Takeloom, the Streaming tab should now say "Connected to YouTube."

## Notes

- **Cost:** none. The YouTube Data API's free daily quota (10,000 units/day) covers far
  more than any real personal use — titling a session costs on the order of 100 units,
  so roughly 100 sessions a day before you'd even approach the limit. No Cloud billing
  needs to be enabled for this.
- **Reconnecting:** while the app stays in Testing mode (it will, indefinitely — see
  above), you'll need to click **Connect YouTube Account** again about once a week.
- **If title automation fails for any reason** (expired token, network issue, stream
  key not found on the connected account), the actual video stream is unaffected — it
  keeps streaming normally, just without an updated title for that session.
- **Broadcast visibility:** the Streaming tab's visibility dropdown (Public / Unlisted
  / Private) controls what each session's auto-created broadcast is set to. It defaults
  to Unlisted so a first connection can't accidentally go public.
- **Title/description templates:** the Streaming tab's "Title template" and
  "Description template" fields control exactly what gets sent, using
  `{placeholder}` tokens: `{date}`, `{studio}`, `{studio-location}`, `{musician}`,
  `{project}`, `{instrument name}`. Defaults to `{date} * {musician} * {project}` for
  the title and `{studio}, {studio-location}, {instrument name}` for the description.
  A blank value (e.g. no musician set for an instrument) just disappears from the
  rendered text rather than being smoothed over — edit the template if that leaves
  stray punctuation you don't want.
- **Description footer:** every broadcast's description always ends with "Made with
  Takeloom" and a link back to this repo, appended after your own description
  template regardless of what it says.
