#!/usr/bin/env python3
"""CI helper: validate knowledge packs and run every rule example.

Usage: python tools/validate_knowledge.py [--config codefix.toml]
Exit status 0 = valid, 2 = errors.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codefix.config import load_config  # noqa: E402
from codefix.knowledge.registry import build_registry  # noqa: E402
from codefix.knowledge.validator import run_examples, validate_knowledge  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(".", args.config)
    reg = build_registry(cfg)
    issues = validate_knowledge(reg, cfg)
    for i in issues:
        print(f"{i.level.upper():<8} {i.rule_id}: {i.message}")
    results = run_examples(reg, cfg)
    failed = [r for r in results if not r.ok]
    for r in failed:
        print(f"FAIL     {r.rule_id}/{r.name}: {r.message}")
    errors = [i for i in issues if i.level == "error"]
    print(f"{len(errors)} error(s), {len(issues) - len(errors)} warning(s); {len(results) - len(failed)}/{len(results)} examples passed")
    return 2 if errors or failed else 0


if __name__ == "__main__":
    sys.exit(main())
