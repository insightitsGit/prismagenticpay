"""Fail if built distributions contain secrets or operator-only files."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from test_release_hygiene import test_built_artifacts_exclude_secrets_and_operator_files  # noqa: E402


def main() -> int:
    dist = ROOT / "dist"
    archives = list(dist.glob("*.whl")) + list(dist.glob("*.tar.gz"))
    if not archives:
        print("dist/ is empty; run python -m build first")
        return 2
    test_built_artifacts_exclude_secrets_and_operator_files()
    print("Release artifacts contain no embedded secrets or operator credential files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
