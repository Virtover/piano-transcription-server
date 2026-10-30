"""`python -m app.config requirements`: what the external services must
provide, derived from the current configuration and code.

Requirements are stated as intents ("signed links must work from any IP
address"), not as a provider's setting names, so they stay true when a
provider redesigns its console. Translating them into the provider's current
settings is left to the reader or their AI agent, using the provider's own
documentation. Requirements marked `checked` are verified by
`check --online`.
"""

import math
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

# PyTorch in the RunPod image is built for this CUDA version (see
# Dockerfile.runpod; a test keeps both in sync). Hosts need a driver for it.
RUNPOD_CUDA_VERSION = "12.8"
# VRAM a GPU of a given nominal size has free for the model and jobs, after
# the CUDA context and driver. Measured headroom is about 1 GiB.
CUDA_CONTEXT_GIB = 1.0
GPU_SIZES_GIB = (16, 24)
IDLE_TIMEOUT_SECONDS = 5
STORAGE_OBJECT_LIFETIME_DAYS = 1


def storage_object_lifetime(config) -> str:
    """How long after upload the bucket may delete objects by itself. Job
    files stay until their result expires, after any wait in the queue, so
    with JOB_STORAGE=s3 that is RESULT_RETENTION plus a day."""
    days = STORAGE_OBJECT_LIFETIME_DAYS
    if config.jobs.storage == "s3":
        days += math.ceil(config.limits.result_retention / 86400)
    return f"{days} day" if days == 1 else f"{days} days"


@dataclass(frozen=True)
class Requirement:
    item: str
    value: str
    reason: str
    # Verified by `check --online`.
    checked: bool = False


@dataclass(frozen=True)
class Section:
    name: str
    provider: str
    enabled: bool
    note: str
    requirements: list[Requirement] = field(default_factory=list)


def jobs_per_gpu(size_gib: int, config) -> int:
    from app.transcription.device import job_capacity

    return job_capacity((size_gib - CUDA_CONTEXT_GIB) * 1024**3, config)


def execution_timeout_minutes(config) -> int:
    from app.outsource.remote import URL_GRACE_SECONDS

    return math.ceil((config.outsource.timeout + URL_GRACE_SECONDS) / 60)


def _or_placeholder(value: str | None, name: str) -> str:
    return value or f"<set {name}>"


# The postgres image in docker-compose.yml; the API image's pg_dump is of the
# same major version, and dumps servers of that version and older only.
POSTGRES_MAJOR_VERSION = 17
# Threads FastAPI runs blocking requests on, each with a database connection
# of its own: AnyIO's default.
API_REQUEST_THREADS = 40


