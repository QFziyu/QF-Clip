"""Immutable source checkpoints and isolated fallback environments (stdlib only)."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import uuid
import zipfile

from .version import RELEASE


EXCLUDED_DIRS = {".runtime", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
                 "cache", "input", "output", "runtime-cpu", "node_modules", "release-data", "data-snapshots",
                 "codex_official", "runtime", "dist", "build", ".idea", ".vscode", ".ruff_cache"}
SOURCE_NAMES = {".env.example", ".gitignore", ".gitattributes", ".editorconfig", ".gitkeep", "LICENSE"}
SOURCE_SUFFIXES = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".ps1", ".cmd", ".bat", ".html", ".css"}
ENV_KEYS = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_TIMEOUT", "OPENAI_MAX_TIMEOUT", "WHISPER_MODEL", "DEVICE",
            "COMPUTE_TYPE", "FCF_RENDER_ENCODER", "FFMPEG_PATH", "FFPROBE_PATH", "HF_HOME", "HF_HUB_CACHE"}
RUNTIME_MUTABLE_FILES = frozenset({"config/settings.yaml", "config/clip_rules.yaml",
                                  "config/football_terms.txt", "prompts/clip_selection.md"})


def version_name(version: str) -> str:
    if not isinstance(version, str) or not re.fullmatch(r"v\d+\.\d+(?:\.\d+)?(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?", version):
        raise ValueError("版本名称必须类似 v1.0.3-gpu 或 v1.0.3-cpu")
    return version


def release_directory(root: str | Path) -> Path:
    root = Path(root).resolve()
    result = root.parent / f"{root.name}_Releases"
    if result.is_symlink() or result.resolve().parent != root.parent:
        raise ValueError("版本目录不能指向项目父目录以外的位置")
    return result


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_member(root: Path, name: str) -> Path:
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError(f"非法清单路径: {name!r}")
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or any(p in ("..", ".", "") for p in name.split("/")):
        raise ValueError(f"非法清单路径: {name!r}")
    result = root.joinpath(*parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"清单路径越界: {name!r}")
    return result


def source_files(root: Path) -> list[Path]:
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS and not d.startswith(".staging-"))
        for d in dirs:
            candidate = parent / d
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                raise ValueError(f"源码包含符号链接或目录联接: {candidate}")
        for filename in sorted(files):
            path = parent / filename
            if filename.lower().startswith(".env") and filename != ".env.example":
                continue
            if filename.lower() == "secrets.toml" or filename.lower().endswith((".local.yaml", ".local.yml")):
                continue
            if path.suffix.lower() not in SOURCE_SUFFIXES and filename not in SOURCE_NAMES:
                continue
            if filename.startswith(".manifest"):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"源码包含符号链接: {path}")
            result.append(path)
    return sorted(result, key=lambda p: p.relative_to(root).as_posix())


def read_dotenv(path: Path) -> dict[str, str]:
    values = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def _database_path(root: Path) -> Path:
    values = {}
    settings = root / "config/settings.yaml"
    if settings.exists():
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("请先安装 requirements.txt，才能读取数据库配置并创建版本") from exc
        values = yaml.safe_load(settings.read_text(encoding="utf-8-sig")) or {}
    env = {**read_dotenv(root / ".env"), **os.environ}
    database = env.get("DATABASE_PATH", "").strip()
    if not database:
        output = env.get("OUTPUT_DIR", "").strip()
        database = str(Path(output) / "factory.sqlite3") if output else values.get("database", "output/factory.sqlite3")
    path = Path(database).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def backup_database(source: Path, destination: Path) -> None:
    """SQLite online backup includes committed WAL transactions without copying live WAL."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"数据库备份已存在: {destination}")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=256, sleep=0.1)
            dst.commit()
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库备份校验失败")


