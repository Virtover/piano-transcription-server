"""Administration commands. Run them in the API container, which has the
billing database:

    docker compose exec api python -m app.admin status
    docker compose exec api python -m app.admin jobs --failed --since 1d
    docker compose exec api python -m app.admin user user@example.com
    docker compose exec api python -m app.admin billing --per month
    docker compose exec api python -m app.admin limits

On the host, `python scripts/admin.py` runs them in the container for you.

Every change is written to the audit log (`audit`), and the destructive ones
and those that affect a user need a --reason. Changes to jobs and users show
what they will do and ask first; --yes skips the question, and deleting an
account asks you to type its email. --dry-run shows a change without making
it. Every command takes --json.
"""

import argparse
import sys

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.admin import audit, jobs, limits, report, status, users
from app.admin.common import AdminError, default_actor

# Errors that mean a service is not reachable, not a fault of the command.
UNREACHABLE: tuple[type[Exception], ...] = (RedisConnectionError, RedisTimeoutError)
try:
    import psycopg

    UNREACHABLE += (psycopg.OperationalError,)
except ImportError:  # pragma: no cover - psycopg is installed with the API
    pass


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m app.admin", description="Operate the transcription server.")
    root.add_argument("--debug", action="store_true", help="show the traceback when a service cannot be reached")
    commands = root.add_subparsers(dest="command", required=True)

    # Options of every command that only reads.
    reads = argparse.ArgumentParser(add_help=False)
    reads.add_argument("--json", action="store_true", help="print JSON instead of text")
    # Options of every command that changes something.
    actor = default_actor()
    changes = argparse.ArgumentParser(add_help=False)
    changes.add_argument("--by", default=actor, help=f"who is acting, for the audit log (default: {actor})")
    changes.add_argument("--reason", help="why, for the audit log; never an email address")
    changes.add_argument("--dry-run", action="store_true", help="show what would change, and change nothing")
    changes.add_argument("--json", action="store_true", help="print JSON; needs --yes where the command asks")
    asks = argparse.ArgumentParser(add_help=False)
    asks.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    expires = argparse.ArgumentParser(add_help=False)
    expires.add_argument("--for", dest="for_", metavar="DURATION", help="remove it on its own after this time, for example 2h")

    command = commands.add_parser("status", parents=[reads], help="jobs, workers, queues, disk, and pending maintenance now")
    command.set_defaults(run=status.run)

    job_commands = commands.add_parser("jobs", parents=[reads], help="list jobs; show, cancel, or requeue one")
    job_commands.add_argument("--failed", action="store_true", help="only failed jobs")
    job_commands.add_argument("--status", choices=jobs.STATUSES)
    job_commands.add_argument("--backend", choices=["local", "remote"])
    job_commands.add_argument("--user", help="email address or user ID")
    job_commands.add_argument("--since", help="only jobs created within this time, for example 1d")
    job_commands.add_argument("--limit", type=int, default=30, help="most jobs to list (default: 30)")
    job_commands.set_defaults(run=jobs.run_list)
    job_actions = job_commands.add_subparsers(dest="action")
    command = job_actions.add_parser("show", parents=[reads], help="everything known about one job, including the full error")
    command.add_argument("job", help="job ID, or its first characters")
    command.set_defaults(run=jobs.run_show)
    command = job_actions.add_parser("cancel", parents=[changes, asks], help="cancel a queued or processing job")
    command.add_argument("job", help="job ID, or its first characters")
    command.add_argument("--refund", action="store_true", help="charge nothing, for a job stuck through no fault of the user")
    command.set_defaults(run=jobs.run_cancel)
    command = job_actions.add_parser("requeue", parents=[changes, asks], help="queue a failed link job again")
    command.add_argument("job", help="job ID, or its first characters")
    backend = command.add_mutually_exclusive_group()
    backend.add_argument("--local", dest="backend", action="store_const", const="local", help="run it on this server")
    backend.add_argument("--remote", dest="backend", action="store_const", const="remote", help="run it on RunPod")
    command.set_defaults(run=jobs.run_requeue)

    user_commands = commands.add_parser("user", parents=[reads], help="one user's account; credit or delete it")
    user_commands.add_argument("user", help="email address or user ID")
    user_commands.set_defaults(run=users.run_show)
    user_actions = user_commands.add_subparsers(dest="action")
    command = user_actions.add_parser("credit", parents=[changes, asks], help="add minutes, or take them with a negative number")
    command.add_argument("minutes", type=int)
    command.set_defaults(run=users.run_credit)
    command = user_actions.add_parser("delete", parents=[changes, asks], help="delete the account and its data")
    command.set_defaults(run=users.run_delete)

    command = commands.add_parser("users", help="find accounts")
    user_searches = command.add_subparsers(dest="action", required=True)
    command = user_searches.add_parser("find", parents=[reads], help="accounts whose email address or user ID contains the text")
    command.add_argument("text", help=f"at least {users.SEARCH_LENGTH} characters, such as part of an email address")
    command.set_defaults(run=users.run_find)

    command = commands.add_parser("billing", parents=[reads], help="minutes sold, free, and used; negative balances; job time")
    command.add_argument("--per", choices=sorted(report.PERIOD_FORMATS), default="day", help="group per day or month (default: day)")
    command.add_argument("--since", default="30d", help="how far back (default: 30d)")
    command.add_argument("--remote-price-per-hour", type=float, help="your remote GPU price per hour, to estimate the cost")
    command.set_defaults(run=report.run)

    limit_commands = commands.add_parser("limits", parents=[reads], help="show or change temporary controls")
    limit_commands.set_defaults(run=limits.run_show)
    limit_actions = limit_commands.add_subparsers(dest="action")
    command = limit_actions.add_parser("pause", parents=[changes, expires], help="refuse new submissions; jobs continue")
    command.add_argument("--message", help="what users are told")
    command.set_defaults(run=limits.run_pause)
    command = limit_actions.add_parser("resume", parents=[changes], help="accept new submissions again")
    command.set_defaults(run=limits.run_resume)
    command = limit_actions.add_parser("links", parents=[changes, expires], help="turn video links off or on; uploads stay on")
    command.add_argument("state", choices=["off", "on"])
    command.add_argument("--message", help="what users are told while links are off")
    command.set_defaults(run=limits.run_links)
    command = limit_actions.add_parser("sites", parents=[changes], help="accept links again after a video site refused the server")
    command.add_argument("state", choices=["reset"])
    command.add_argument("site", nargs="?", help="a host such as youtube.com; every site if left out")
    command.set_defaults(run=limits.run_sites)
    command = limit_actions.add_parser("block", parents=[changes, expires], help="stop a user from starting transcriptions")
    command.add_argument("user", help="email address or user ID")
    command.set_defaults(run=limits.run_block)
    command = limit_actions.add_parser("unblock", parents=[changes], help="let a blocked user start transcriptions again")
    command.add_argument("user", help="email address or user ID")
    command.set_defaults(run=limits.run_unblock)
    command = limit_actions.add_parser("per-user", parents=[changes, expires], help="change the per-user job limit, or `default`")
    command.add_argument("limit", help="jobs in progress per user, or `default`")
    command.set_defaults(run=limits.run_per_user)

    command = commands.add_parser("audit", parents=[reads], help="recent admin actions")
    command.add_argument("--limit", type=int, default=30, help="most actions to list (default: 30)")
    command.set_defaults(run=audit.run)
    return root


def main(arguments: list[str]) -> int:
    args = parser().parse_args(arguments)
    try:
        args.run(args)
    except AdminError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except UNREACHABLE as error:
        if args.debug:
            raise
        print(
            f"error: Redis or the billing database cannot be reached ({type(error).__name__}: {error}). "
            "Is the stack running? --debug shows the traceback.",
            file=sys.stderr,
        )
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nStopped. Nothing more was changed", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
