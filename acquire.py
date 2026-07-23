"""acquire.py — repo-root shim for the acquisition agent CLI."""

from tools.acquisition.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
