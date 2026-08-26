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

Routine push and PR runs stop after the native-platform Electron tests; they do not enter the packaging jobs. A manual
candidate run builds the three installers, runs the bundled `wyckoff-ipc` health and daemon entrypoints, and retains the
downloads for at most one day. Tag releases run the same packaged-runtime checks and delete their temporary Actions
artifacts immediately after copying the installers to GitHub Releases. A separate daily cleanup removes any still-active
artifact of at least 50 MB once it is more than 24 hours old.

Build locally from `desktop/`:

```bash
npm ci
npm run dist
```

The cross-platform Python bundler is `scripts/build_python_ipc.py`; `scripts/build_python_ipc.sh` remains as a macOS/Linux compatibility wrapper.

## Candidate versus public release

GitHub Releases is the public distribution and changelog surface. Workflow artifacts are temporary, hidden behind an Actions run, and must not be linked as the product download.

Normal branch and PR runs execute tests only. A manual workflow run builds and uploads candidates for one day when a
clean-machine download is actually needed. Public releases should be started with the repository's user-invocable
`desktop-release` Skill, which validates `main`, increments `desktop/package.json` and its lockfile, waits for the version
commit's CI, and pushes a matching `desktop-vX.Y.Z` tag:

```text
/desktop-release patch
/desktop-release minor
/desktop-release 0.2.0
```

When no argument is supplied, the Skill defaults to a patch release. The equivalent low-level tag operation is:

```bash
git tag desktop-v0.1.0
git push origin desktop-v0.1.0
```

The `Desktop` workflow then validates tag/version equality, reruns packaged-runtime smoke checks, creates
`SHA256SUMS.txt`, and publishes the unsigned Windows installer and ad-hoc-signed macOS DMGs in a GitHub Release. This path
uses no paid code-signing certificate, Apple Developer membership, or notarization service. The Release title and notes
explicitly disclose the unsigned status.

Before tagging, merge only after all required PR checks and install the candidate on clean matching machines. Verify first
launch, model setup, a two-turn chat, K-line opening, settings, quit, and relaunch. Clean-machine acceptance remains a human
release decision; checksums, upload and release-note generation are automated.

## Zero-cost signing policy

No signing secrets are required or consumed. Windows packages are unsigned. macOS packages use local ad-hoc signing only
so their bundle structure can still be verified, but they are not Apple-notarized. Consequently:

- Windows may show a Microsoft Defender SmartScreen unknown-publisher warning.
- macOS may block the first launch; the user can use Finder's right-click → Open flow after verifying the checksum and source.
- Do not describe these binaries as signed or notarized production installers.

The CI artifacts intentionally identify themselves as `unsigned` (Windows) or `adhoc` (macOS) so they cannot be mistaken for a production distribution.

## User-visible update channel

The stable machine-readable endpoint is `https://wyckoff-analysis.pages.dev/desktop/latest`. It selects the newest
non-draft, non-prerelease `desktop-v*` GitHub Release and exposes its three assets. The desktop app checks this endpoint in
Settings → General → Software updates and opens the repository release page when a newer semantic version exists. The
manifest is only advisory: downloads remain GitHub Release assets and the main process allowlists this repository's
HTTPS release URLs before opening them.

## Release notes minimum

Every desktop release should tell the user:

- what the app does and which markets/models it supports;
- exact OS and architecture downloads;
- that model API keys stay in the local `~/.wyckoff` configuration;
- which actions require approval and that no investment outcome is guaranteed;
- verification performed and any remaining limitations;
- upgrade/uninstall instructions and a link to the issue tracker.