def database_sections(config) -> list[Section]:
    """Redis and PostgreSQL run by a provider, when their URLs lead outside the
    Compose stack, as in the Kubernetes deployment."""
    from app.backups import RetentionPolicy
    from app.config import effective_database_url, runs_elsewhere

    redis_host = urlsplit(config.service.redis_url).hostname
    redis_managed = runs_elsewhere(config.service.redis_url)
    database_url = effective_database_url(config)
    database_host = urlsplit(database_url).hostname
    database_managed = runs_elsewhere(database_url)
    backups = config.database.backups == "server"
    longest = RetentionPolicy.from_settings(config.database).longest()
    redis = Section(
        "Redis",
        "a managed Redis service",
        redis_managed,
        f"enabled (REDIS_URL leads to {redis_host})" if redis_managed else "not used: Redis runs in the Compose stack",
        [
            Requirement(
                "Version",
                "Redis 7 or later, or a compatible service",
                "The Redis users' rules (redis/users.acl) use Redis 7's key permissions.",
            ),
            Requirement(
                "Encryption",
                "TLS (a rediss:// URL), with a certificate from a public CA or with the provider's CA in REDIS_CA_FILE",
                "Job state, results' locations, and the password cross networks you do not control.",
                True,
            ),
            Requirement(
                "Users",
                "the users of redis/users.acl with their rules, each with a password of its own; at least the "
                "services' user (app), which must not change the configuration",
                "With only one user, the admin web page signs in with the services' rights instead of its own, "
                "which can only read.",
                True,
            ),
            Requirement(
                "Commands",
                "Lua scripts (EVAL, EVALSHA) and KEYS allowed for the services' user",
                "Dramatiq, which queues the jobs, runs Lua scripts and lists keys.",
            ),
            Requirement(
                "Eviction",
                "none (maxmemory-policy noeviction)",
                "Evicting keys would silently drop queued jobs, their status, and their leases.",
            ),
            Requirement(
                "Persistence",
                "an append-only file synced every second, or replicas with automatic failover",
                "Queued jobs and every job's status live only in Redis. A failover can lose the last second of "
                "writes; a job queued in it stays queued until the next full restart sends it again.",
            ),
            Requirement(
                "Location",
                "in the EU or EEA if your users are there, close to the API and workers",
                "Job titles and users' IDs are personal data. Every request reads Redis several times.",
            ),
        ],
    )
    postgres = Section(
        "PostgreSQL",
        "a managed PostgreSQL service",
        database_managed,
        f"enabled (the billing database is at {database_host})"
        if database_managed
        else "not used: PostgreSQL runs in the Compose stack",
        [
            Requirement(
                "Version",
                f"{POSTGRES_MAJOR_VERSION} or older" if backups else "11 or later",
                "The API's pg_dump makes the billing backups, and reads servers of its own version and older."
                if backups
                else "The server needs hashtextextended, from PostgreSQL 11; backups are off, so pg_dump is not used.",
            ),
            Requirement(
                "Encryption",
                "TLS with certificate checks: sslmode=verify-full and sslrootcert=<the provider's CA file> in the "
                "URL in secrets/database_url",
                "Balances, purchases, and users' email addresses cross networks you do not control.",
            ),
            Requirement(
                "Connections",
                f"at least {API_REQUEST_THREADS} per API replica, plus 1 per worker thread",
                "Each request and each job opens a connection of its own while it reads or changes billing. A "
                "pooler in transaction mode works too: the server's locks last one transaction.",
            ),
            Requirement(
                "Backups",
                "pg_dump allowed for the billing user, and DATABASE_BACKUP_DIR on lasting storage"
                if backups
                else f"automatic backups with point-in-time recovery, kept {_days(longest)} or longer",
                f"The API dumps the database every {config.database.backup_interval / 60:g} minutes."
                if backups
                else "With DATABASE_BACKUPS=provider, the server makes none; the provider's are the only ones. The "
                "retention follows DATABASE_BACKUP_KEEP_*.",
            ),
            Requirement(
                "Location",
                "in the EU or EEA if your users are there",
                "The database holds users' email addresses and purchase records, personal data under GDPR.",
            ),
        ],
    )
    return [redis, postgres]


def storage_sections(config) -> list[Section]:
    """The bucket that outsourcing and JOB_STORAGE=s3 share. Each reason
    says what the features in use need; with neither in use, what outsourcing
    would."""
    job_files = config.jobs.storage == "s3"
    outsourcing = config.outsource.provider == "runpod"
    uses = [name for name, used in (("OUTSOURCE_PROVIDER=runpod", outsourcing), ("JOB_STORAGE=s3", job_files)) if used]
    note = (
        f"enabled ({', '.join(uses)})"
        if uses
        else "not enabled; OUTSOURCE_PROVIDER=runpod and JOB_STORAGE=s3 use it"
    )
    # Without either, describe it as outsourcing uses it.
    audio = outsourcing or not job_files
    endpoint = _or_placeholder(config.storage.endpoint, "STORAGE_ENDPOINT")
    bucket = _or_placeholder(config.storage.bucket, "STORAGE_BUCKET")

    def joined(*parts: str | None) -> str:
        return " ".join(part for part in parts if part)

    return [
        Section(
            "Storage",
            "S3-compatible object storage, for example Cloudflare R2",
            bool(uses),
            note,
            [
                Requirement(
                    "Bucket",
                    bucket,
                    joined(
                        "Audio is handed to the GPU endpoint through this bucket." if audio else None,
                        "Job files are kept in it for every API and worker." if job_files else None,
                    ),
                    True,
                ),
                Requirement(
                    "S3 endpoint URL",
                    endpoint,
                    "The server uploads here" + (", and the GPU endpoint downloads only from this host." if audio else "."),
                    True,
                ),
                Requirement(
                    "Credentials",
                    f"an access key ID and secret key that can {'list, ' if job_files else ''}read, write, and delete "
                    "objects in this bucket only, saved as secrets/storage_access_key and secrets/storage_secret_key",
                    joined(
                        "The server uploads audio, signs download links, and deletes the audio afterwards." if audio else None,
                        "It keeps job files, signs links to results, and lists the files to find expired ones."
                        if job_files else None,
                    ),
                    True,
                ),
                Requirement(
                    "Network restriction",
                    "none: signed download links must work from any IP address",
                    joined(
                        "GPU workers download from the provider's addresses, which change." if audio else None,
                        "Users' apps download MIDI results through these links." if job_files else None,
                        "check --online can only test downloads from this server.",
                    ),
                ),
                Requirement(
                    "Credential expiry",
                    "none, or a renewal date you track",
                    "When the credentials expire, " + ("every job fails." if job_files else "every outsourced job fails."),
                ),
                Requirement(
                    "Automatic deletion",
                    f"objects deleted {storage_object_lifetime(config)} after upload" + (", not sooner" if job_files else ""),
                    joined(
                        "The server deletes each object itself; this catches leftovers after a crash.",
                        "Sooner would delete results that can still be downloaded." if job_files else None,
                    ),
                ),
                *([Requirement(
                    "Shared with",
                    "no other server, such as a development or test copy",
                    "Cleanup deletes job files whose job this server's Redis does not know, so two servers on one "
                    "bucket delete each other's files.",
                )] if job_files else []),
                Requirement(
                    "Location",
                    "in the EU or EEA if your users are there",
                    "User audio is personal data under GDPR. Usually fixed when the bucket is created.",
                ),
            ],
        )
    ]


