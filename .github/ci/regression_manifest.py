"""Prove that distributed CI executed the complete, identical pytest collection."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def shard_for(nodeid: str, count: int) -> int:
    # Keep module fixtures and file-level concurrency tests together.
    filename = nodeid.split("::", 1)[0]
    return int.from_bytes(hashlib.sha256(filename.encode()).digest()[:8], "big") % count


def verify(manifests: list[dict], *, count: int, revision: str) -> int:
    if count < 1 or len(manifests) != count:
        raise ValueError("missing or extra shard manifests")
    if sorted(m["shard"] for m in manifests) != list(range(count)):
        raise ValueError("missing or duplicate shard indices")
    full = manifests[0]["collected"]
    if not full or len(full) != len(set(full)):
        raise ValueError("empty or duplicate full collection")
    completed = []
    for manifest in manifests:
        if manifest["revision"] != revision or manifest["count"] != count:
            raise ValueError("wrong revision or shard count")
        if manifest["exitstatus"] != 0 or manifest["errors"]:
            raise ValueError("failed or incomplete shard")
        if manifest["collected"] != full:
            raise ValueError("shards disagree on full collection")
        expected = sorted(n for n in full if shard_for(n, count) == manifest["shard"])
        if manifest["selected"] != expected or sorted(manifest["completed"]) != expected:
            raise ValueError("missing, duplicate, or unexpected test execution")
        completed.extend(manifest["completed"])
    if sorted(completed) != full:
        raise ValueError("shard union differs from full collection")
    return len(full)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    manifests = [json.loads(p.read_text()) for p in sorted(args.directory.rglob("manifest.json"))]
    total = verify(manifests, count=args.count, revision=args.revision)
    print(f"Verified {total} collected tests executed exactly once across {args.count} shards")


if __name__ == "__main__":
    main()
