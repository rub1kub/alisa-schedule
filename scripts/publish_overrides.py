"""Validate and atomically publish a replacement file inside the skill's directory."""

import argparse
import os
import tempfile
from pathlib import Path

from app.models import Overrides, read_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--target", type=Path, default=Path("data/overrides.json"))
    args = parser.parse_args()
    data = Overrides.model_validate(read_json(args.source))
    payload = data.model_dump_json(indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".overrides-", dir=args.target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, args.target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Overrides validated and published")


if __name__ == "__main__":
    main()
