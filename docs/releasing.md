# Releasing prherd

The **Release** workflow (`.github/workflows/release.yml`) builds prherd for macOS,
Windows and Linux, signs and notarizes the macOS app, writes the release notes, and
publishes a GitHub release. The Windows build is not code-signed (see
[Windows](#windows-isnt-code-signed)).

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
| Windows (x64) | `windows-latest` | `prherd-<version>-windows-x64.zip`, not code-signed |
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
fails without the macOS ones, unless the repository variable `ALLOW_UNSIGNED_RELEASES` is
`true`. In that case the macOS build is ad-hoc signed, with a warning. Pull request builds
are never signed. [Setting up macOS signing](#setting-up-macos-signing) walks through
getting each one.

| Secret | What it is |
| --- | --- |
| `MACOS_CERTIFICATE_P12` | Your **Developer ID Application** certificate and its private key, exported from Keychain Access as `.p12`, then `base64 -i cert.p12 \| pbcopy` |
| `MACOS_CERTIFICATE_PASSWORD` | The password you set on that export |
| `MACOS_SIGNING_IDENTITY` | The certificate's name, like `Developer ID Application: Your Name (TEAMID1234)` (`security find-identity -v -p codesigning`) |
| `APPLE_API_KEY` | An App Store Connect API key's `.p8` file, as its text, for notarization |
| `APPLE_API_KEY_ID` | That key's ID |
| `APPLE_API_ISSUER_ID` | The issuer ID shown above the keys in App Store Connect |
| `OPENROUTER_API_KEY` | Required. Lets a free model write the release notes (below) |

The macOS certificate goes into a temporary keychain that's deleted when the job ends.
The app is signed with the hardened runtime and `packaging/entitlements.plist`, then the
DMG is signed, notarized with `notarytool` and stapled. Notarization signs in with the
API key, so no Apple ID or app-specific password is involved. The key is written to a
file only for the `notarytool` call and deleted after.

## Setting up macOS signing

Once, by the developer account's **Account Holder**. It takes about half an hour, plus
Apple's approval of the membership.

1. **Join the Apple Developer Program** at
   [developer.apple.com/programs](https://developer.apple.com/programs/enroll/) ($99 a year).
   An individual membership signs as your name; an organization one needs a D-U-N-S
   number and signs as the company.
2. **Make a certificate signing request** on your Mac: Keychain Access → **Keychain
   Access** menu → **Certificate Assistant** → **Request a Certificate From a Certificate
   Authority**. Enter your email, choose **Saved to disk**, and save the `.certSigningRequest`.
3. **Create the certificate**: [Certificates, IDs &
   Profiles](https://developer.apple.com/account/resources/certificates/list) → **+** →
   **Developer ID Application** → the **G2 Sub-CA** → upload the request → **Download**.
   Double-click the `.cer` so it joins your login keychain, next to the private key the
   request made. (Xcode can do steps 2 and 3 in one: **Settings → Accounts → Manage
   Certificates → + → Developer ID Application**.)
4. **Check it and copy its name**:
   ```sh
   security find-identity -v -p codesigning
   ```
   The line that says `Developer ID Application: Your Name (TEAMID1234)` is the identity.
   Everything inside the quotes is `MACOS_SIGNING_IDENTITY`. If you already have one,
   start here.
5. **Export it with its key**: Keychain Access → **login** → **My Certificates**, expand
   the Developer ID Application certificate so its private key shows, select both,
   **File → Export Items…**, save as `prherd.p12`, and set a password. That password is
   `MACOS_CERTIFICATE_PASSWORD`.
6. **Make an API key for notarization**: [App Store Connect](https://appstoreconnect.apple.com)
   → **Users and Access** → **Integrations** → **App Store Connect API** → **Team Keys**.
   The first time, request access, which is granted straight away. Then **+**, name it
   `prherd notarization`, give it the **Developer** role, and **Generate**.
   - **Download** the `.p8` file. Apple lets you download it only once.
   - The key's ID is in its row: `APPLE_API_KEY_ID`.
   - The **Issuer ID** above the list is `APPLE_API_ISSUER_ID`.
7. **Add the six secrets** (`gh secret set` asks for each value when it isn't piped in):
   ```sh
   base64 -i prherd.p12 | gh secret set MACOS_CERTIFICATE_P12 -R pacnpal/prherd
   gh secret set MACOS_CERTIFICATE_PASSWORD -R pacnpal/prherd
   gh secret set MACOS_SIGNING_IDENTITY -R pacnpal/prherd
   gh secret set APPLE_API_KEY -R pacnpal/prherd < AuthKey_XXXXXXXXXX.p8
   gh secret set APPLE_API_KEY_ID -R pacnpal/prherd
   gh secret set APPLE_API_ISSUER_ID -R pacnpal/prherd
   ```
   Then delete `prherd.p12`, the `.certSigningRequest` and the `.p8`. The certificate
   stays in your keychain, and a lost key can be revoked and replaced in App Store Connect.
8. **Try it with a prerelease**: run the workflow by hand with a version like
   `1.0.0-rc.1`. In the macOS job's log, `notarytool` should end with `status: Accepted`,
   and `spctl` should say `source=Notarized Developer ID`. Then open the DMG from the
   release on a Mac: it should open with no warning.

The certificate lasts five years. When it's renewed, repeat steps 2 to 5 and update
`MACOS_CERTIFICATE_P12`, `MACOS_CERTIFICATE_PASSWORD` and `MACOS_SIGNING_IDENTITY`.
If notarization fails, the log names the file Apple rejected; `xcrun notarytool log
<submission id> --key AuthKey.p8 --key-id … --issuer …` gives the full reasons.

## Windows isn't code-signed

The Windows build ships unsigned, on purpose. A Windows code-signing certificate costs
hundreds of dollars a year, and since mid-2023 it has to live on a hardware token or a
cloud HSM, so it can't simply be stored as a secret. Signing also wouldn't stop the first
warning: SmartScreen trusts a new certificate only after enough people have run what it
signed.

So the first time someone opens `prherd.exe`, Windows says it "protected your PC". Choose
**More info**, then **Run anyway**. Each release's notes say this, and `SHA256SUMS.txt`
lets anyone check the zip is the one the workflow built.

To sign it later, add a step before **Package (Windows)** that signs
`dist\prherd\prherd.exe` with your provider's tool, such as Azure Trusted Signing's
action or `signtool` with a token.

## Release notes

[`pacnpal/free-ai-release-notes`](https://github.com/marketplace/actions/free-ai-release-notes)
writes the title and notes from every commit since the previous `v*` tag (merge commits
left out). It uses free OpenRouter models with `OPENROUTER_API_KEY`:

- The title is `v1.2.0 - ` plus a short summary of the release.
- **Breaking changes come first.** Commits marked `feat!:` or carrying a
  `BREAKING CHANGE:` footer are sent to the model as ones it must list there, with what
  to do when upgrading. If it leaves one out, the action only logs a warning, so check
  the run's log for it and that each flagged change made it into the notes.
- Then come New Features, Bug Fixes, Performance, Documentation and Maintenance, as they
  apply, and the Full Changelog link.
- The workflow adds the download table under the notes.

A release fails without the key. If every model fails, the notes fall back to GitHub's
generated notes or a plain commit list, titled with the tag. Check the notes on the release page after
publishing, since a model wrote them.

Each pull request's run previews the notes its changes would get, in the **Preview
release notes** job's summary.
