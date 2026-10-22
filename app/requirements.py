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

# PyTorch in the RunPod image is built for this CUDA version (see
# Dockerfile.runpod; a test keeps both in sync). Hosts need a driver for it.
RUNPOD_CUDA_VERSION = "12.8"
# VRAM a GPU of a given nominal size has free for the model and jobs, after
# the CUDA context and driver. Measured headroom is about 1 GiB.
CUDA_CONTEXT_GIB = 1.0
GPU_SIZES_GIB = (16, 24)
IDLE_TIMEOUT_SECONDS = 5
STORAGE_OBJECT_LIFETIME = "1 day"


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


def outsourcing_sections(config) -> list[Section]:
    enabled = config.outsource.provider == "runpod"
    note = "enabled (OUTSOURCE_PROVIDER=runpod)" if enabled else "not enabled; set OUTSOURCE_PROVIDER=runpod to use it"
    endpoint = _or_placeholder(config.outsource.storage_endpoint, "OUTSOURCE_STORAGE_ENDPOINT")
    bucket = _or_placeholder(config.outsource.storage_bucket, "OUTSOURCE_STORAGE_BUCKET")
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

    storage = Section(
        "Storage",
        "S3-compatible object storage, for example Cloudflare R2",
        enabled,
        note,
        [
            Requirement("Bucket", bucket, "Audio is handed to the GPU endpoint through this bucket.", True),
            Requirement(
                "S3 endpoint URL",
                endpoint,
                "The server uploads here, and the GPU endpoint downloads only from this host.",
                True,
            ),
            Requirement(
                "Credentials",
                "an access key ID and secret key that can read, write, and delete objects in this bucket only, "
                "saved as secrets/outsource_storage_access_key and secrets/outsource_storage_secret_key",
                "The server uploads audio, signs download links, and deletes the audio afterwards.",
                True,
            ),
            Requirement(
                "Network restriction",
                "none: signed download links must work from any IP address",
                "GPU workers download from the provider's addresses, which change. "
                "check --online can only test downloads from this server.",
            ),
            Requirement(
                "Credential expiry",
                "none, or a renewal date you track",
                "When the credentials expire, every outsourced job fails.",
            ),
            Requirement(
                "Automatic deletion",
                f"objects deleted {STORAGE_OBJECT_LIFETIME} after upload",
                "The server deletes each object itself; this catches leftovers after a crash.",
            ),
            Requirement(
                "Location",
                "in the EU or EEA if your users are there",
                "User audio is personal data under GDPR. Usually fixed when the bucket is created.",
            ),
        ],
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
                f"OUTSOURCE_STORAGE_ENDPOINT={endpoint}",
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
    return [storage, registry, gpu]


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
                "To list products, and to read and consume purchases. New permissions can take up to 24 hours.",
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


def build_requirements(config) -> list[Section]:
    return outsourcing_sections(config) + google_sections(config)


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
