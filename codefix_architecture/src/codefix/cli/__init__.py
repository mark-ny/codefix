"""Command-line interface for Codefix."""

import argparse


def main() -> int:
    """Run the Codefix command-line interface."""
    parser = argparse.ArgumentParser(description="Analyze and repair code safely.")
    parser.add_argument("--version", action="version", version="codefix 0.1.0")
    parser.parse_args()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
