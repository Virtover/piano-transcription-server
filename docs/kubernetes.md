# ☸️ Kubernetes

The Compose stack runs everything on one machine. For more than one machine, `deploy/kubernetes` runs the same images on Kubernetes. [Scaling beyond one machine](scaling.md) compares the two and moves a running server over.

> [!WARNING]
> **Not yet run on real infrastructure.** The Kubernetes deployment has been tested only on the test cluster on this PC (`python scripts/kube.py local up`) and by `tests/test_kubernetes.py`. A real cluster with managed Redis, PostgreSQL, and a bucket, GPU nodes, and KEDA has not served users yet. Docker Compose is the supported way to run the server; try a real cluster on a copy first, with its own bucket and database, before moving users to it.

* **API replicas**, two or more behind the Service `api`, added on CPU. Any replica answers any request; one leads the periodic tasks.
* **GPU workers**, one GPU each. KEDA adds them while jobs wait and removes them slowly afterwards, as many as the jobs need at what each worker reports it takes, so GPUs of different sizes can be mixed; see [Metrics for scaling](architecture.md#-metrics-for-scaling).
* **One remote worker** for RunPod, **one cleanup**, **two `cloudflared` connectors**, and optionally the [admin web page](#-the-admin-web-page).
* **No Redis, PostgreSQL, or data volume of its own.** Redis and PostgreSQL are managed services, and job files are kept in the object storage bucket.

The manifests set `JOB_STORAGE=s3` and `DATABASE_BACKUPS=provider` themselves, because several machines share no folder: see [Job storage](architecture.md#job-storage) and [Managed Redis and PostgreSQL](architecture.md#managed-redis-and-postgresql). `tests/test_kubernetes.py` keeps them in step with `docker-compose.yml`: the same commands, wiring, secret paths, user, and hardening. No pod gets a token for the Kubernetes API, which none of them uses, so a worker that a malicious file takes over cannot act on the cluster. [Network rules](#-network-rules-and-spreading) let each pod be reached only by what uses it, and keep replicas on different machines.

| Path | What |
| --- | --- |
| `deploy/kubernetes/base` | What every Kubernetes deployment runs |
| `deploy/kubernetes` | A real cluster: its namespace, `settings.env`, and images, on top of `base` |
| `deploy/kubernetes/cluster.env` | The real cluster's own choices: the registry, the GPU nodes, the workers' scaling, the admin web page. See [Cluster choices](#-cluster-choices). Not committed |
| `deploy/kubernetes/cluster` | The component `scripts/kube.py` writes from `cluster.env`. Not committed |
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

`local` before a command acts on the test cluster instead of the real one. Also `check` (`check --online` inside the cluster), `logs <deployment>`, `build <tag>` (the images alone), and `config`, which writes `deploy/kubernetes/cluster` from `cluster.env` without kubectl, for `kubectl apply -k` by hand.

`restart`, `stop`, `start`, `restore`, and `cancel` are the commands of `scripts/server.py`, with the same options, and `admin` runs those of `scripts/admin.py`: the same code, which reaches the cluster with kubectl instead of Docker Compose. See [Restarts and administration](#-restarts-and-administration).

* **`up` and `update` do every step**: `settings.env` from `server.env`; the component from `cluster.env`, which the first `up` makes from `cluster.env.example`; the Secrets, from the files in `secrets`, created or updated; the images, built and, for a real cluster, pushed under a new tag; `kubectl apply`, waiting for every Deployment; and `check --online` in the cluster. They ask once, naming the kubectl context, before they change the cluster or push an image.
* **Secrets are never shown.** Each is made from its files with `kubectl --dry-run` and applied from memory. Files only a real cluster uses, such as the managed database's URL, are in `secrets/kubernetes`, so the Compose stack on this machine never reads them; `up` asks for a missing one without showing it, and generates `metrics_token`.
* **A cluster set up by hand** works with it too. A key a Secret already has in the cluster needs no file here: `up`, `update`, and `restart` keep it as it is, and replace only the keys they have a file for. To remove a key, use `kubectl edit secret`. `images` in `kustomization.yaml` may be written in any order, also with a `digest`, which `update` replaces with the new tag.
* **`down`** deletes the namespace, with every pod and Secret. For a real cluster you type its name; the managed Redis, PostgreSQL, and bucket are not touched.
* **`status`** shows every Deployment's ready pods, KEDA's workers, the queue and its oldest wait, the leading API, the Secrets by name, and every problem the API reports, including features Kubernetes needs that are off. `python scripts/setup.py status` shows, for each feature, whether Kubernetes needs it.

## 🎛️ Cluster choices

What differs from one cluster to another, and is not the server's, is in `deploy/kubernetes/cluster.env`; the server's settings stay in `server.env`, the same everywhere. The first `python scripts/kube.py up` makes it from `cluster.env.example`, and `up`, `update`, `start`, `restart`, and `config` write the component in `deploy/kubernetes/cluster` from it, so no manifest is edited by hand. A choice that is not valid stops them before anything is built or changed, naming it. An unset choice keeps the value below, which is `base`'s.

| Choice | Unset | What |
| --- | --- | --- |
| `KUBE_REGISTRY` | asked for | Where the images are pushed and the cluster pulls them from, such as `docker.io/<user>` or `ghcr.io/<user>`. Asked for once and written here; a registry already set by hand in `kustomization.yaml` moves here. |
| `KUBE_GPU_NODE_LABEL` | none | The GPU nodes' label as `name=value`, such as `nvidia.com/gpu.present=true`: the GPU workers run only there. Unset, on any node that offers `nvidia.com/gpu`. |
| `KUBE_GPU_NODE_TAINT` | none | The key of the taint that keeps other pods off the GPU nodes, such as `nvidia.com/gpu`, which the GPU workers tolerate with any effect. |
| `KUBE_MIN_WORKERS`, `KUBE_MAX_WORKERS` | `1`, `4` | The fewest and most GPU workers KEDA runs, one GPU each. The most is what you are willing to pay for, or the GPUs the cluster has; workers beyond the free GPUs wait unscheduled and take no jobs. |
| `KUBE_MAX_WAIT` | `60s` | One worker more while the oldest job has waited longer than this. |
| `KUBE_KEDA_NAMESPACE` | `keda` | The namespace KEDA runs in, whose pods alone may read `/metrics` besides the tunnel. |
| `KUBE_ADMIN_WEB_MODE` | `disabled` | The [admin web page](#-the-admin-web-page): `disabled`, `read-only`, or `read-write`. It deploys the page and sets `ADMIN_WEB_MODE` in `settings.env`. |
| `KUBE_PRIVATE_EGRESS` | none | Private addresses the API and workers may reach, separated by commas, such as `10.0.4.0/24` for managed services in the cluster's private network, or `192.168.1.20/32` for a bucket on the local network. Every other private and link-local address is closed to them; see [Network rules](#-network-rules-and-spreading). |

The tag of the images changes with every build, so `scripts/kube.py` writes it, with the registry, under `images` in `kustomization.yaml`.

## 🧪 A test cluster on this PC

`python scripts/kube.py local up` runs the server on Docker Desktop's Kubernetes (turn it on in Docker Desktop's settings; kubectl's context is then `docker-desktop`), as a real cluster runs it, with what a cluster gets from providers inside it instead:

* **Redis, PostgreSQL, and SeaweedFS** for the bucket, with random passwords in `secrets/kubernetes-local`, kept from one `up` to the next. Redis has the users and rules of `redis/users.acl`.
* **The images built here**, which Docker Desktop's Kubernetes uses without a registry when it is set up with kubeadm; with kind, its node has images of its own and the pods cannot start. They keep the tag `local`: a rebuild replaces the last one, and `local up` and `update` restart the pods to use it.
* **Two API replicas**, on `http://localhost:8080`, beside a Compose stack on `8000`.
* **CPU workers, added by KEDA** as the queue grows, up to 3, each taking the jobs its CPU and memory allow, as GPU workers on a real cluster; `local up` installs KEDA when it is missing.
* **Its own choices** in `local/kustomization.yaml`, without `cluster.env` or the admin web page.
* **No tunnel, billing, outsourcing, or error reports:** `settings.env` is `server.env` with those off, and the services above. Storage is at `host.docker.internal:8333`, so signed links to results work from the pods and from a browser.

`python scripts/kube.py local down` removes it, with its database; KEDA stays installed. To see the scaling, submit a few uploads and watch `python scripts/kube.py local status`.

## 📋 What you need

* A Kubernetes cluster, version 1.27 or newer for the spreading rules, with GPU nodes that offer `nvidia.com/gpu`, through NVIDIA's device plugin or GPU operator.
* A network plugin that enforces NetworkPolicies, such as Calico or Cilium, for the [network rules](#-network-rules-and-spreading). Without one the server runs, without them.
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

   Then put the names and tag under `images` in `deploy/kubernetes/kustomization.yaml`, and the registry in `KUBE_REGISTRY` in `deploy/kubernetes/cluster.env` (copy it from `cluster.env.example`), so that `scripts/kube.py` pushes there later.

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

5. **The cluster's choices.** In `deploy/kubernetes/cluster.env`, set `KUBE_GPU_NODE_LABEL` and `KUBE_GPU_NODE_TAINT` to your cluster's GPU node label and taint, `KUBE_MAX_WORKERS` to the most GPUs you want to pay for, and `KUBE_KEDA_NAMESPACE` if KEDA runs elsewhere; see [Cluster choices](#-cluster-choices). The jobs one worker runs at once need no setting: each worker reports them. Then write the component:

   ```shell
   python scripts/kube.py config
   ```

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

A rolling update interrupts nothing, so it needs no notice. To change settings, edit `settings.env` and apply: the settings' ConfigMap gets a new name, which restarts the pods that use it. To change a choice, edit `cluster.env` and run `python scripts/kube.py update`, or `config` before applying by hand.

## 🛡️ Network rules and spreading

`base/network-policy.yaml` lets each pod be reached only by what uses it. The rules need a network plugin that enforces them; elsewhere they change nothing.

* **Nothing reaches a pod without a rule.** The API answers `cloudflared` and KEDA's namespace (`KUBE_KEDA_NAMESPACE`), on port 8000; the admin web page answers only `cloudflared`, on 8100. A Prometheus of your own that scrapes `/metrics` needs a rule like KEDA's.
* **The API and the workers**, which fetch video links and decode untrusted media, reach the internet and this namespace, but no private or link-local address: not the machines of the network around the cluster, such as a home router or NAS, not other namespaces' pods, and not the address where clouds answer with the machine's credentials. `app/url_safety.py` refuses those addresses too; this rule still holds if a decoder is taken over.
* **Private services the server uses**, such as a managed Redis reached over the provider's private network, or a bucket or database on the local network, need their addresses in `KUBE_PRIVATE_EGRESS`. Without them, `check --online`, which `up` runs, reports the service unreachable.
* **Names** resolve only through the cluster's DNS pods (`k8s-app: kube-dns`, as CoreDNS is labelled on most clusters) or a node-local DNS cache on `169.254.20.10`.
* **The test cluster** adds rules of its own for its Redis, PostgreSQL, and storage, and for this PC; Docker Desktop's Kubernetes enforces none of them.

Replicas of the API and `cloudflared` are kept on different machines (`topologySpreadConstraints`), so one machine going away leaves the others serving, and spread across zones where the cluster has them. A cluster with one node still runs them both there; nodes whose taint keeps them off do not count. GPU workers prefer different machines, but never wait for one.

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

Set `KUBE_ADMIN_WEB_MODE=read-only` (or `read-write`) in `cluster.env`: it deploys `components/admin-web` and sets `ADMIN_WEB_MODE` in `settings.env`, which with Compose is in `.env`, so a copy of `server.env` does not have it. The page signs in to Redis and PostgreSQL with roles of its own, as in Compose: create at the providers the Redis user `admin-read-only` (or `admin-read-write`) with the rules of `redis/users.acl`, and the PostgreSQL role that the API creates for `ADMIN_WEB_MODE`. Then their Secret:

```shell
kubectl -n piano-transcription create secret generic piano-admin-web --from-literal=redis_url=rediss://admin-read-only@<host>:<port>/0 --from-file=redis_password=<file with its Redis password> --from-file=database_url=<file with its database URL>
```

The tunnel sends the page's hostname to `http://admin:8100`, behind Cloudflare Access, as described in [Administration](administration.md#-admin-web-page).
