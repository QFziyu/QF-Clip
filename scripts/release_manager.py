"""Create/list/inspect/verify immutable source releases; never restore over live data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.releases import create_release, read_manifest, release_directory, verify_release


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="版本快照与完整性校验（不覆盖工作目录）")
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("version")
    snapshot.add_argument("--kind", default="checkpoint")
    sub.add_parser("list")
    for command in ("inspect", "verify"):
        item = sub.add_parser(command)
        item.add_argument("version")
        if command == "verify":
            item.add_argument("--primary", action="store_true", help="校验主目录是否与该快照一致")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = create_release(args.root, args.version, args.kind)
        elif args.command == "list":
            result = [{"version": p.name.removesuffix(".manifest.json"), "manifest": str(p)}
                      for p in sorted(release_directory(args.root).glob("*.manifest.json"))]
        elif args.command == "inspect":
            result = read_manifest(args.root, args.version)
        else:
            result = verify_release(args.root, args.version, code_root=args.root if args.primary else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"版本操作失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
