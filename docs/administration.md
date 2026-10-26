# 🛠️ Administration

`python -m app.admin` shows what the server is doing and changes it while it runs: jobs, user accounts, the billing figures, and temporary limits. Run it in the `api` container, which has the billing database:

```shell
docker compose exec api python -m app.admin status
docker compose exec api python -m app.admin --help
```

Each command has its own `--help`, for example `python -m app.admin jobs cancel --help`. The commands run as the same user as the services, so files they write stay usable by them.

Every command that changes something:

* writes an entry to the audit log, with who (`--by`, default `admin`), what, when, and why (`--reason`). See [Audit log](#-audit-log).
* shows what it will change and asks first when the change is destructive: cancelling a job, queueing one again, taking minutes away, and deleting an account. `--yes` skips the question, for scripts.
* behaves like the matching action of a user or of the server: a cancelled job is charged like one the user cancels, and a job queued again is checked, reserved, and routed like a new one.

Commands that only read (`status`, `jobs`, `jobs show`, `user`, `billing`, `limits`, and `audit`) print JSON with `--json`, for scripts and monitoring. No output contains secrets or purchase tokens.

## 📊 Status

```shell
docker compose exec api python -m app.admin status
```

The first thing to look at when users say the server is slow:

* **Jobs:** places used out of `MAX_ACTIVE_JOBS`, jobs by status (being checked, uploading, queued, processing), and how many are assigned to the local and the remote backend.
* **Local worker:** its heartbeat, with how many jobs it runs at once and its average job time. No heartbeat means the worker is stopped or not consuming the local queue.
* **Remote:** remote slots in use out of the limit, when outsourcing is on.
* **Queues:** messages in the local and remote queues that are not finished yet, including ones a worker has taken, and dead-lettered messages.
* **Disk:** space used by job files, and space free on the data volume.
* **Backups:** how many billing backups are on this disk and the age of the newest; with off-site copies on, when the last copy succeeded, or why it failed.
* **Maintenance:** a pending restart or shutdown, from `scripts/server.py`.
* **Video sites:** for each site with something to report, whether its links are refused because it rate-limited this server or stopped answering, for how long and why, or whether they are read one at a time to recover; downloads running; and how many incidents there were. See [When a video site refuses the server](api.md#-when-a-video-site-refuses-the-server).
* **Controls:** active [limits](#-limits).

## 🧾 Jobs

```shell
docker compose exec api python -m app.admin jobs --failed --since 1d
docker compose exec api python -m app.admin jobs show 3f2a9c1e
```

`jobs` lists recent jobs from their [diagnostics records](architecture.md#-job-diagnostics), newest first, so jobs that ended long ago are included until `BILLING_JOB_DIAGNOSTICS_RETENTION`. Filter with `--failed` or `--status`, `--user` (email address or user ID), `--backend local` or `remote`, and `--since` (a duration such as `2h` or `7d`). `--limit` sets how many are listed (default 30).

A job is named by its ID or its first characters, as the list shows them.

* **`jobs show <job>`** prints everything known about the job: its live fields in Redis while they exist, the diagnostics record with the full error that users never see, its billing reservation, and the files in its folder.
* **`jobs cancel <job>`** cancels a queued or processing job, for example one stuck after a crash. It is charged like a job the user cancels: nothing below 5% progress, then part of its cost. `--refund` charges nothing, for a job that is stuck through no fault of the user. A worker running the job stops at its next progress update. Jobs being checked or uploaded are not queued yet; their placeholder expires on its own.
* **`jobs requeue <job>`** queues a failed link job again, under the same job ID, so the user's app finds it. It is checked, reserved, and charged like a new submission, because the failure gave the minutes back, and it counts against the job limits. Routing chooses the backend, or `--local` and `--remote` choose it. It works while the failed job is still in Redis, an hour after it failed. Failed uploads cannot be queued again: the file is deleted when the job fails. An app that stopped polling after the failure shows the job again when it next asks for its status.

## 👤 Users

These commands need billing; without it there are no accounts.

```shell
docker compose exec api python -m app.admin user user@example.com
docker compose exec api python -m app.admin user user@example.com credit 30 --reason "refund for a failed job"
docker compose exec api python -m app.admin user user@example.com credit -30 --reason "chargeback"
docker compose exec api python -m app.admin user user@example.com delete --reason "deletion request by email"
```

A user is named by email address or user ID. The email must belong to someone who has signed in at least once.

* **`user <user>`** shows the balance, minutes reserved by running jobs, the last and next free-minute grant, a block, purchases with order IDs and whether they were voided, the latest balance changes, and recent jobs. A negative balance means a refunded purchase's minutes were already used; see [Refunds and chargebacks](google-play-billing.md#-refunds-and-chargebacks).
* **`credit <minutes>`** adds minutes, or takes them away with a negative number, which asks first. The balance can go below zero. The reason is kept with the balance change and in the audit log.
* **`delete`** deletes the account and its data, the same as `DELETE /api/account`, including running jobs and remaining minutes, and cannot be undone. It asks first. See **Account deletion** in the [API guide](api.md#-account-deletion) for what is kept. Use it for deletion requests that arrive by email or a web form.

## 💰 Billing

```shell
docker compose exec api python -m app.admin billing --per month --since 365d
docker compose exec api python -m app.admin billing --remote-price-per-hour 1.2
```

Whether the price per minute covers the GPU time:

* **Minutes** per day or month (`--per`): sold, given free, used by transcriptions, taken back for voided purchases, and changed by hand. `--since` sets how far back (default `30d`). Balance changes are recorded from the version that added this command on; earlier ones are not in the report, and it says so.
* **Negative balances:** how many users owe minutes, and the largest.
* **Voided purchases:** when the API last read them from Google Play.
* **Completed jobs and their run time** per backend. Run time is from start to finish, including the download, so it overstates the time a remote GPU is billed for. With `--remote-price-per-hour`, your provider's price per hour of the GPU you use, it estimates the remote cost. Job records are kept for `BILLING_JOB_DIAGNOSTICS_RETENTION`, which limits how far back this goes.

Times are in UTC.

## 🚦 Limits

Temporary controls for abuse or a blocked video site. They apply to the next request, without editing `.env` or restarting, and never stop running or queued jobs.

```shell
docker compose exec api python -m app.admin limits
docker compose exec api python -m app.admin limits pause --message "Back at 18:00 UTC" --for 2h
docker compose exec api python -m app.admin limits resume
docker compose exec api python -m app.admin limits links off --for 1d
docker compose exec api python -m app.admin limits links on
docker compose exec api python -m app.admin limits sites reset youtube.com
docker compose exec api python -m app.admin limits block user@example.com --reason "abuse" --for 7d
docker compose exec api python -m app.admin limits unblock user@example.com
docker compose exec api python -m app.admin limits per-user 1 --for 6h
docker compose exec api python -m app.admin limits per-user default
```

* **`limits`** shows what is active, and for how long.
* **`pause`** refuses new transcriptions, links and uploads, with `503` and the `--message` (or a default) as `detail`. It is lighter than a [planned restart](architecture.md#-planned-restarts-and-shutdowns): nothing is announced in advance, and asking a cost still works. `resume` ends it.
* **`links off`** refuses video links, and their cost, with `503`; uploads are still accepted. Use it when a video site blocks the server. `links on` ends it.
* **`sites reset`** accepts links to a site again before its cooldown ends, and starts its next cooldown from the first again. Name the site by any of its hosts, such as `youtube.com` or `youtu.be`; without one, every site is reset. Use it once the site answers again, for example after the server's address changed. Resetting while the site still refuses the server only brings the next refusal sooner.
* **`block`** stops a user from starting transcriptions or checking a link's cost, with `403`. The user does not see the reason. Reading jobs, the balance, and deleting the account still work. Needs billing. `unblock` ends it.
* **`per-user`** replaces `MAX_ACTIVE_JOBS_PER_USER` while it is set; `default` goes back to the setting. Users above the new limit keep their jobs and cannot add more until they are below it. Needs billing, like the setting.

`--for` (a duration such as `30m`, `2h`, or `7d`) removes a control on its own after that time; without it, it lasts until removed. A pause or links-off with `--for` sends `Retry-After` with the time left. The controls are kept in Redis, so they last through a restart of the API but not a loss of Redis data.

## 📜 Audit log

```shell
docker compose exec api python -m app.admin audit
```

Lists the latest admin actions (`--limit`, default 30) with their time, who acted (`--by`), the action, its target (a job or user ID), details, and the reason. The log is the `admin_actions` table of the billing database, so the billing backups include it.

## 🧩 Using the commands from code

Each command is a data function in `app/admin/` that returns plain data, with the command line printing it. Another tool, such as an admin web page, calls the same functions, so it behaves exactly like the commands, including the audit log:

* Reading: `status.collect()`, `jobs.list_jobs()`, `jobs.job_details()`, `users.account()`, `report.billing_report()`, `limits.current()`, and `audit.entries()`.
* Changing: `jobs.cancel()`, `jobs.requeue()`, `users.credit()`, `users.delete()`, and the functions in `limits`. Each takes the actor and an optional reason for the audit log.
* Confirming: `jobs.cancel_plan()`, `jobs.requeue_plan()`, `users.credit_plan()`, and `users.delete_plan()` return what the matching change would do and change nothing. Show it before asking.

A function that cannot do what was asked raises `AdminError` (in `app.admin.common`) with a message for the operator. Durations are in seconds. The functions need the billing database and Redis, so they run in the API container.

## 🗄️ What is recorded

Two tables of the billing database support these commands:

* **`minute_ledger`**: every change to a balance, with its kind (purchase, free grant, use, voided purchase, change by hand), the minutes, the time, and a note: the order ID, the job ID, or the reason given.
* **`admin_actions`**: the audit log.

When an account is deleted, its ledger entries are kept without the user, so totals stay right, like its purchases. Audit entries about it, including the deletion, name it by a hash of its ID instead of the ID. Reasons are kept as written, so do not put an email address in one.
