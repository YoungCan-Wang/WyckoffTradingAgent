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

Before publishing a release:

1. Merge only after all required PR checks, including all three package jobs, are successful.
2. Install each candidate on a clean machine of the matching architecture. Verify first launch, model setup, a two-turn chat, K-line opening, settings, quit, and relaunch.
3. Sign Windows executables with an Authenticode certificate. Unsigned candidates show an unknown-publisher warning and are not official releases.
4. Sign macOS builds with a Developer ID Application certificate, notarize them, and staple the ticket. Ad-hoc signed CI candidates are not official releases.
5. Create a versioned GitHub Release such as `desktop-v0.1.0`, attach the Windows installer and both DMGs, publish release notes, checksums, screenshots, supported systems, and known limitations.
6. Link the latest GitHub Release from the repository README and announcement copy. Documentation may explain the release, but it must not act as binary storage.

## Required signing configuration

The repository currently has no desktop signing secrets configured. A public desktop release remains blocked until the maintainer provides:

- macOS: Developer ID certificate plus Apple notarization credentials.
- Windows: Authenticode signing certificate or Azure Trusted Signing credentials.

The CI artifacts intentionally identify themselves as `unsigned` (Windows) or `adhoc` (macOS) so they cannot be mistaken for a production distribution.

## Release notes minimum

Every desktop release should tell the user:

- what the app does and which markets/models it supports;
- exact OS and architecture downloads;
- that model API keys stay in the local `~/.wyckoff` configuration;
- which actions require approval and that no investment outcome is guaranteed;
- verification performed and any remaining limitations;
- upgrade/uninstall instructions and a link to the issue tracker.