def outsourcing_sections(config) -> list[Section]:
    enabled = config.outsource.provider == "runpod"
    note = "enabled (OUTSOURCE_PROVIDER=runpod)" if enabled else "not enabled; set OUTSOURCE_PROVIDER=runpod to use it"
    endpoint = _or_placeholder(config.storage.endpoint, "STORAGE_ENDPOINT")
    small, large = (jobs_per_gpu(size, config) for size in GPU_SIZES_GIB)
    timeout_minutes = execution_timeout_minutes(config)
    if config.outsource.max_concurrency:
        workers = f"at least {math.ceil(config.outsource.max_concurrency / max(1, small)) + 1}"
        workers_reason = (
            f"OUTSOURCE_MAX_CONCURRENCY={config.outsource.max_concurrency} jobs at about {small} per worker, "
            "plus one worker of headroom while another starts."
        )
    else:
        workers = "any; this caps the cost"
        workers_reason = (
            "OUTSOURCE_MAX_CONCURRENCY=auto follows the endpoint's maximum workers, read with the optional "
            "read key; without it, the server allows 4 remote jobs."
        )

    registry = Section(
        "Image registry",
        "any container registry the GPU provider can pull from, for example Docker Hub",
        enabled,
        note,
        [
            Requirement("Repository", "private", "The image contains the server code."),
            Requirement(
                "Your credential",
                "can push",
                "Used with docker login to upload the image.",
            ),
            Requirement(
                "The GPU provider's credential",
                "can only pull",
                "If this copy leaks, nobody can overwrite the image.",
            ),
            Requirement(
                "Image",
                "built from Dockerfile.runpod, with a new tag for every build",
                "Providers may keep running a cached image when the tag does not change.",
            ),
        ],
    )
    gpu = Section(
        "GPU endpoint",
        "RunPod Serverless",
        enabled,
        note,
        [
            Requirement(
                "Image",
                "<registry>/piano-transcription-runpod:<tag>, with the pull credential",
                "The handler and the model are in the image; no volume is needed.",
                True,
            ),
            Requirement(
                "Environment variable",
                f"STORAGE_ENDPOINT={endpoint}",
                "The handler downloads only from this host. It must match the server exactly.",
                True,
            ),
            Requirement(
                "Secrets",
                "none",
                "Storage and API keys stay on this server; the endpoint only receives short-lived download links.",
            ),
            Requirement(
                "GPU memory",
                f"{GPU_SIZES_GIB[0]} GB or more",
                f"About {small} jobs fit on {GPU_SIZES_GIB[0]} GB and {large} on {GPU_SIZES_GIB[1]} GB "
                f"(WORKER_MODEL_MEMORY_GIB={config.worker.model_memory_gib:g}, "
                f"WORKER_MEMORY_PER_JOB_GIB={config.worker.memory_per_job_gib:g}).",
            ),
            Requirement(
                "Host CUDA version",
                f"{RUNPOD_CUDA_VERSION} or newer only",
                f"PyTorch in the image is built for CUDA {RUNPOD_CUDA_VERSION}; on an older driver the job fails at start.",
                True,
            ),
            Requirement(
                "Execution timeout",
                f"at least {timeout_minutes} min",
                f"OUTSOURCE_TIMEOUT ({config.outsource.timeout / 60:g} min) plus the download link's grace period. "
                "Shorter, and the provider stops jobs the server still waits for.",
                True,
            ),
            Requirement("Minimum workers", "0", "Workers that are always on are billed while idle.", True),
            Requirement("Maximum workers", workers, workers_reason, True),
            Requirement(
                "Scaling",
                f"by the number of requests, about {small} per worker ({large} on {GPU_SIZES_GIB[1]} GB)",
                "A worker takes that many requests at once and runs them as one batch, so another worker should "
                "start only when it is full. Scaling by queue delay adds workers late.",
                True,
            ),
            Requirement(
                "Idle timeout",
                f"about {IDLE_TIMEOUT_SECONDS} s",
                "How long an idle worker stays billed while waiting for the next request.",
                True,
            ),
            Requirement(
                "Idle workers",
                "paused instead of stopped, if offered",
                "A paused worker resumes in about a second; a cold start takes 10 to 15 s.",
            ),
            Requirement(
                "Hosting",
                "provider-operated data centers, not machines of private individuals",
                "User audio is processed there.",
            ),
            Requirement(
                "Data centers",
                "several in the EU or EEA if your users are there",
                "GDPR; several locations keep GPUs available. Compliance filters beyond GDPR remove data centers "
                "and do not apply to music audio.",
            ),
            Requirement(
                "API key",
                "restricted to this endpoint with read and write access, saved as secrets/runpod_api_key",
                "The server submits, follows, and cancels jobs with it. A leaked copy cannot touch anything else.",
                True,
            ),
            Requirement(
                "Read key (optional)",
                "read-only access to the account, saved as secrets/runpod_read_api_key",
                "Restricted keys cannot read endpoint settings. With this key, check --online verifies the "
                "settings in this list, and OUTSOURCE_MAX_CONCURRENCY=auto reads the maximum workers. It cannot "
                "change anything.",
                True,
            ),
        ],
    )
    return [registry, gpu]


