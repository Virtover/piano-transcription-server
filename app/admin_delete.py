import argparse

from app.billing import Billing
from app.worker.tasks import delete_account


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete a billed user's account and data")
    parser.add_argument("--user", required=True, help="User email address")
    parser.add_argument("--yes", action="store_true", help="Delete without asking for confirmation")
    args = parser.parse_args()

    email = args.user.strip().lower()
    user_id = Billing().user_id_by_email(email)
    if user_id is None:
        parser.error(f"No billed user found for email: {email}")

    if not args.yes:
        answer = input(f"Permanently delete the account of {email}, its jobs, and remaining minutes? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Cancelled")
            return

    delete_account(user_id)
    print(f"Deleted account of {email}")


if __name__ == "__main__":
    main()
