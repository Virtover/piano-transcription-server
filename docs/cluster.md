# 🏠 Home cluster

Your other devices can run work for the server: Linux or Windows, with an NVIDIA GPU or without, always on or only now and then. One machine is the **hub** and runs the server as always, with Docker Compose. Every other device is a **node** and runs only a worker, which reaches the hub over [Tailscale](https://tailscale.com).

| | Hub | Node |
| --- | --- | --- |
| Runs | the whole stack: API, Redis, PostgreSQL, tunnel, workers | a worker: transcriptions, arrangements, RunPod dispatch, or link checks |
| Files | `.env`, `server.env`, `secrets/`, `billing-data/` | `node.env` and `secrets/node/`, which `join` writes |
| Commands | `scripts/server.py`, `scripts/admin.py` as always | `scripts/server.py join`, `start`, `stop`, `restart`, `update`, `status` |
| Must be | always on | on when you like: jobs move to other devices when it goes |

One rule: **`server.py` runs this machine, `admin.py` runs the cluster.** A node needs no copy of `server.env`: it takes the server's settings from the hub, and its `node.env` holds only what is its own.

For a cluster of cloud machines instead, see [Kubernetes](kubernetes.md).

## 🧭 How it works

* **Nodes reach only Redis, and the bucket.** Each node signs in to the hub's Redis as `node-<name>`, with its own password and rules: it may run jobs and write its own status, and cannot change limits, maintenance notices, other devices' switches, its own included, or Redis itself. It never reaches PostgreSQL: when it ends a job it only names it, and the hub settles the billing by the job's state, as after a crash. A node taken over by a malicious file can therefore not touch balances or purchases.
* **Settings come from the hub.** The hub's API publishes the server's settings; a node starts with them, and starts again when they change, after its running jobs finish. What depends on the device (`WORKER_THREADS`, `WORKER_ENFORCE_CPU`, `WORKER_GPU_RESERVE_GIB`, `WORKER_CPU_RESERVE_GIB`, `WORKER_BATCH_SIZE`, `WORKER_MAX_CONCURRENCY`) is never taken; set it in the node's `node.env`, which also overrides any other setting for that device.
* **Switches, per device and task.** Every device, the hub included, has a switch and a list of allowed tasks: `transcribe`, `arrange`, `dispatch` (sending jobs to RunPod), and `check` (reading a video's length for the hub while a site refuses the hub's address). The worker reads them every 10 seconds. A task switched off finishes its running jobs first.
* **A device that goes away loses no job.** A worker renews a lease on each job it runs. When a device loses power or its network, the lease expires within a minute, and the hub queues the job again, keeping its reservation, preferably on another device, up to `JOB_MAX_ATTEMPTS` (3) times; then it fails and is refunded. Each attempt has a token: a device that comes back later finds the job taken, and writes, charges, and deletes nothing.
* **Jobs go where there is room.** Each worker reports how busy its GPU is, by any program, and how much VRAM is free. A device at `WORKER_BUSY_UTILIZATION` (90%) or more, or without the VRAM one more job needs, leaves a new job to a less busy device with a free place and room; a GPU device only to another GPU device. When all are busy, the job runs where it landed.
* **Rate limits per address.** Video sites limit addresses, not devices. Each device learns its public address, and every video site's rate limit, pacing, and download places are kept per address. A job a site refuses moves to a device that reaches it from another address; the API asks such a device to check a link while the site refuses the hub. Only when every address is refused does the job fail, with "try again later" and no charge. Devices behind one router share one address, so this helps only with devices elsewhere: a laptop at work, a phone's hotspot, a VPN. `NODE_EGRESS` names a device's address by hand.

## 📋 What you need

* **Tailscale** on every device, signed in to one tailnet: it gives each device an address only your devices reach, and encrypts everything between them.
* **Docker** on every device: Docker Desktop on Windows and macOS, Docker Engine on Linux. With an NVIDIA GPU, its driver, and on Linux the NVIDIA Container Toolkit.
* **This repository** on every device, at the same version as the hub's.
* **Nothing else for job files:** with `JOB_STORAGE=local`, the default, the hub keeps them in a bucket of its own, which its nodes reach over the tailnet, each with keys of its own. With `JOB_STORAGE=s3` they stay in your provider's bucket, such as Cloudflare R2, whose keys every node then gets.

`python scripts/setup.py cluster` checks all of this on the machine it runs on, the hub or a node, says what is missing and how to fix it, and offers to make a machine with Tailscale the hub. `python scripts/setup.py status` shows the same in short.

The hub should be the most reliable machine, always on. **Linux is the better hub:** Docker starts at boot, and the published addresses work as described. On Windows, Docker Desktop starts only when a user signs in; set it to start with Windows and sign in automatically, and keep the machine from sleeping.

## 🚀 Setting up the hub

1. **Tailscale.** Install it and sign in. Its address is `tailscale ip -4`, such as `100.101.102.103`.
2. **Make this machine the hub**, with its own bucket for job files:

   ```shell
   python scripts/admin.py nodes hub 100.101.102.103
   ```

   This publishes Redis and the hub's bucket (SeaweedFS) on that address only (`docker-compose.hub.yml`), never on the local network, and keeps PostgreSQL private. It gives the bucket the hub's keys; each node gets keys of its own when invited. Job files move to that bucket with `JOB_STORAGE=local`, so change nothing else.

3. **Apply it**, while no job is queued: jobs queued with files in the data volume's folders cannot move to the bucket.

   ```shell
   python scripts/server.py restart 1m
   ```

4. **On Linux, start Docker after Tailscale**, so Redis's address exists when Docker publishes it after a power cut:

   ```shell
   sudo mkdir -p /etc/systemd/system/docker.service.d
   printf '[Unit]\nAfter=tailscaled.service\nWants=tailscaled.service\n' | sudo tee /etc/systemd/system/docker.service.d/tailscale.conf
   sudo systemctl daemon-reload
   ```

### Results and the bucket

Results in the hub's bucket are sent by the API, as on one machine: apps need nothing new, and the bucket stays private. With `JOB_STORAGE=s3`, `RESULT_DOWNLOAD` decides, as without a cluster; see [Job storage](configuration.md#job-storage).

RunPod reads audio from the `STORAGE_` bucket, which must be reachable from the internet: keep your cloud bucket for it. Job files stay in the hub's bucket.

## ➕ Adding a device

1. **On the hub, invite it**, naming what it may do (default: all):

   ```shell
   python scripts/admin.py nodes invite laptop --tasks transcribe,arrange
   ```

   This creates the node's Redis password and its own keys to the hub's bucket, and writes `secrets/invites/laptop.json`. The invite holds only what the node needs, never the hub's other secrets: the cloud bucket's keys only with `JOB_STORAGE=s3` or the `dispatch` task. Copy it to the device privately, for example with `tailscale file cp secrets/invites/laptop.json laptop:`; never by email or chat.

2. **On the device:** install Tailscale and Docker, get this repository at the hub's version, and join with the invite. `join` writes `node.env` and `secrets/node/`, deletes the invite, and starts the worker:

   ```shell
   python scripts/server.py join laptop.json
   ```

   With an NVIDIA GPU it uses it; `--cpu` runs on the CPU, and `--legacy-gpu` builds the worker for older GPUs such as the GTX 10 series. Join from a checkout of its own, not the hub's.

3. **Check it:**

   ```shell
   python scripts/server.py status        # on the device
   python scripts/admin.py nodes          # on the hub: every device
   ```

## 🎛️ Day to day

| Do | Where | Command |
| --- | --- | --- |
| Take a device out, its jobs finishing first | the device | `python scripts/server.py stop` |
| Bring it back | the device | `python scripts/server.py start` |
| Stop a device taking work, from anywhere | the hub | `python scripts/admin.py nodes off laptop` (and `on`) |
| Allow or deny a task | the hub | `python scripts/admin.py nodes deny laptop arrange` (and `allow`) |
| Keep the hub's own GPU for other work | the hub | `python scripts/admin.py nodes off hub` |
| See every device | the hub | `python scripts/admin.py nodes`, or the admin page's **Devices** |
| Update a device | the device | `python scripts/server.py update` |
| Cut a device off at once | the hub | `python scripts/admin.py nodes revoke laptop` |

A switch set on the hub lasts until changed, also across reboots. A device stopped with `stop` stays stopped after a reboot; one that was running starts again by itself when Docker starts. The admin web page's **Devices** section has the same switches in read-write mode, with the reason in the audit log; inviting and revoking stay on the command line.

**Updates:** update the hub first (`git pull`, then `python scripts/server.py restart 5m`), then each device (`git pull`, then `python scripts/server.py update`). A device of an older protocol version waits, taking no job, until it is updated; one of other code but the same protocol runs, and `nodes` says so.

**Revoking** removes the node's Redis user and its bucket keys, and closes its connections; its running jobs are queued again elsewhere within a minute. With a provider's bucket, every node shares its keys: replace them at the provider if a device may be in the wrong hands.

## 📦 Images from the hub (optional)

Each device builds its worker image itself by default, about 8 GB and a while. Instead, the hub can build once and the devices pull:

```shell
python scripts/admin.py nodes hub 100.101.102.103 --registry hub.example.ts.net
python scripts/server.py restart 1m
tailscale serve --bg --https=443 http://127.0.0.1:5000
python scripts/server.py push-images --legacy
```

`--registry` takes the hub's Tailscale name; HTTPS certificates must be on for the tailnet. The registry listens on the hub itself, and `tailscale serve` gives it to the tailnet with a valid certificate, so no device needs an insecure-registry setting. Invite devices after this, so their invites name the registry. `push-images` builds the worker, and with `--legacy` the build for older GPUs, pushes them, and publishes their digests; a device's `start` and `update` pull its build by digest, so an image someone else pushed is never run. Every `restart` of the hub pushes them again.

## 🛡️ What it protects, and what it does not

* **Network:** Redis and the bucket answer only on the hub's tailnet address, and refuse anyone without a password. Tailscale encrypts the traffic.
* **A node taken over** by a malicious media file can read and change jobs in Redis, as any worker can: mark them done or failed, read their links and users' IDs, report a false load to draw or push away jobs, or queue jobs no one pays for, which the hub's GPU or RunPod, up to `OUTSOURCE_MAX_CONCURRENCY`, would run. It cannot reach billing, purchases, users' balances, limits, the tunnel token, the admin page, other devices' switches, or PostgreSQL; and with the hub's bucket, only the bucket of job files. A job's download serves only that job's own result. Nodes see the settings their workers need, never the administrators' addresses or the sign-in, billing, tunnel, and backup settings. Revoke a device you no longer trust. It can try to reach other machines on its own network: Compose has no network rules like [Kubernetes'](kubernetes.md#-network-rules-and-spreading). `app/url_safety.py` still refuses private addresses in links, and ffmpeg opens only files.
* **A stolen device:** revoke it on the hub.
* **The hub** remains the single point of failure: while it is down nothing is served, and no minute is lost. Its backups work as before.

## 🔍 Troubleshooting

* **A device shows "cannot reach Redis".** Check Tailscale on both machines, and that the hub is started: `python scripts/server.py status` on the device, and `tailscale ping <hub>`.
* **"waiting for an update".** The hub was updated to a new protocol: `git pull`, then `python scripts/server.py update` on the device.
* **Jobs do not move to a device.** It must run the task (`nodes` shows *running*), have a free place, and be less busy than `WORKER_BUSY_UTILIZATION`. A job moves at most 30 times, 2 seconds apart, then runs where it is.
* **Docker Desktop on Windows** must be started for a node to work; set it to start with Windows. The GPU needs Docker Desktop's WSL 2 backend and a current NVIDIA driver.