def google_sections(config) -> list[Section]:
    enabled = config.billing.provider == "google_play"
    note = (
        "enabled (BILLING_PROVIDER=google_play)"
        if enabled
        else "not enabled; set BILLING_PROVIDER=google_play to use sign-in and billing"
    )
    package = _or_placeholder(config.google_play.package_name, "GOOGLE_PLAY_PACKAGE_NAME")
    client_count = len([client_id for client_id in config.auth.client_ids if client_id.strip()])
    products = ", ".join(f"{product} ({minutes} min)" for product, minutes in config.billing.products.items())

    sign_in = Section(
        "Google sign-in",
        "Google Cloud, OAuth",
        enabled,
        note,
        [
            Requirement(
                "Project",
                "one Google Cloud project, also used by the Play service account",
                "Keeps the clients, the API, and the service account together.",
            ),
            Requirement(
                "Consent screen",
                "for external users, with the scopes openid and email only",
                "The server needs the account ID and a verified email. Both are non-sensitive scopes, so no app "
                "review is needed. While in testing, only listed test users can sign in.",
            ),
            Requirement(
                "Android clients",
                f"one per signing certificate, for package {package}: debug, upload key, and Play app signing",
                "Sign-in checks the package name and the SHA-1 of the certificate that signed the app. Without the "
                "Play app signing client, sign-in fails for every Play Store install (error 10, DEVELOPER_ERROR).",
            ),
            Requirement(
                "Web client",
                "one, without redirect URIs",
                "Android sign-in libraries use it as the server client ID.",
            ),
            Requirement(
                "GOOGLE_OAUTH_CLIENT_IDS",
                f"every client ID above, as a JSON list (now {client_count})",
                "The server accepts tokens issued to these clients only. Client IDs are public; no client secret.",
                True,
            ),
        ],
    )
    play = Section(
        "Google Play",
        "Google Cloud and Play Console",
        enabled,
        note,
        [
            Requirement(
                "API",
                "the Google Play Android Developer API, enabled in the sign-in project",
                "The server lists products and verifies purchases with it.",
                True,
            ),
            Requirement(
                "Service account",
                "in the same project, without Google Cloud roles, with a JSON key saved as "
                "secrets/google_play_service_account.json",
                "Its permissions come from Play Console, not Google Cloud.",
                True,
            ),
            Requirement(
                "Play Console access",
                f"the service account invited, with access to {package}: read app information, read orders and "
                "financial data, manage orders",
                "To list products, to read and consume purchases, and to read refunds and chargebacks so their minutes "
                "are taken back. New permissions can take up to 24 hours.",
                True,
            ),
            Requirement(
                "Products",
                f"active one-time products: {products}" if products else "<set BILLING_PRODUCTS>",
                "Only products that exist in Play Console and are listed in BILLING_PRODUCTS are offered.",
                True,
            ),
            Requirement(
                "Test purchases",
                "your account as a license tester, and the app installed from a testing track",
                "License testers are never charged. Billing does not work in an app that was never uploaded.",
            ),
        ],
    )
    return [sign_in, play]


