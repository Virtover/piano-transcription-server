# ☸️ Kubernetes

The Compose stack runs everything on one machine. For more than one machine, `deploy/kubernetes` runs the same images on Kubernetes:

* **API replicas**, two or more behind the Service `api`, added on CPU. Any replica answers any request; one leads the periodic tasks.
* **GPU workers**, one GPU each. KEDA adds them while jobs wait and removes them slowly afterwards; see [Metrics for scaling](architecture.md#-metrics-for-scaling).
* **One remote worker** for RunPod, **one cleanup**, **two `cloudflared` connectors**, and optionally the [admin web page](#-the-admin-web-page).
* **No Redis, PostgreSQL, or data volume of its own.** Redis and PostgreSQL are managed services, and job files are kept in the object storage bucket.

The manifests set `JOB_STORAGE=s3` and `BILLING_BACKUPS=provider` themselves, because several machines share no folder: see [Job storage](architecture.md#job-storage) and [Managed Redis and PostgreSQL](architecture.md#managed-redis-and-postgresql). `tests/test_kubernetes.py` keeps them in step with `docker-compose.yml`: the same commands, wiring, secret paths, user, and hardening.

## 📋 What you need

* A Kubernetes cluster with GPU nodes that offer `nvidia.com/gpu`, through NVIDIA's device plugin or GPU operator.
* KEDA, with a `metrics-api` scaler that reads the Prometheus text format.
* A container registry the cluster can pull from.
* A managed Redis, a managed PostgreSQL, and the object storage bucket. What each must provide comes from the server: put `REDIS_URL` and the database URL in place as described below, then run

  ```shell
  docker compose run --rm -e REDIS_URL=<the managed Redis URL> api python -m app.config requirements
  ```

  and see the sections Redis, PostgreSQL, and Storage.

## 🚀 First deployment

The commands run from the repository's folder, with `kubectl` signed in to the cluster.

1. **Build and push the images.** Use a new tag for every version:

   ```shell
   docker build -f Dockerfile.api -t <registry>/piano-transcription-api:<tag> .
   docker build -f Dockerfile.worker -t <registry>/piano-transcription-worker:<tag> .
   docker push <registry>/piano-transcription-api:<tag>
   docker push <registry>/piano-transcription-worker:<tag>
   ```

   Then put the names and tag under `images` in `deploy/kubernetes/kustomization.yaml`.

2. **Settings.** Copy `.env` to `deploy/kubernetes/settings.env`, which git ignores. Remove `COMPOSE_PROFILES`, and add the managed Redis's address, with the services' user:

   ```env
   REDIS_URL=rediss://app@<host>:<port>/0
   ```

   If the provider's own CA signs its certificate, put that CA's file in `secrets/redis_ca.crt` and add `REDIS_CA_FILE=/run/secrets/redis_ca.crt`. Leave out `JOB_STORAGE` and `BILLING_BACKUPS`: the manifests set them.

3. **The database URL**, with its password and TLS options, goes in `secrets/billing_database_url`, for example `postgresql://billing:<password>@<host>:5432/billing?sslmode=verify-full&sslrootcert=/run/secrets/postgres_ca.crt`, with the provider's CA file in `secrets/postgres_ca.crt`. Write it with the commands in [Secrets](configuration.md#-secrets), so it is never in a command's history.

4. **The secrets**, as Kubernetes Secrets. First the namespace, then the secret files the API and workers read, each with its own `--from-file`; leave out the ones you do not use:

   ```shell
   kubectl create namespace piano-transcription
   kubectl -n piano-transcription create secret generic piano-secrets --from-file=secrets/billing_database_url --from-file=secrets/storage_access_key --from-file=secrets/storage_secret_key --from-file=secrets/metrics_token --from-file=secrets/google_play_service_account.json --from-file=secrets/runpod_api_key --from-file=secrets/postgres_ca.crt
   ```

   The Redis password of the services' user, under the key `app`, and the tunnel token, each in a Secret of its own so that only their pods see them:

   ```shell
   kubectl -n piano-transcription create secret generic piano-redis --from-file=app=<file with the services' Redis password>
   kubectl -n piano-transcription create secret generic piano-cloudflare --from-file=tunnel_token=secrets/cloudflare/tunnel_token
   ```

   `metrics_token` is needed: KEDA reads the queue with it. Create one with the commands in [Secrets](configuration.md#-secrets).

5. **GPU nodes.** In `deploy/kubernetes/worker.yaml`, uncomment `nodeSelector` and `tolerations` and give them your cluster's GPU node label and taint. Set the first trigger's `targetValue` to the jobs one worker runs at once, which the worker's log shows at startup (`kubectl -n piano-transcription logs deploy/worker`), and `maxReplicaCount` to the most GPUs you want to pay for.

6. **Apply** and wait for the API:

   ```shell
   kubectl apply -k deploy/kubernetes
   kubectl -n piano-transcription rollout status deploy/api
   ```

7. **Check** from inside the cluster:

   ```shell
   kubectl -n piano-transcription exec deploy/api -- python -m app.config check --online
   kubectl -n piano-transcription exec deploy/api -- python -m app.admin status
   ```

8. **The tunnel** keeps its public hostnames: the Service `api` answers at `http://api:8000`, as in Compose. While the Compose stack still runs with the same tunnel token, Cloudflare sends requests to both; stop it with `python scripts/server.py stop` once the cluster serves. The two stacks use different databases, so move billing first; see [Moving the billing database](configuration.md#moving-the-billing-database).

## 🔄 Updates

Build and push the new version under a new tag, change the tag in `kustomization.yaml`, and apply again. Kubernetes replaces the pods one at a time:

* An API replica that stops hands the lead to another at once; the new replicas join without recovering anything.
* A worker that stops takes no new job and gets 15 minutes to finish the ones it runs (`terminationGracePeriodSeconds` in `worker.yaml`). A job still running then fails and is refunded.

`scripts/server.py`, which announces a restart to clients and drains the stack, is for Compose; a rolling update interrupts nothing, so it needs no notice. To change settings, edit `settings.env` and apply: the settings' ConfigMap gets a new name, which restarts the pods that use it.

## 🛠️ Administration

The administration commands run in an API pod instead of through `scripts/admin.py`:

```shell
kubectl -n piano-transcription exec -it deploy/api -- python -m app.admin status
kubectl -n piano-transcription exec -it deploy/api -- python -m app.admin jobs --failed
```

## 🔐 The admin web page

Uncomment `components` in `kustomization.yaml`. The page signs in to Redis and PostgreSQL with roles of its own, as in Compose: create at the providers the Redis user `admin-read-only` (or `admin-read-write`) with the rules of `redis/users.acl`, and the PostgreSQL role that the API creates for `ADMIN_WEB_MODE`. Then their Secret:

```shell
kubectl -n piano-transcription create secret generic piano-admin-web --from-literal=redis_url=rediss://admin-read-only@<host>:<port>/0 --from-file=redis_password=<file with its Redis password> --from-file=billing_database_url=<file with its database URL>
```

The tunnel sends the page's hostname to `http://admin:8100`, behind Cloudflare Access, as described in [Administration](administration.md#-admin-web-page).
