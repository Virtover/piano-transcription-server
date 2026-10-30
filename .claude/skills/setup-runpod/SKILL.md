---
name: setup-runpod
description: Guide the user through setting up RunPod outsourcing for this server (S3-compatible storage, the handler image on a registry, the RunPod Serverless endpoint and API key, secrets and server.env), driven by `python -m app.config requirements` and verified with `python -m app.config check --online`. Use when the user wants to set up, fix, or update outsourcing to RunPod, or when check --online reports a storage or RunPod problem.
---

# Set up RunPod outsourcing

The server states what it needs; you translate that into the providers' current settings. Neither this skill nor the docs contain provider menus or setting names, because those change.

## Rules

- **The requirements come from the server**, never from memory: `docker compose run --rm api python -m app.config requirements --json` (or `<venv python> -m app.config requirements --json --secrets-dir secrets` without Docker, where `<venv python>` is `.\.venv\Scripts\python` on Windows and `.venv/bin/python` on macOS and Linux). Use the sections Storage, Image registry, and GPU endpoint.
- **Look up the providers' current documentation** to find where each requirement is set, every time. Do not rely on remembered menu paths or labels. If the documentation is unclear, say what you are unsure of and ask the user what their screen shows.
- **You cannot click in web consoles.** Tell the user what to set, one group at a time, with the requirement's reason, and wait for them.
- **Never ask the user to paste a secret into the chat**, and never read secret files. Give them the command from "Configuration" in `docs/runpod.md` to run in their own terminal, then check only that the file exists and is not empty: `Get-Item secrets\<name> | Select-Object Name, Length` on Windows, `ls -l secrets/<name>` on macOS and Linux.
- **Give commands for the user's OS**: the docs show Windows (PowerShell) and macOS and Linux versions where they differ.
- **Ask before outward-facing actions:** `docker push`, or changing the user's cloud accounts through an API.
- After the user writes a secret, run `git status --short`: only `secrets/` is ignored, so a stray file elsewhere could be committed.

## Procedure

1. **Prerequisites.** Docker Desktop runs (`docker info`), and the disk Docker uses (drive C: on Windows) has at least 20 GB free for the image build. If not, follow "Freeing disk space" in `docs/setup.md`.
2. **Requirements.** Run `requirements --json`. Summarize for the user what will be created at which provider, and ask which providers they use if they differ from the defaults (any S3-compatible storage and any registry work).
3. **Storage, then registry.** Guide the user through each requirement. Then the secrets and the `server.env` lines from "Configuration" in `docs/runpod.md`. Run `requirements` again: the placeholders are now filled with their values.
4. **Image.** Build with a new tag (see "Building the image" in `docs/runpod.md`), then ask before pushing.
5. **GPU endpoint and API key.** Guide the user through every GPU endpoint requirement, including its environment variable, then the API key secret and `RUNPOD_ENDPOINT_ID`.
6. **Verify.** Rebuild the API image if the code changed (`docker compose build api`), then run `docker compose run --rm api python -m app.config check --online`. Resolve every `FAIL` and `WARN` with its hint, and repeat until nothing fails. Requirements not marked `checked` cannot be verified by the server: confirm each with the user.
7. **Start and test.** Follow steps 5 and 6 of "Setting it up" in `docs/runpod.md`. Ask before stopping the local worker, because a running job would fail.
8. **Finish.** User audio now goes to the storage provider and RunPod: the app's privacy policy must name both as processors. Suggest removing old local images.
