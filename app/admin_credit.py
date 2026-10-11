import argparse

from app.billing import Billing


def main() -> None:
    parser = argparse.ArgumentParser(description="Give transcription minutes to a billed user")
    parser.add_argument("--user", required=True, help="User email address")
    parser.add_argument("--minutes", required=True, type=int)
    args = parser.parse_args()
    if args.minutes <= 0:
        parser.error("--minutes must be greater than zero")

    billing = Billing()
    try:
        billing.credit_minutes_by_email(args.user, args.minutes)
    except ValueError as error:
        parser.error(str(error))
    print(f"Credited {args.minutes} minutes to {args.user.strip().lower()}")


if __name__ == "__main__":
    main()