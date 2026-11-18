"""Build a source-only ZIP with an integrity manifest; never publish or upload."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.releases import sha256, source_files
from core.version import VERSION


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'dist' / f'FootballClipFactory-v{VERSION}-source.zip')
    args = parser.parse_args(argv)
    destination = args.output.resolve()
    files = source_files(ROOT)
    files.extend(path for directory in ('input', 'cache', 'output')
                 if (path := ROOT / directory / '.gitkeep').is_file())
    files = sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix())
    hashes = {path.relative_to(ROOT).as_posix(): sha256(path) for path in files}
    if not {'main.py', 'ui/app.py', 'storage/database.py', 'README.md'} <= hashes.keys():
        raise RuntimeError('Required project files are missing')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting an earlier source handoff.
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, 'FootballClipFactory/' + path.relative_to(ROOT).as_posix())
        archive.writestr('FootballClipFactory/SOURCE_MANIFEST.json',
                        json.dumps({'version': VERSION, 'sha256': hashes}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'archive': str(destination), 'source_files': len(files),
                      'sha256': sha256(destination)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
