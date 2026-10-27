"""Compatibility entry point for the explicit billing migration command."""
from app.migrate_billing import main

if __name__ == "__main__":
    raise SystemExit(main())