def create_release(root: str | Path, version: str, kind: str = "checkpoint") -> dict:
    root = Path(root).resolve()
    version = version_name(version)
    releases = release_directory(root)
    releases.mkdir(parents=True, exist_ok=True)
    snapshot, archive = releases / version, releases / f"{version}.zip"
    manifest_path = releases / f"{version}.manifest.json"
    database_dir = releases / "data-snapshots" / version
    for target in (snapshot, archive, manifest_path, database_dir):
        if target.exists():
            raise FileExistsError(f"版本已存在，禁止覆盖: {target}")
    lock = releases / f".{version}.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    staging = releases / f".staging-{version}-{uuid.uuid4().hex}"
    try:
        staged_code = staging / "code"
        staged_code.mkdir(parents=True)
        hashes = {}
        for path in source_files(root):
            name = path.relative_to(root).as_posix()
            destination = safe_member(staged_code, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            hashes[name] = sha256(destination)
        if not hashes or "main.py" not in hashes or "ui/app.py" not in hashes:
            raise ValueError("项目缺少 main.py 或 ui/app.py，无法创建可运行版本")
        staged_archive = staging / "source.zip"
        with zipfile.ZipFile(staged_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for name in sorted(hashes):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, (staged_code / name).read_bytes())
        manifest = {"schema_version": 1, "version": version, "kind": str(kind),
                    "created_at": datetime.now(timezone.utc).isoformat(), "code_path": str(snapshot),
                    "source_files": hashes, "source_count": len(hashes),
                    "archive": str(archive), "archive_sha256": sha256(staged_archive),
                    "database_snapshot": None, "secrets_included": False,
                    "runtime": "Source snapshot; Python/FFmpeg/model files are not bundled. Use primary or dedicated venv."}
        database = _database_path(root)
        if database.is_file():
            backup = staging / "database" / "factory.sqlite3"
            backup_database(database, backup)
            manifest["database_snapshot"] = str(database_dir / backup.name)
            manifest["database_sha256"] = sha256(backup)
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged_code.rename(snapshot)
        staged_archive.rename(archive)
        if (staging / "database").exists():
            database_dir.parent.mkdir(parents=True, exist_ok=True)
            (staging / "database").rename(database_dir)
        (staging / "manifest.json").rename(manifest_path)
        return manifest
    finally:
        # Only remove this invocation's unique staging directory, never an existing release.
        if staging.exists() and staging.resolve().parent == releases.resolve():
            shutil.rmtree(staging)
        lock.unlink(missing_ok=True)


def read_manifest(root: str | Path, version: str) -> dict:
    path = release_directory(root) / f"{version_name(version)}.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    recorded = manifest.get("version", "")
    if recorded != version and "v" + recorded != version:  # Original v0.1 manifest predates this API.
        raise ValueError("版本清单名称不一致")
    if not isinstance(manifest.get("source_files"), dict) or not manifest["source_files"]:
        raise ValueError("版本清单缺少源码哈希")
    return manifest


def verify_release(root: str | Path, version: str, *, code_root: str | Path | None = None,
                   allow_customization: bool = False) -> dict:
    releases = release_directory(root)
    manifest = read_manifest(root, version)
    code = Path(code_root).resolve() if code_root else releases / version_name(version)
    if allow_customization and code != Path(root).resolve():
        raise ValueError("仅主项目运行校验允许自定义配置；版本快照必须严格校验")
    if code_root is None and (code.is_symlink() or code.resolve().parent != releases.resolve()):
        raise ValueError("版本源码目录越界")
    errors, customized = [], []
    for name, expected in manifest["source_files"].items():
        path = safe_member(code, name)
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("非法 SHA256 清单")
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing: {name}")
        elif sha256(path) != expected:
            if allow_customization and name in RUNTIME_MUTABLE_FILES:
                customized.append(name)
            else:
                errors.append(f"changed: {name}")
    known = set(manifest["source_files"])
    errors.extend(f"unexpected: {p.relative_to(code).as_posix()}" for p in source_files(code.resolve())
                  if p.relative_to(code).as_posix() not in known)
    archive = releases / f"{version}.zip"
    if manifest.get("archive_sha256") and (not archive.is_file() or sha256(archive) != manifest["archive_sha256"]):
        errors.append("archive hash mismatch")
    if manifest.get("database_sha256"):
        database = releases / "data-snapshots" / version / "factory.sqlite3"
        if not database.is_file() or sha256(database) != manifest["database_sha256"]:
            errors.append("database snapshot hash mismatch")
    return {"version": version, "ok": not errors, "source_count": len(known), "errors": errors,
            "customized_files": sorted(customized), "code_path": str(code)}


def launch_environment(root: str | Path, version: str, inherited: dict | None = None) -> tuple[Path, dict]:
    root = Path(root).resolve()
    env = dict(os.environ if inherited is None else inherited)
    # Secrets are passed through environment only. A release never receives an .env file.
    for key, value in read_dotenv(root / ".env").items():
        if key in ENV_KEYS and key not in env:
            env[key] = value
    if version == "final":
        code = root
        env["FCF_RELEASE_VERSION"] = RELEASE
    else:
        version_name(version)
        code = release_directory(root) / version
        data = release_directory(root) / "release-data" / version
        if not data.resolve().is_relative_to(release_directory(root).resolve()):
            raise ValueError("回退数据目录越界")
        isolated = {"CACHE_DIR": data / "cache", "OUTPUT_DIR": data / "output",
                    "DATABASE_PATH": data / "output" / "factory.sqlite3"}
        for path in isolated.values():
            if not path.resolve().is_relative_to(data.resolve()):
                raise ValueError("回退数据目录包含指向外部的链接")
        env.update({**{key: str(path) for key, path in isolated.items()}, "FCF_RELEASE_VERSION": version})
    profile_version = RELEASE if version == "final" else version
    if profile_version.endswith("-cpu"):
        # The same interpreter can run both profiles. Explicit CPU settings must
        # outrank CUDA values inherited from the primary project's .env.
        env.update(DEVICE="cpu", COMPUTE_TYPE="int8", FCF_RENDER_ENCODER="libx264")
    elif profile_version.endswith("-gpu"):
        env.update(DEVICE="cuda", COMPUTE_TYPE="int8_float16", FCF_RENDER_ENCODER="auto")
    env["PYTHONUTF8"] = "1"
    env["FCF_PRIMARY_ROOT"] = str(root)
    env["FCF_CODE_ROOT"] = str(code)
    return code, env


def current_environment(root, profile="gpu", inherited=None):
    """Current source for both devices; historical snapshots stay explicit opt-ins."""
    if profile not in ("cpu", "gpu"):
        raise ValueError("运行设备应为 cpu 或 gpu")
    root = Path(root).resolve()
    code, env = launch_environment(root, "final", inherited)
    env["FCF_PROFILE"] = profile
    env["FCF_RELEASE_VERSION"] = RELEASE.removesuffix("-gpu") + "-" + profile
    if profile == "cpu":
        # Reuse the most recent existing CPU database without copying or modifying it.
        base = release_directory(root) / "release-data"
        candidates = list(base.glob("v*-cpu/output/factory.sqlite3")) if base.is_dir() else []
        candidates = [p for p in candidates if re.fullmatch(r"v\d+\.\d+(?:\.\d+)?-cpu", p.parents[1].name)]
        candidates.sort(key=lambda p: tuple(map(int, p.parents[1].name[1:-4].split("."))), reverse=True)
        data = candidates[0].parents[1] if candidates else root / "runtime-cpu"
        if not data.resolve().is_relative_to(root.parent):
            raise ValueError("CPU 数据目录越界")
        env.update(DEVICE="cpu", COMPUTE_TYPE="int8", FCF_RENDER_ENCODER="libx264",
                   OUTPUT_DIR=str(data/"output"), CACHE_DIR=str(data/"cache"),
                   DATABASE_PATH=str(data/"output/factory.sqlite3"))
    return code, env
