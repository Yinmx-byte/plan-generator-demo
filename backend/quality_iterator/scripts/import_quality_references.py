from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.quality_reference_store import get_quality_reference_store


def main() -> None:
    parser = argparse.ArgumentParser(description="Import classified high-quality DOCX plans to RDS + OSS.")
    parser.add_argument("source_dir")
    args = parser.parse_args()
    root = Path(args.source_dir)
    if not root.is_dir():
        raise SystemExit(f"Source directory does not exist: {root}")
    result = get_quality_reference_store().import_directory(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
