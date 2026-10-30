# ☸️ Kubernetes

The Compose stack runs everything on one machine. For more than one machine, `deploy/kubernetes` runs the same images on Kubernetes. [Scaling beyond one machine](scaling.md) compares the two and moves a running server over.

> [!WARNING]
> **Not yet run on real infrastructure.** The Kubernetes deployment has been tested only on the test cluster on this PC (`python scripts/kube.py local up`) and by `tests/test_kubernetes.py`. A real cluster with managed Redis, PostgreSQL, and a bucket, GPU nodes, and KEDA has not served users yet. Docker Compose is the supported way to run the server; try a real cluster on a copy first, with its own bucket and database, before moving users to it.

* **API replicas**, two or more behind the Service `api`, added on CPU. Any replica answers any request; one leads the periodic tasks.
* **GPU workers**, one GPU each. KEDA adds them while jobs wait and removes them slowly afterwards; see [Metrics for scaling](architecture.md#-metrics-for-scaling).
* **One remote worker** for RunPod, **one cleanup**, **two `cloudflared` connectors**, and optionally the [admin web page](#-the-admin-web-page).
* **No Redis, PostgreSQL, or data volume of its own.** Redis and PostgreSQL are managed services, and job files are kept in the object storage bucket.

The manifests set `JOB_STORAGE=s3` and `DATABASE_BACKUPS=provider` themselves, because several machines share no folder: see [Job storage](architecture.md#job-storage) and [Managed Redis and PostgreSQL](architecture.md#managed-redis-and-postgresql). `tests/test_kubernetes.py` keeps them in step with `docker-compose.yml`: the same commands, wiring, secret paths, user, and hardening.

| Folder | What |
| --- | --- |
| `deploy/kubernetes/base` | What every Kubernetes deployment runs |
| `deploy/kubernetes` | A real cluster: its namespace, `settings.env`, and images, on top of `base` |
| `deploy/kubernetes/local` | A test cluster on this PC: `base`, with Redis, PostgreSQL, and S3-compatible storage inside it |

## 🧰 `scripts/kube.py`

```shell
python scripts/kube.py local up       # a test cluster on this PC, everything inside it
python scripts/kube.py up             # a real cluster: managed Redis, PostgreSQL, bucket
python scripts/kube.py status         # pods, KEDA, queue, leader, problems
python scripts/kube.py update         # new images, rolling update
python scripts/kube.py restart 5m     # announced to clients, as scripts/server.py
python scripts/kube.py admin jobs     # the administration commands, as scripts/admin.py
python scripts/kube.py down
```

`local` before a command acts on the test cluster instead of the real one. Also `check` (`check --online` inside the cluster), `logs <deployment>`, and `build <tag>` (the images alone).

`restart`, `stop`, `start`, `restore`, and `cancel` are the commands of `scripts/server.py`, with the same options, and `admin` runs those of `scripts/admin.py`: the same code, which reaches the cluster with kubectl instead of Docker Compose. See [Restarts and administration](#-restarts-and-administration).

* **`up` and `update` do every step**: `settings.env` from `server.env`; the Secrets, from the files in `secrets`, created or updated; the images, built and, for a real cluster, pushed under a new tag; `kubectl apply`, waiting for every Deployment; and `check --online` in the cluster. They ask once, naming the kubectl context, before they change the cluster or push an image.
* **Secrets are never shown.** Each is made from its files with `kubectl --dry-run` and applied from memory. Files only a real cluster uses, such as the managed database's URL, are in `secrets/kubernetes`, so the Compose stack on this machine never reads them; `up` asks for a missing one without showing it, and generates `metrics_token`.
* **`down`** deletes the namespace, with every pod and Secret. For a real cluster you type its name; the managed Redis, PostgreSQL, and bucket are not touched.
* **`status`** shows every Deployment's ready pods, KEDA's workers, the queue and its oldest wait, the leading API, the Secrets by name, and every problem the API reports, including features Kubernetes needs that are off. `python scripts/setup.py status` shows, for each feature, whether Kubernetes needs it.

## 🧪 A test cluster on this PC

`python scripts/kube.py local up` runs the server on Docker Desktop's Kubernetes (turn it on in Docker Desktop's settings; kubectl's context is then `docker-desktop`), as a real cluster runs it, with what a cluster gets from providers inside it instead:

* **Redis, PostgreSQL, and SeaweedFS** for the bucket, with random passwords in `secrets/kubernetes-local`, kept from one `up` to the next. Redis has the users and rules of `redis/users.acl`.
* **The images built here**, which Docker Desktop's Kubernetes uses without a registry.
* **Two API replicas**, on `http://localhost:8080`, beside a Compose stack on `8000`.
* **CPU workers, added by KEDA** as the queue grows, one job each and up to 3, as GPU workers on a real cluster; `local up` installs KEDA when it is missing.
* **No tunnel, billing, outsourcing, or error reports:** `settings.env` is `server.env` with those off, and the services above. Storage is at `host.docker.internal:8333`, so signed links to results work from the pods and from a browser.

`python scripts/kube.py local down` removes it, with its database; KEDA stays installed. To see the scaling, submit a few uploads and watch `python scripts/kube.py local status`.

## 📋 What you need

* A Kubernetes cluster with GPU nodes that offer `nvidia.com/gpu`, through NVIDIA's device plugin or GPU operator.
* KEDA, with a `metrics-api` scaler that reads the Prometheus text format.
* A container registry the cluster can pull from.
* A managed Redis, a managed PostgreSQL, and the object storage bucket. What each must provide comes from the server: put `REDIS_URL` and the database URL in place as described below, then run

  ```shell
  docker compose run --rm -e REDIS_URL=<the managed Redis URL> api python -m app.config requirements
  ```

  and see the sections Redis, PostgreSQL, and Storage.

## 🚀 First deployment by hand

`python scripts/kube.py up` does this; by hand, the commands run from the repository's folder, with `kubectl` signed in to the cluster.

1. **Build and push the images.** Use a new tag for every version:

   ```shell
   docker build -f Dockerfile.api -t <registry>/piano-transcription-api:<tag> .
   docker build -f Dockerfile.worker -t <registry>/piano-transcription-worker:<tag> .
   docker push <registry>/piano-transcription-api:<tag>
   docker push <registry>/piano-transcription-worker:<tag>
   ```

   Then put the names and tag under `images` in `deploy/kubernetes/kustomization.yaml`.

2. **Settings.** Copy `server.env` to `deploy/kubernetes/settings.env`, which git ignores; it holds the server's settings and nothing of Docker Compose's. Add the managed Redis's address, with the services' user:

   ```env
   REDIS_URL=rediss://app@<host>:<port>/0
   ```

   If the provider's own CA signs its certificate, put that CA's file in `secrets/redis_ca.crt` and add `REDIS_CA_FILE=/run/secrets/redis_ca.crt`. Leave out `JOB_STORAGE` and `DATABASE_BACKUPS`: the manifests set them.

3. **The database URL**, with its password and TLS options, goes in `secrets/database_url`, for example `postgresql://billing:<password>@<host>:5432/billing?sslmode=verify-full&sslrootcert=/run/secrets/postgres_ca.crt`, with the provider's CA file in `secrets/postgres_ca.crt`. Write it with the commands in [Secrets](configuration.md#-secrets), so it is never in a command's history.

4. **The secrets**, as Kubernetes Secrets. First the namespace, then the secret files the API and workers read, each with its own `--from-file`; leave out the ones you do not use:

   ```shell
   kubectl create namespace piano-transcription
   kubectl -n piano-transcription create secret generic piano-secrets --from-file=secrets/database_url --from-file=secrets/storage_access_key --from-file=secrets/storage_secret_key --from-file=secrets/metrics_token --from-file=secrets/google_play_service_account.json --from-file=secrets/runpod_api_key --from-file=secrets/postgres_ca.crt
   ```

   The Redis password of the services' user, under the key `app`, and the tunnel token, each in a Secret of its own so that only their pods see them:

   ```shell
   kubectl -n piano-transcription create secret generic piano-redis --from-file=app=<file with the services' Redis password>
   kubectl -n piano-transcription create secret generic piano-cloudflare --from-file=tunnel_token=secrets/cloudflare/tunnel_token
   ```

   `metrics_token` is needed: KEDA reads the queue with it. Create one with the commands in [Secrets](configuration.md#-secrets).

5. **GPU nodes.** In `deploy/kubernetes/base/worker.yaml`, uncomment `nodeSelector` and `tolerations` and give them your cluster's GPU node label and taint. Set the first trigger's `targetValue` to the jobs one worker runs at once, which the worker's log shows at startup (`kubectl -n piano-transcription logs deploy/worker`), and `maxReplicaCount` to the most GPUs you want to pay for.

6. **Apply** and wait for the API:

   ```shell
   kubectl apply -k deploy/kubernetes
   kubectl -n piano-transcription rollout status deploy/api
   ```

7. **Check** from inside the cluster:

   ```shell
   python scripts/kube.py check
   python scripts/kube.py admin status
   ```

8. **The tunnel** keeps its public hostnames: the Service `api` answers at `http://api:8000`, as in Compose. While the Compose stack still runs with the same tunnel token, Cloudflare sends requests to both; stop it with `python scripts/server.py stop` once the cluster serves. The two stacks use different databases, so move billing first; see [Moving the billing database](configuration.md#moving-the-billing-database).

## 🔄 Updates

Build and push the new version under a new tag, change the tag in `kustomization.yaml`, and apply again. Kubernetes replaces the pods one at a time:

* An API replica that stops hands the lead to another at once; the new replicas join without recovering anything.
* A worker that stops takes no new job and gets 15 minutes to finish the ones it runs (`terminationGracePeriodSeconds` in `base/worker.yaml`). A job still running then fails and is refunded.

A rolling update interrupts nothing, so it needs no notice. To change settings, edit `settings.env` and apply: the settings' ConfigMap gets a new name, which restarts the pods that use it.

## 🛠️ Restarts and administration

`scripts/kube.py` runs the commands of `scripts/server.py` and `scripts/admin.py` on the cluster; `local` first for the test cluster:

```shell
python scripts/kube.py restart 5m --reason "update"   # announce, build and push, drain, rolling restart
python scripts/kube.py stop 10m --back-in 2h          # announce, drain, scale the server to none
python scripts/kube.py start                          # apply again, wait until ready
python scripts/kube.py cancel
python scripts/kube.py admin status
python scripts/kube.py admin jobs --failed --since 1d
```

They differ from Compose where a cluster does:

* **`restart`, `stop`, and `start` ask once**, naming the kubectl context, before they announce anything. `restart` builds and pushes the images under a new tag while the countdown runs, then applies the manifests and restarts the server's Deployments one pod at a time.
* **`stop`** pauses KEDA and scales the server's Deployments to none; `start` applies the manifests, which brings them back, and resumes KEDA. On the test cluster, its Redis, PostgreSQL, and storage keep running.
* **Redis is not saved or reloaded:** a real cluster's is managed, and the test cluster's keeps nothing past a restart of its own.
* **`restore` refuses:** the database's provider keeps its backups; restore with the provider's own restore.
* `--profile` and `--legacy-gpu` are Compose's and change nothing here; `status` is `scripts/kube.py`'s own, which also shows a planned restart or shutdown.

## 🔐 The admin web page

Uncomment `components` in `kustomization.yaml`, and add `ADMIN_WEB_MODE=read-only` (or `read-write`) to `settings.env`: with Compose it is in `.env`, which a copy of `server.env` does not have. The page signs in to Redis and PostgreSQL with roles of its own, as in Compose: create at the providers the Redis user `admin-read-only` (or `admin-read-write`) with the rules of `redis/users.acl`, and the PostgreSQL role that the API creates for `ADMIN_WEB_MODE`. Then their Secret:

```shell
kubectl -n piano-transcription create secret generic piano-admin-web --from-literal=redis_url=rediss://admin-read-only@<host>:<port>/0 --from-file=redis_password=<file with its Redis password> --from-file=database_url=<file with its database URL>
```

The tunnel sends the page's hostname to `http://admin:8100`, behind Cloudflare Access, as described in [Administration](administration.md#-admin-web-page).
