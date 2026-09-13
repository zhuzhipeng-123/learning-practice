import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory

from app.storage.database import backup_database
from app.storage.migrations import CURRENT_VERSION


class ExportError(RuntimeError):
    """An export or restore verification failed."""


FACT_TABLES = ("question", "question_version", "task", "attempt", "evaluation", "review_round", "valid_review_pass",
               "reflection", "interview_session", "interview_turn", "interview_derivation", "free_practice_batch", "reference_correction",
               "reference_verification", "reference_correction_history")


def export_learning_data(connection, destination: Path, media_directory: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent, prefix="export-") as temporary:
        database_path = Path(temporary) / "learning.db"
        backup_database(connection, database_path)
        media = _media_manifest(media_directory)
        with closing(sqlite3.connect(database_path)) as backup:
            _validate_required_media(backup, media)
            manifest = {"schema_version": backup.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
                        "database_sha256": _file_hash(database_path), "media": media,
                        "counts": {table: backup.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in FACT_TABLES}}
        staged = Path(temporary) / "export.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database_path, "learning.db")
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for relative_path, expected_hash in media.items():
                file = media_directory / relative_path
                data = file.read_bytes()
                if hashlib.sha256(data).hexdigest() != expected_hash:
                    raise ExportError("media changed during export; retry")
                archive.writestr(f"media/{relative_path}", data)
        staged.replace(destination)
    return destination


def verify_export(archive_path: Path, extraction_directory: Path) -> dict[str, int]:
    extraction_directory.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(dir=extraction_directory, prefix="verify-") as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(archive_path) as archive:
                _reject_unsafe_paths(archive)
                names = set(archive.namelist())
                if not {"learning.db", "manifest.json"} <= names:
                    raise ExportError("archive is missing database or manifest")
                manifest = json.loads(archive.read("manifest.json"))
                _validate_manifest(manifest, names)
                archive.extractall(root)
            database_path = root / "learning.db"
            if manifest.get("database_sha256") and _file_hash(database_path) != manifest["database_sha256"]:
                raise ExportError("database hash mismatch")
            with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as restored:
                if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ExportError("database integrity check failed")
                if restored.execute("PRAGMA foreign_key_check").fetchall():
                    raise ExportError("restored database has foreign key violations")
                version = restored.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                if version != manifest["schema_version"] or not 1 <= version <= CURRENT_VERSION:
                    raise ExportError("unsupported or mismatched schema version")
                for table, expected in manifest.get("counts", {}).items():
                    if table not in FACT_TABLES or restored.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != expected:
                        raise ExportError("learning record counts do not match the manifest")
                count = restored.execute("SELECT count(*) FROM question").fetchone()[0]
                _validate_required_media(restored, manifest['media'])
            for relative_path, expected in manifest["media"].items():
                if _file_hash(root / "media" / relative_path) != expected:
                    raise ExportError("media hash mismatch")
            return {"question_count": count, "media_count": len(manifest["media"])}
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, KeyError, TypeError) as error:
        raise ExportError("archive could not be verified: " + type(error).__name__) from error


def _validate_manifest(manifest, names):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("media"), dict):
        raise ExportError("invalid manifest")
    for relative_path, digest in manifest["media"].items():
        if not _safe_path(relative_path) or not isinstance(digest, str):
            raise ExportError("invalid media manifest entry")
        if f"media/{relative_path}" not in names:
            raise ExportError("archive is missing referenced media")


def _validate_required_media(connection, media):
    """Check the frozen database's full closure, including retired versions."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    groups = []
    if 'version_resources' in tables:
        groups.extend(json.loads(row[0]) for row in connection.execute('SELECT materials_json FROM version_resources'))
    if 'parser_candidate' in tables:
        groups.extend(json.loads(row[0]).get('materials', []) for row in connection.execute('SELECT draft_json FROM parser_candidate'))
    for items in groups:
        for item in items:
            if item.get('status') != 'complete' or item.get('kind') not in {'media', 'sheet'}:
                continue
            path, digest = item.get('path'), item.get('sha256')
            if not _safe_path(path) or path not in media:
                raise ExportError('数据库引用的已归档附件缺失，备份未通过完整性检查')
            if not digest or media[path] != digest:
                raise ExportError('数据库引用的附件哈希不匹配，备份未通过完整性检查')


def _media_manifest(media_directory: Path) -> dict[str, str]:
    if not media_directory.exists():
        return {}
    return {path.relative_to(media_directory).as_posix(): _file_hash(path)
            for path in media_directory.rglob("*") if path.is_file() and not path.is_symlink()}


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(name):
    return (isinstance(name, str) and bool(name) and "\\" not in name and ":" not in name
            and not PurePosixPath(name).is_absolute() and not PureWindowsPath(name).is_absolute()
            and ".." not in PurePosixPath(name).parts)


def _reject_unsafe_paths(archive):
    entries = archive.infolist()
    if len(entries) > 10000 or sum(item.file_size for item in entries) > 1_000_000_000:
        raise ExportError("archive exceeds restore limits")
    if len({item.filename for item in entries}) != len(entries):
        raise ExportError("archive contains duplicate paths")
    for item in entries:
        name = item.filename
        if not _safe_path(name) or name.startswith(".env") or "secret" in name.lower():
            raise ExportError("archive contains an unsafe or forbidden path")
