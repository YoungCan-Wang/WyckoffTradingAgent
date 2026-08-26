# Desktop packaging and release

The desktop app ships as a self-contained Electron application with a bundled Python IPC runtime. A renderer-only package or an installer containing a placeholder IPC executable is not a releasable build.

## Outputs

The `Desktop` workflow builds and smoke-tests three candidate installers on native GitHub-hosted runners:

| Platform | Architecture | Candidate artifact |
|---|---|---|
| Windows | x64 | NSIS `.exe` |
| macOS | Intel x64 | `.dmg` |
| macOS | Apple Silicon arm64 | `.dmg` |

Windows ARM64 is not a supported release target yet. The locked `cryptography`
dependency does not publish `win_arm64` wheels, so CI cannot currently create a
reproducible self-contained Python runtime for that architecture without adding
and maintaining a native OpenSSL build toolchain.

Each job runs the bundled `wyckoff-ipc` health and daemon entrypoints from the packaged application before upload. Candidate artifacts are retained for 14 days and are intended for maintainer testing, not as the public download channel.

Build locally from `desktop/`:

```bash
npm ci
npm run dist
```

The cross-platform Python bundler is `scripts/build_python_ipc.py`; `scripts/build_python_ipc.sh` remains as a macOS/Linux compatibility wrapper.

## Candidate versus public release

GitHub Releases is the public distribution and changelog surface. Workflow artifacts are temporary, hidden behind an Actions run, and must not be linked as the product download.

Normal branch and PR runs still produce unsigned/ad-hoc candidates retained for 14 days. A tag matching the version in
`desktop/package.json` turns the same verified workflow into the public release path:

```bash
git tag desktop-v0.1.0
git push origin desktop-v0.1.0
```

The `Desktop` workflow then validates tag/version equality, signs the Windows installer, signs and notarizes both macOS
DMGs, staples the notarization tickets, reruns packaged-runtime smoke checks, creates `SHA256SUMS.txt`, and publishes all
three installers in a GitHub Release. That tag push is the only per-release maintainer action; a missing credential fails
closed before packaging rather than publishing an unsigned binary.

Before tagging, merge only after all required PR checks and install the candidate on clean matching machines. Verify first
launch, model setup, a two-turn chat, K-line opening, settings, quit, and relaunch. Clean-machine acceptance remains a human
release decision; signing, notarization, checksums, upload and release-note generation are automated.

## Required signing configuration

Configure these GitHub Actions repository secrets once:

- `WINDOWS_CSC_LINK`, `WINDOWS_CSC_KEY_PASSWORD`: Authenticode certificate accepted by electron-builder and its password.
- `MACOS_CSC_LINK`, `MACOS_CSC_KEY_PASSWORD`: Developer ID Application certificate and its password.
- `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`: Apple notarization identity.

The CI artifacts intentionally identify themselves as `unsigned` (Windows) or `adhoc` (macOS) so they cannot be mistaken for a production distribution.

## User-visible update channel

The stable machine-readable endpoint is `https://wyckoff-analysis.pages.dev/desktop/latest`. It selects the newest
non-draft, non-prerelease `desktop-v*` GitHub Release and exposes its three assets. The desktop app checks this endpoint in
Settings → General → Software updates and opens the repository release page when a newer semantic version exists. The
manifest is only advisory: downloads remain signed GitHub Release assets and the main process allowlists this repository's
HTTPS release URLs before opening them.

## Release notes minimum

Every desktop release should tell the user:

- what the app does and which markets/models it supports;
- exact OS and architecture downloads;
- that model API keys stay in the local `~/.wyckoff` configuration;
- which actions require approval and that no investment outcome is guaranteed;
- verification performed and any remaining limitations;
- upgrade/uninstall instructions and a link to the issue tracker.