def tunnel_sections(config) -> list[Section]:
    enabled = config.tunnel.enabled
    note = (
        "enabled (COMPOSE_PROFILES=cloudflare)"
        if enabled
        else "not enabled; set COMPOSE_PROFILES=cloudflare to serve the API over HTTPS through Cloudflare"
    )
    hostname = _or_placeholder(config.tunnel.hostname, "CLOUDFLARE_TUNNEL_HOSTNAME")
    upload_mb = (
        f"{config.limits.max_upload_size_mb:g}"
        if config.limits.max_upload_size_mb is not None
        else "unlimited"
    )
    return [
        Section(
            "Cloudflare Tunnel",
            "Cloudflare",
            enabled,
            note,
            [
                Requirement(
                    "Domain",
                    "a domain whose DNS is managed by Cloudflare",
                    "A tunnel's public hostnames are names in a domain of the same Cloudflare account.",
                ),
                Requirement(
                    "Tunnel",
                    "one tunnel, run with a token, the token saved as secrets/cloudflare/tunnel_token",
                    "The cloudflared service in docker-compose.yml runs it. It connects out to Cloudflare, so no "
                    "port needs to be open on this machine or its router.",
                    True,
                ),
                Requirement(
                    "Public hostname",
                    f"{hostname}, sent to the service http://api:8000",
                    "cloudflared runs in the Compose network, where the API is reachable by its service name. "
                    "localhost there is cloudflared itself.",
                    True,
                ),
                Requirement(
                    "Request size",
                    f"request bodies up to {upload_mb} MB (MAX_UPLOAD_SIZE_MB), or MAX_UPLOAD_SIZE_MB lowered to "
                    "what the plan accepts",
                    "Cloudflare refuses larger uploads before they reach the server, with an error the app cannot "
                    "explain. The limit depends on the Cloudflare plan.",
                ),
                Requirement(
                    "No challenges or caching",
                    f"no browser challenge, bot protection, or access login on {hostname}, and no caching",
                    "The app calls the API directly and cannot pass a challenge page. Responses belong to one "
                    "user and change while jobs run. Protect the server with its own password instead.",
                    True,
                ),
            ],
        )
    ]


def admin_web_sections(config) -> list[Section]:
    from app.admin_web import PORT, SERVICE_HOST

    admin = config.admin_web
    enabled = admin.enabled
    note = (
        f"enabled (ADMIN_WEB_MODE={admin.mode})"
        if enabled
        else "not enabled; set ADMIN_WEB_MODE=read-only and add admin to COMPOSE_PROFILES for a page behind Cloudflare Access"
    )
    hostname = _or_placeholder(admin.hostname, "ADMIN_WEB_HOSTNAME")
    emails = ", ".join(admin.emails) or "<set ADMIN_WEB_EMAILS>"
    return [
        Section(
            "Admin web page",
            "Cloudflare",
            enabled,
            note,
            [
                Requirement(
                    "Public hostname",
                    f"{hostname} on the same tunnel, sent to the service http://{SERVICE_HOST}:{PORT}",
                    "The admin service publishes no port; cloudflared reaches it by its service name. It must be a "
                    "hostname of its own: the API's hostname has no sign-in, and the app could not pass one.",
                    True,
                ),
                Requirement(
                    "Sign-in in front of it",
                    f"Cloudflare Access protecting every path of {hostname}",
                    "Strangers must not reach the page at all. The page also checks Cloudflare's signature on every "
                    "request, so it stays closed if this is ever missing, but that is the second lock, not the first.",
                    True,
                ),
                Requirement(
                    "Who may sign in",
                    f"only {emails}",
                    "The page refuses every other address too; a broader rule only lets others reach that refusal.",
                ),
                Requirement(
                    "Second factor",
                    "sign-in with a second factor, for example through an identity provider that requires one",
                    "The page shows every account and job. A stolen password alone must not be enough.",
                ),
                Requirement(
                    "Session length",
                    "sessions of at most 24 hours",
                    "A lost or shared device keeps access only as long as its session.",
                ),
                Requirement(
                    "Application tag",
                    "the Access application's AUD tag in CLOUDFLARE_ACCESS_AUD",
                    "The page accepts only tokens Cloudflare signed for this application, not for another one of yours.",
                    True,
                ),
                Requirement(
                    "Team domain",
                    f"{_or_placeholder(admin.access_team_domain, 'CLOUDFLARE_ACCESS_TEAM_DOMAIN')}, the team's own domain",
                    "The page reads the keys that sign the tokens from it.",
                    True,
                ),
                Requirement(
                    "The Cloudflare account",
                    "a second factor on the Cloudflare account itself",
                    "Whoever controls the account can change who Access lets in.",
                ),
            ],
        )
    ]


