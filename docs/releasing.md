# Builds and releases

Development is unchanged: commits go straight to `main`, the Mac Mini runs
an editable install of this working copy, and the laptop's `takeloom ui`
reinstalls from `main` via pipx whenever it's behind (`update_check.py`).
That self-update only ever applies to a pipx-from-git install. A package
manager's install (PyPI, Homebrew, apt, ...) has no git commit recorded,
so takeloom leaves updating it to the package manager.

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
GitHub release with the commits since the previous release. Each
distribution channel (PyPI, the Homebrew tap, the apt repository, Flathub,
the app stores) is published from this same workflow as it's added.

It refuses to run if `main` is already exactly a release.
