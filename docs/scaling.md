# 📈 Scaling beyond one machine

The server runs in one of two ways, from the same code:

| | One machine (default) | Several machines |
| --- | --- | --- |
| Runs on | Docker Compose, `scripts/server.py` | Kubernetes, `deploy/kubernetes` ([Kubernetes](kubernetes.md)) |
| API | one container | two or more replicas behind a Service; one leads the periodic tasks |
| GPU workers | the machine's GPUs | one GPU per pod, added and removed on the queue by KEDA |
| Job files | the `data` volume (`JOB_STORAGE=local`) | the object storage bucket (`JOB_STORAGE=s3`) |
| Redis, PostgreSQL | containers of the stack | managed services |
| Billing backups | the server's, every 15 minutes, optionally off-site | the database provider's (`DATABASE_BACKUPS=provider`) |
| Updates | `python scripts/server.py restart`, announced to clients | rolling updates, which interrupt nothing |

Clients see one difference: with `JOB_STORAGE=s3`, the MIDI download answers with a redirect to a signed link instead of the file. Apps must follow it without their `Authorization` header; see [Following a job](api.md#-following-a-job).

One machine is the right choice until it is not enough: a busy GPU that RunPod outsourcing cannot relieve affordably, or an outage you cannot accept while the machine restarts. `python scripts/admin.py billing` shows the minutes sold and used, against the fixed cost of managed services and a cluster.

## 🧩 What makes several machines work

Each part works on one machine too, so each can be turned on and checked before the move:

* **Several API replicas.** One replica leads the periodic tasks, such as backups and the abandoned job check, and another takes over within 30 seconds when it stops. Startup recovery runs only when no other replica runs. See [Several API replicas](architecture.md#several-api-replicas).
* **Several local workers.** Each publishes a heartbeat of its own, and routing adds up their capacity. See [Routing](architecture.md#routing).
* **Shared limits.** Video checks are counted across replicas (`MAX_VIDEO_CHECKS`), as video site downloads already were.
* **Job storage** in the bucket (`JOB_STORAGE=s3`): uploads, video information, and results, with a scratch folder per attempt. See [Job storage](architecture.md#job-storage).
* **Managed Redis and PostgreSQL**, over TLS. See [Managed Redis and PostgreSQL](architecture.md#managed-redis-and-postgresql).
* **Metrics** for the autoscaler: jobs waiting and running per queue, and the oldest job's wait, at `/metrics` with `metrics_token`. See [Metrics for scaling](architecture.md#-metrics-for-scaling).

`python -m app.config requirements` lists what each external service must provide for the settings in use: the sections Redis, PostgreSQL, and Storage cover several machines. `--json` gives the same for agents.

## 🚚 Moving a running server

Move during a quiet hour; users cannot submit jobs from step 4 until step 8. The order keeps every balance and purchase:

1. **Create the services**: a bucket for this server alone, a managed Redis, and a managed PostgreSQL. Check them against `requirements` as above.
2. **Try job storage on one machine.** Set `JOB_STORAGE=s3` with the bucket's settings in `server.env`, apply them during a quiet moment (`python scripts/server.py restart 5m`), and transcribe a link and an upload. The MIDI download now redirects to the bucket. Change it back later only when no job is queued.
3. **Prepare the cluster** as in [Kubernetes](kubernetes.md), steps 1 to 5, without applying yet. The database URL that goes into `secrets/database_url` there is the managed one; keep a copy of it in `secrets/managed_database_url` for step 6. That name is read by nothing else, so the Compose stack keeps its own database meanwhile.
4. **Stop new jobs**, and let the running ones finish:

   ```shell
   python scripts/admin.py limits pause --message "Moving to new servers; back within the hour"
   python scripts/admin.py status
   ```

   Repeat `status` until it shows no job queued or processing. Jobs left in Redis are not moved: their reserved minutes are given back.

5. **Stop the stack** and take a last backup:

   ```shell
   python scripts/server.py stop 1m --back-in 1h
   docker compose up -d --wait postgres
   docker compose run --rm --no-deps api python -m app.backups now
   ```

6. **Copy it into the managed database**, which must still be empty. The restore is one transaction, so on any error the managed database stays empty:

   ```shell
   docker compose run --rm --no-deps api python -m app.backups copy-to /run/secrets/managed_database_url
   docker compose stop postgres
   ```

7. **Start the cluster**: steps 6 and 7 of [Kubernetes](kubernetes.md). `status` there shows the users and balances of the old database; check a few with `python -m app.admin user <email>`.
8. **The tunnel**: the cluster's `cloudflared` connects with the same token, so the hostnames now lead to the cluster. Submissions are open: the pause was in the old Redis.

Keep the Compose stack's folders until the cluster has run for a while. To go back, pause and drain the cluster the same way, export the managed database with `pg_dump --format=custom` into `billing-data/backups` under a name such as `billing-<unix time>.dump`, and restore it with `python scripts/server.py restore --from local <that name>`, which starts the stack again.