def _days(seconds: float) -> str:
    return f"{seconds / (24 * 60 * 60):g} days"


def offsite_sections(config) -> list[Section]:
    from app.backups import RetentionPolicy

    enabled = config.offsite.enabled
    note = (
        "enabled (OFFSITE_BACKUP_BUCKET is set)"
        if enabled
        else "not enabled; set OFFSITE_BACKUP_BUCKET to keep encrypted billing backups off this machine"
    )
    endpoint = _or_placeholder(config.offsite.endpoint, "OFFSITE_BACKUP_ENDPOINT")
    bucket = _or_placeholder(config.offsite.bucket, "OFFSITE_BACKUP_BUCKET")
    longest = RetentionPolicy.from_settings(config.database).longest()
    return [
        Section(
            "Off-site backups",
            "S3-compatible object storage, for example Cloudflare R2 or Backblaze B2",
            enabled,
            note,
            [
                Requirement(
                    "Bucket",
                    f"{bucket}, used for nothing else, not the outsourcing bucket",
                    "Billing backups are copied here, encrypted, after every backup.",
                    True,
                ),
                Requirement("S3 endpoint URL", endpoint, "The server uploads, lists, and deletes backups here.", True),
                Requirement(
                    "Credentials",
                    "an access key ID and secret key that can list, read, write, and delete objects in this bucket "
                    "only, saved as secrets/offsite_backup_access_key and secrets/offsite_backup_secret_key",
                    "The server deletes the backups its retention steps no longer keep. Keys of their own mean the "
                    "keys used for every outsourced job cannot reach the backups.",
                    True,
                ),
                Requirement(
                    "Public access",
                    "none",
                    "The backups are encrypted, but nobody needs to read them over the web.",
                ),
                Requirement(
                    "Automatic deletion",
                    f"none, or after more than {_days(longest)}",
                    "The server deletes what DATABASE_BACKUP_KEEP_* and DATABASE_BACKUP_RETENTION no longer keep; "
                    "an earlier deletion rule would remove the oldest history.",
                ),
                Requirement(
                    "Location",
                    "in the EU or EEA if your users are there, in a different place than this machine",
                    "The backups hold user emails and purchase records, personal data under GDPR. A copy in the "
                    "same building does not survive a fire or flood.",
                ),
                Requirement(
                    "Encryption key",
                    "an age key pair: the public key in OFFSITE_BACKUP_RECIPIENT, the private key stored away from "
                    "this machine, for example in a password manager",
                    "Backups are encrypted before upload, so neither the storage provider nor a leaked storage key "
                    "can read them. Only the private key can, and a backup cannot be restored without it.",
                ),
            ],
        )
    ]


def build_requirements(config) -> list[Section]:
    return (
        database_sections(config) + storage_sections(config) + outsourcing_sections(config) + google_sections(config) + tunnel_sections(config)
        + admin_web_sections(config) + offsite_sections(config)
    )


def format_requirements(sections: list[Section]) -> list[str]:
    lines = []
    for section in sections:
        lines.append(f"{section.name} ({section.provider}): {section.note}")
        for requirement in section.requirements:
            mark = "  [checked]" if requirement.checked else ""
            lines.append(f"  {requirement.item}: {requirement.value}{mark}")
            lines.append(f"      why: {requirement.reason}")
        lines.append("")
    lines.append("[checked] = verified by: python -m app.config check --online")
    return lines


def requirements_json(sections: list[Section]) -> list[dict]:
    return [asdict(section) for section in sections]
