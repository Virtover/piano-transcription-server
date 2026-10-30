# 🛠️ Administration

`python scripts/admin.py` shows what the server is doing and changes it while it runs: jobs, user accounts, the billing figures, and temporary limits. Run it on the host, in the project folder:

```shell
python scripts/admin.py status
python scripts/admin.py --help
```

Each command has its own `--help`, for example `python scripts/admin.py jobs cancel --help`.

The script runs `python -m app.admin` in the `api` container, which has the billing database, as the same user as the services, so files it writes stay usable by them. It adds two things to `docker compose exec api python -m app.admin`, which runs the same commands: the audit log names you by your user name on this machine, and without a terminal, as in a script or a pipe, the container gets none either.

Every command that changes something:

* writes an entry to the audit log, with who (`--by`, by default your user name), what, when, and why (`--reason`). A change to a balance or an account is saved together with its entry, so neither exists without the other. A change kept in Redis is made only when the audit log can be reached, and says so if its entry could not be written afterwards. See [Audit log](#-audit-log).
* needs a reason when it is destructive or affects a user: cancelling a job or queueing it again, credits, deleting an account, and blocking a user. In a terminal, it asks for a missing one. A reason must not contain an email address, because reasons are kept after an account is deleted.
* shows what it will change and asks first when it changes a job or a balance: cancelling a job, queueing it again, and adding or taking away minutes. Deleting an account asks you to type its email address. `--yes` skips the question, for scripts. Without a terminal and without `--yes`, nothing is asked and nothing changes.
* shows what it would do with `--dry-run`, and changes nothing.
* names a job by at least 8 characters of its ID, as the job list shows them. Commands that only read take any unique start.
* prints the command that undoes it, when there is one.
* behaves like the matching action of a user or of the server: a cancelled job is charged like one the user cancels, and a job queued again is checked, reserved, and routed like a new one.

Every command prints JSON with `--json`, for scripts and monitoring. A change prints `{"dry_run": true, "plan": ...}` or `{"dry_run": false, "result": ...}`, and needs `--yes` where it would ask. No output contains secrets or purchase tokens.

When Redis or the billing database cannot be reached, a command says so in one line and exits with code 2; `--debug` before the command, as in `python scripts/admin.py --debug status`, shows the traceback.

## 📊 Status

```shell
python scripts/admin.py status
```

The first thing to look at when users say the server is slow:

* **Jobs:** places used out of `MAX_ACTIVE_JOBS`, jobs by status (being checked, uploading, queued, processing), and how many are assigned to the local and the remote backend.
* **Local worker:** its heartbeat, with how many jobs it runs at once and its average job time. With several local workers, their totals and then one line for each, named by container and process. No heartbeat means the worker is stopped or not consuming the local queue.
* **Waiting:** jobs queued but not running yet, per backend, and how long the oldest has waited. A wait that keeps growing means too few workers; see [Metrics for scaling](architecture.md#-metrics-for-scaling).
* **Video checks:** links being checked for their length right now, by every API replica together, out of `MAX_VIDEO_CHECKS`. When all places stay taken, users get `503` when they submit or price a link.
* **API leader:** the API replica that runs backups and the abandoned job check, by container. `none` means no API is running, or it lost Redis; see [Several API replicas](architecture.md#several-api-replicas).
* **Remote:** remote slots in use out of the limit, when outsourcing is on.
* **Queues:** messages in the local and remote queues that are not finished yet, including ones a worker has taken, and dead-lettered messages.
* **Disk:** space used by job files, and space free on the data volume. With Docker Desktop (Windows and macOS), the volume is in Docker's virtual disk, so the free space and total are that disk's limit, about 1 TB by default, not the host's drive: check the drive itself, where the virtual disk grows.
* **Backups:** how many billing backups are on this disk and the age of the newest; with off-site copies on, when the last copy succeeded, or why it failed.
* **Maintenance:** a pending restart or shutdown, from `scripts/server.py`.
* **Video sites:** for each site with something to report, whether its links are refused because it rate-limited this server or stopped answering, for how long and why, or whether they are read one at a time to recover; downloads running; and how many incidents there were. See [When a video site refuses the server](api.md#-when-a-video-site-refuses-the-server).
* **Controls:** active [limits](#-limits).

## 🧾 Jobs

```shell
python scripts/admin.py jobs --failed --since 1d
python scripts/admin.py jobs show 3f2a9c1e
```

`jobs` lists recent jobs from their [diagnostics records](architecture.md#-job-diagnostics), newest first, so jobs that ended long ago are included until `JOB_DIAGNOSTICS_RETENTION`. Filter with `--failed` or `--status`, `--user` (email address or user ID), `--backend local` or `remote`, and `--since` (a duration such as `2h` or `7d`). `--limit` sets how many are listed (default 30).

A job is named by its ID or its first characters, as the list shows them.

* **`jobs show <job>`** prints everything known about the job: its live fields in Redis while they exist, the diagnostics record with the full error that users never see, its billing reservation, and the files in its folder.
* **`jobs cancel <job>`** cancels a queued or processing job, for example one stuck after a crash. It is charged like a job the user cancels: nothing below 5% progress, then part of its cost. `--refund` charges nothing, for a job that is stuck through no fault of the user. A worker running the job stops at its next progress update. Jobs being checked or uploaded are not queued yet; their placeholder expires on its own.
* **`jobs requeue <job>`** queues a failed link job again, under the same job ID, so the user's app finds it. It is checked, reserved, and charged like a new submission, because the failure gave the minutes back, and it counts against the job limits. Routing chooses the backend, or `--local` and `--remote` choose it. It works while the failed job is still in Redis, an hour after it failed. Failed uploads cannot be queued again: the file is deleted when the job fails. An app that stopped polling after the failure shows the job again when it next asks for its status.

## 👤 Users

These commands need billing; without it there are no accounts.

```shell
python scripts/admin.py user user@example.com
python scripts/admin.py user user@example.com credit 30 --reason "refund for a failed job"
python scripts/admin.py user user@example.com credit -30 --reason "chargeback"
python scripts/admin.py user user@example.com delete --reason "deletion request by email"
python scripts/admin.py users find example.com
```

A user is named by email address or user ID. The email must belong to someone who has signed in at least once. If a name matches two accounts, which happens only when one account's ID is another's email address, the command stops and asks for the user ID.

* **`user <user>`** shows the balance, minutes reserved by running jobs, the last and next free-minute grant, a block, purchases with order IDs and whether they were voided, the latest balance changes, and recent jobs. A negative balance means a refunded purchase's minutes were already used; see [Refunds and chargebacks](google-play-billing.md#-refunds-and-chargebacks).
* **`credit <minutes>`** adds minutes, or takes them away with a negative number. It shows the balance before and after, and asks first. The balance can go below zero. The reason is kept with the balance change and in the audit log.
* **`delete`** deletes the account and its data, the same as `DELETE /api/account`, including running jobs and remaining minutes, and cannot be undone. It asks you to type the account's email address, or its user ID if it has none. See **Account deletion** in the [API guide](api.md#-account-deletion) for what is kept. Use it for deletion requests that arrive by email or a web form.
* **`users find <text>`** lists the accounts whose email address or user ID contains the text, at least 3 characters, for when you know only part of an address. `%` and `_` are matched as written.

## 💰 Billing

```shell
python scripts/admin.py billing --per month --since 365d
python scripts/admin.py billing --remote-price-per-hour 1.2
```

Whether the price per minute covers the GPU time:

* **Minutes** per day or month (`--per`): sold, given free, used by transcriptions, taken back for voided purchases, and changed by hand. `--since` sets how far back (default `30d`). Balance changes are recorded from the version that added this command on; earlier ones are not in the report, and it says so.
* **Negative balances:** how many users owe minutes, and the largest.
* **Voided purchases:** when the API last read them from Google Play.
* **Completed jobs and their run time** per backend. Run time is from start to finish, including the download, so it overstates the time a remote GPU is billed for. With `--remote-price-per-hour`, your provider's price per hour of the GPU you use, it estimates the remote cost. Job records are kept for `JOB_DIAGNOSTICS_RETENTION`, which limits how far back this goes.

Times are in UTC.

## 🚦 Limits

Temporary controls for abuse or a blocked video site. They apply to the next request, without editing `server.env` or restarting, and never stop running or queued jobs.

```shell
python scripts/admin.py limits
python scripts/admin.py limits pause --message "Back at 18:00 UTC" --for 2h
python scripts/admin.py limits resume
python scripts/admin.py limits links off --for 1d
python scripts/admin.py limits links on
python scripts/admin.py limits sites reset youtube.com
python scripts/admin.py limits block user@example.com --reason "abuse" --for 7d
python scripts/admin.py limits unblock user@example.com
python scripts/admin.py limits per-user 1 --for 6h
python scripts/admin.py limits per-user default
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
python scripts/admin.py audit
```

Lists the latest admin actions (`--limit`, default 30) with their time, who acted (`--by`), the action, its target (a job or user ID), details, and the reason. The log is the `admin_actions` table of the billing database, so the billing backups include it.

The [admin web page](#-admin-web-page) logs as `web:<email>`: `web.user_view` when an account is opened, `web.user_search` when accounts are searched (without the search text), and `web.lower` when its access is lowered. `web.restore` is logged when the command line raises it again.

## 🌐 Admin web page

Optional: a page that shows what `status`, `jobs`, `user`, `billing`, `limits`, and `audit` show, for running the server from anywhere: an overview that refreshes itself, jobs with their full errors, accounts, charts of minutes and jobs per day or month, limits, and the audit log. It runs in a service of its own, `admin`, reached through the [Cloudflare Tunnel](cloudflare-tunnel.md) on a hostname of its own, behind Cloudflare Access.

The charts are drawn from what the server already keeps (balance changes, and job records for `JOB_DIAGNOSTICS_RETENTION`) and from what is live now; the page collects and stores nothing of its own for them.

`ADMIN_WEB_MODE` sets what it may do, when the service starts:

* **`disabled`** (the default): the service does not start.
* **`read-only`**: the page shows everything above. It adds only two kinds of entries to the audit log: who opened which account, and who lowered the page's access.
* **`read-write`**: the page can also cancel a job, with or without a refund; add or take away minutes, at most 600 at once; pause and resume submissions; turn video links off and on; accept a video site again; block and unblock a user; and change the per-user limit. Each change goes through the same functions as the command line, so it is checked, charged, and written to the audit log the same way. Queueing a job again and deleting an account stay on the command line: the first downloads from a video site and writes job files, which the page's service may not, and the second cannot be undone.

### Making a change from the page

Every change takes two steps. First you give a reason (required to cancel, change a balance, or block) and the page shows what will happen, from the server: for example the balance before and after. Then you confirm within two minutes. The server keeps what you reviewed under a one-time confirmation, so the browser cannot change it; the change is made once, by the same administrator, and only if nothing changed meanwhile; otherwise you review it again. Each administrator can make at most 30 changes an hour from the page; more is done on the command line.

### How it is protected

* **Two locks.** Cloudflare Access signs you in before a request reaches the server. The page then checks the token Access adds to every request: Cloudflare's signature, your team, the Access application, its lifetime, and that the address is in `ADMIN_WEB_EMAILS`. Either lock alone keeps strangers out, so a mistake in Access does not open the page, and a program on the server's internal network cannot pretend to be Cloudflare.
* **Its own credentials, one pair per mode.** The service signs in to Redis and PostgreSQL as `admin-<mode>`, and gets only that mode's passwords. `admin-read-only` may read only the keys the page shows, and every table except purchase tokens, and add rows only to the audit log and to the page's own lowerings. `admin-read-write` may also write jobs, limits, and video sites' state in Redis, and change balances and reservations and record jobs in PostgreSQL; it can still not delete anything, create accounts, touch purchases, or write the queues. Before it serves anything, the service tries changes its mode must be refused, including read-write's changes in read-only mode, and stops if one is accepted. It gets no other secret, cannot write files, and publishes no port.
* **In the browser.** The page loads nothing from elsewhere, cannot be shown inside another site, is never cached, and shows text from users only as text. It accepts a change only as JSON from its own address.
* **Opening an account** is written to the audit log first; if that fails, nothing is shown.

### Setting it up

What Cloudflare must provide, with values from your configuration:

```shell
docker compose run --rm api python -m app.config requirements
```

1. **In Cloudflare**, as `requirements` lists under **Admin web page**: a second public hostname on the tunnel, such as `admin.example.com`, sent to `http://admin:8100`, and an Access application that protects it, allowing only your addresses, with a second factor. Look up where each is set in Cloudflare's current documentation.
2. **In `.env`**, which Docker Compose reads: `COMPOSE_PROFILES=cloudflare,admin`, which starts the page's service with the tunnel, and `ADMIN_WEB_MODE=read-only`, by which Compose signs it in with its roles. **In `server.env`**, with the application's AUD tag and your team domain from Cloudflare:

   ```env
   ADMIN_WEB_HOSTNAME=admin.example.com
   ADMIN_WEB_EMAILS=["you@example.com"]
   CLOUDFLARE_ACCESS_TEAM_DOMAIN=example.cloudflareaccess.com
   CLOUDFLARE_ACCESS_AUD=<the application's AUD tag>
   ```

3. **Start it and verify.** `start` and `restart` create the page's passwords in `secrets/redis` and `secrets/admin-web`:

   ```shell
   python scripts/server.py restart 5m
   docker compose run --rm api python -m app.config check --online
   ```

   `check --online` confirms that the hostname asks for the Access sign-in instead of showing the page, that your team's keys can be read, and that the service refuses a request without Access's token.

### Lowering its access from the page

The page can lower its own access, for every administrator at once: from `read-write` to `read-only` or `disabled`, or from `read-only` to `disabled`. Use it when a device that is signed in is lost, or to close the page while you are away. It is under **Access**, and asks twice: it shows what will happen, then you type the new access name exactly, such as `read-only`, and press the button. A reason is required. A lowered page stops making changes at once, also ones already reviewed.

Nothing on the page raises the access again. A disabled page shows only who disabled it, when, and how to restore it. Raise it on the server:

```shell
python scripts/admin.py web
python scripts/admin.py web restore --reason "found the phone"
```

`web` shows the mode in effect and every lowering; `restore` asks first and removes them, and the page has its configured access again from its next request. `status` shows the page's mode too.

To turn the page off at once on the server, stop the service with `docker compose stop admin`; set `ADMIN_WEB_MODE=disabled` and remove `admin` from `COMPOSE_PROFILES` to keep it off.

## 🧩 Using the commands from code

Each command is a data function in `app/admin/` that returns plain data, with the command line printing it. Another tool, such as an admin web page, calls the same functions, so it behaves exactly like the commands, including the audit log:

* Reading: `status.collect()`, `jobs.list_jobs()`, `jobs.job_details()`, `users.account()`, `users.find()`, `report.billing_report()`, `limits.current()`, and `audit.entries()`.
* Changing: `jobs.cancel()`, `jobs.requeue()`, `users.credit()`, `users.delete()`, and the functions in `limits`. Each takes the actor and a reason for the audit log. The reason is required by `jobs.cancel()`, `jobs.requeue()`, `users.credit()`, `users.delete()`, and `limits.block()`, and refused if it contains an email address.
* Confirming: `jobs.cancel_plan()`, `jobs.requeue_plan()`, `users.credit_plan()`, and `users.delete_plan()` return what the matching change would do and change nothing. Show it before asking.

A function that cannot do what was asked raises `AdminError` (in `app.admin.common`) with a message for the operator. Durations are in seconds. The functions need the billing database and Redis, so they run in the API container.

## 🗄️ What is recorded

Two tables of the billing database support these commands:

* **`minute_ledger`**: every change to a balance, with its kind (purchase, free grant, use, voided purchase, change by hand), the minutes, the time, and a note: the order ID, the job ID, or the reason given.
* **`admin_actions`**: the audit log.

When an account is deleted, its ledger entries are kept without the user, so totals stay right, like its purchases. Audit entries about it, including the deletion, name it by a hash of its ID instead of the ID. Reasons are kept as written, so do not put an email address in one.
