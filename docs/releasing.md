# Builds and releases

Development is unchanged: commits go straight to `main`, the Mac Mini runs
an editable install of this working copy, and the laptop follows `main`
in development mode.

## Development mode

A pipx install can follow GitHub's `main` instead of releases: on each
launch, `takeloom ui` reinstalls from `main` if it's moved, and restarts
(`update_check.py`). Turn it on or off in the Remote tab's "This
Computer's Takeloom" section, or with `takeloom dev-mode on|off`. It takes
effect the next time the UI opens:

- on, in a release install: switches to `main`;
- off, in a `main` install: switches back to the latest PyPI release;
- never set: on for a `main` install, so existing ones keep updating.

With it off, nothing is checked at launch. Installs that pipx doesn't
manage (Homebrew, apt, the App Store) don't have development mode; their
package manager updates them. The Mac Mini's editable install always runs
its working copy, so it doesn't either.

## Versions

The version comes from git tags (hatch-vcs, see `pyproject.toml`):

- a release is the tag `vX.Y.Z`, and builds as exactly `X.Y.Z`;
- anything after it builds as `X.Y.(Z+1).devN`, N commits later.
  Package managers and pip sort that after the last release and before
  the next one, and never treat it as a release.

`daily-YYYY-MM-DD` tags are ignored for versioning.

## Daily builds

`.github/workflows/daily.yml` runs at 07:00 UTC, just after midnight in
Denver. If anything was committed since the last daily build or release,
it builds the sdist and wheel and publishes them as a GitHub **pre-release**
tagged `daily-YYYY-MM-DD`, named for the day of the newest commit. The
notes list the commits since the previous build. On a day with no
commits, nothing is published. Only the 14 most recent daily builds are
kept. Nothing goes to package managers.

Run one early from the Actions tab (Daily build → Run workflow), or:

    gh workflow run daily.yml

## Releases

Every few development days, make a release:

    gh workflow run release.yml -f bump=minor     # or patch / major

or Actions tab → Release → Run workflow. It tags the current `main` as the
next version, builds and smoke-tests it, pushes the tag, and publishes a
GitHub release with the commits since the previous release. Then it sends
the release to each distribution channel:

- **PyPI** (`pipx install takeloom`): trusted publishing, so no token is
  stored. PyPI's takeloom project trusts workflow `release.yml` in the
  GitHub environment `pypi`. Renaming either breaks publishing until the
  publisher settings on pypi.org are updated to match.

- **Homebrew** (`brew install priestc/takeloom/takeloom`): the tap repo
  [priestc/homebrew-takeloom](https://github.com/priestc/homebrew-takeloom)
  has one formula, pointing at the release's sdist on PyPI. The workflow
  rewrites its `url`/`sha256` and pushes, using the `HOMEBREW_TAP_TOKEN`
  secret: a fine-grained token with contents read/write on that repo only.
  Without the secret, the release still happens and only this step is
  skipped, with a warning. The formula gives takeloom its own virtualenv,
  letting pip install the Python dependencies as wheels, and fetches the
  static ffmpeg at install time (see `ffmpeg_bin.py`). `brew install
  --HEAD` builds `main` instead.

The apt repository, Flathub and the app stores get added here the same way.

It refuses to run if `main` is already exactly a release.
