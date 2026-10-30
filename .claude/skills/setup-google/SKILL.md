---
name: setup-google
description: Guide the user through setting up Google sign-in (consent screen, OAuth clients) and Google Play billing (Play Developer API, service account, Play Console access, products) for this server, driven by `python -m app.config requirements` and verified with `python -m app.config check --online`. Use when the user wants to set up or fix sign-in or billing, when users get 401 errors or error 10 / DEVELOPER_ERROR, or when check --online reports a Google problem.
---

# Set up Google sign-in and Play billing

The server states what it needs; you translate that into Google's current settings. Neither this skill nor the docs contain console menus or setting names, because Google changes them often.

## Rules

- **The requirements come from the server**, never from memory: `docker compose run --rm api python -m app.config requirements --json` (or `<venv python> -m app.config requirements --json --secrets-dir secrets` without Docker, where `<venv python>` is `.\.venv\Scripts\python` on Windows and `.venv/bin/python` on macOS and Linux). Use the sections Google sign-in and Google Play.
- **Look up Google's current documentation** to find where each requirement is set, every time. Do not rely on remembered menu paths, labels, or permission names. If the documentation is unclear, say what you are unsure of and ask the user what their screen shows.
- **You cannot click in web consoles.** Tell the user what to set, one group at a time, with the requirement's reason, and wait for them.
- **Never ask the user to paste a secret into the chat**, and never read `secrets/google_play_service_account.json`. Client IDs are public and may be pasted; client secrets are never needed. Check the key only exists: `Get-Item secrets\google_play_service_account.json | Select-Object Name, Length` on Windows, `ls -l secrets/google_play_service_account.json` on macOS and Linux.
- **Give commands for the user's OS**: the docs show Windows (PowerShell) and macOS and Linux versions where they differ.
- **You may compute SHA-1 fingerprints** for keystores on this machine with the commands in `docs/google-sign-in.md`. Let the user run any command that needs a keystore password.
- After the user saves the key, run `git status --short`: only `secrets/` is ignored.

## Procedure

1. **Requirements.** Run `requirements --json`. Ask how the app is installed (from Android Studio, as a directly installed release build, from Google Play): each signing certificate needs its own Android client. If the app is or will be on Google Play, the Play app signing client is required.
2. **Sign-in.** Guide the user through each Google sign-in requirement. Put every client ID in `GOOGLE_OAUTH_CLIENT_IDS`, as in `docs/google-sign-in.md`.
3. **Billing.** Guide the user through each Google Play requirement, then the key and `server.env`, as in `docs/google-play-billing.md`.
4. **Verify.** Rebuild the API image if the code changed (`docker compose build api`), then run `docker compose run --rm api python -m app.config check --online`. Resolve every `FAIL` and `WARN` with its hint. A 401 or 403 from Google Play shortly after giving the service account access is usually Google's delay in applying permissions, up to a day: say so, and offer to check again later instead of changing settings. Requirements not marked `checked` cannot be verified by the server: confirm each with the user.
5. **Restart and test.** `docker compose restart api`, then have the user sign in from the app and call the balance endpoint, as in `docs/google-sign-in.md`. Test purchases need a license tester account and an install from a testing track.
