# Releasing prherd

The **Release** workflow (`.github/workflows/release.yml`) builds prherd for macOS,
Windows and Linux, signs the builds, writes the release notes, and publishes a
GitHub release.

## Cutting a release

The version comes from whatever starts the run:

| Trigger | Version | Result |
| --- | --- | --- |
| Push a tag: `git tag v1.2.0 && git push origin v1.2.0` | `1.2.0`, from the tag | Release `v1.2.0` from that tag |
| **Actions → Release → Run workflow**, version `1.2.0` | `1.2.0`, from the input | Tags `v1.2.0` on the chosen branch's head and releases it |
| A pull request that touches the app or the build | `0.0.0-pr.<number>+<sha>` | Builds only, as workflow artifacts. Previews the notes in the run summary |

A version with a suffix, like `1.3.0-rc.1`, is published as a prerelease. Running it
by hand refuses a version whose tag already exists.

The version goes into the app (`prherd --version`), the macOS `Info.plist`
(`CFBundleShortVersionString` takes the `x.y.z` part, `CFBundleVersion` the run number),
the Windows file properties, and the file names.

## What gets built

| Platform | Runner | Artifact |
| --- | --- | --- |
| macOS (Apple silicon) | `macos-latest` | `prherd-<version>-macos-arm64.dmg`, signed, notarized and stapled |
| Windows (x64) | `windows-latest` | `prherd-<version>-windows-x64.zip`, with `prherd.exe` Authenticode-signed |
| Linux (x64) | `ubuntu-22.04` | `prherd-<version>-linux-x64.tar.gz`, with `prherd.desktop` and the icon |

`SHA256SUMS.txt` covers all three. Linux is built on the oldest supported Ubuntu so its
bundled glibc runs on older distributions too. There's no Intel macOS build.

`packaging/build.py` does the build on each runner, the same way it runs locally:

```sh
python3 -m pip install -r packaging/requirements.txt
python3 packaging/build.py --version 1.2.0
```

It writes the version into a copy of `prherd.py`, makes the platform's icon from
`assets/prherd.png` (`.icns` on macOS, `.ico` on Windows), and runs PyInstaller. The
icon also goes inside the app, so the window shows it.

## Signing secrets

Add these under **Settings → Secrets and variables → Actions → Secrets**. A release
fails without them, unless the repository variable `ALLOW_UNSIGNED_RELEASES` is `true`.
In that case macOS builds are ad-hoc signed and Windows builds unsigned, each with a
warning. Pull request builds are never signed.

| Secret | What it is |
| --- | --- |
| `MACOS_CERTIFICATE_P12` | Your **Developer ID Application** certificate and its private key, exported from Keychain Access as `.p12`, then `base64 -i cert.p12 \| pbcopy` |
| `MACOS_CERTIFICATE_PASSWORD` | The password you set on that export |
| `MACOS_SIGNING_IDENTITY` | The certificate's name, like `Developer ID Application: Your Name (TEAMID1234)` (`security find-identity -v -p codesigning`) |
| `APPLE_ID` | The Apple ID of the developer account, for notarization |
| `APPLE_APP_SPECIFIC_PASSWORD` | An app-specific password for that Apple ID, from account.apple.com |
| `APPLE_TEAM_ID` | The 10-character team ID |
| `WINDOWS_CERTIFICATE_PFX` | A code-signing certificate as `.pfx`, base64-encoded (`[Convert]::ToBase64String([IO.File]::ReadAllBytes('cert.pfx'))`) |
| `WINDOWS_CERTIFICATE_PASSWORD` | The `.pfx` password |
| `OPENROUTER_API_KEY` | Optional. Lets a model write the release notes (below) |

The macOS certificate goes into a temporary keychain that's deleted when the job ends.
The app is signed with the hardened runtime and `packaging/entitlements.plist`, then the
DMG is signed, notarized with `notarytool` and stapled.

Code-signing certificates issued since mid-2023 live on hardware tokens or cloud HSMs and
can't be exported as a `.pfx`. If that's yours, replace the Windows signing step with your
provider's signing action, such as Azure Trusted Signing.

## Release notes

`scripts/release_notes.py` writes the title and notes from the full message of every
commit since the previous `v*` tag. With `OPENROUTER_API_KEY` set, a free model on
OpenRouter (the `openrouter/free` router) writes them in the same style as
[mcpelevator's releases](https://github.com/pacnpal/mcpelevator/releases):

- The title is `v1.2.0 - ` plus three to five of the biggest changes.
- **Breaking changes come first**, grouped by version, each saying what changed, who it
  affects and what to do instead. Earlier releases' breaking changes are carried along.
- The version's own section opens with a short summary. Then come one section per
  change with its pull request numbers, then Fixes, Security, Dependencies and upgrade
  notes as they apply.
- **Then the download table and the Full Changelog link**, followed by the previous
  release's notes, so one page covers an upgrade across several versions.

The model only sees the commits. A reply that cites a pull request number that no commit
mentions is thrown away, as is one that doesn't follow the format. After three tries, or
with no key, the notes list the commit subjects in the same layout. Check the notes on
the release page after publishing, since a model wrote them.

Each pull request's run previews the notes its changes would get, in the **Preview
release notes** job's summary.
