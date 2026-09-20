import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory

from app.services.materials import material_closure, normalize_material
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
        _validate_material_structure(items)
        for item in material_closure(items):
            if item.get('status') != 'complete' or item.get('kind') not in {'media', 'sheet'}:
                continue
            path, digest = item.get('path'), item.get('sha256')
            if not _safe_path(path) or path not in media:
                raise ExportError('数据库引用的已归档附件缺失，备份未通过完整性检查')
            if not digest or media[path] != digest:
                raise ExportError('数据库引用的附件哈希不匹配，备份未通过完整性检查')


def _validate_material_structure(items):
    """Reject broken links inside ordered-v1/table material graphs."""
    if not isinstance(items, list):
        raise ExportError('结构化素材不是有效列表，备份未通过完整性检查')
    records = list(material_closure(items))
    index = {}
    for item in records:
        block_id, role = item.get('block_id'), item.get('role')
        if not block_id:
            continue
        key = (role, str(block_id))
        if key in index and index[key].get('kind') != item.get('kind'):
            raise ExportError('结构化素材存在冲突的块标识，备份未通过完整性检查')
        index[key] = item
    for item in records:
        if item.get('kind') == 'ordered_content':
            _validate_ordered(item, index)
        if item.get('kind') == 'table':
            _validate_table_hash(item)
            _validate_grid(item, index)
        if item.get('kind') == 'sheet' and item.get('status') == 'complete':
            normalized = normalize_material(item)
            _validate_grid(normalized, index)
            _validate_sheet_archive(normalized)


def _validate_ordered(item, index):
    role, nodes = item.get('role'), item.get('nodes')
    if role not in {'prompt', 'reference'} or not isinstance(nodes, list):
        raise ExportError('有序素材缺少题干/参考归属，备份未通过完整性检查')
    encoded = json.dumps(nodes, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if item.get('sha256') != hashlib.sha256(encoded).hexdigest():
        raise ExportError('有序素材哈希不匹配，备份未通过完整性检查')
    for node in nodes:
        if not isinstance(node, dict) or node.get('kind') not in {'text', 'code', 'media', 'sheet', 'table'}:
            raise ExportError('有序素材包含不支持的节点，备份未通过完整性检查')
        if node['kind'] in {'text', 'code'}:
            if not isinstance(node.get('text'), str):
                raise ExportError('有序文字节点不完整，备份未通过完整性检查')
            continue
        resource = index.get((role, str(node.get('block_id'))))
        if resource is None or resource.get('kind') != node['kind']:
            raise ExportError('有序素材引用的附件不存在，备份未通过完整性检查')


def _validate_table_hash(item):
    if item.get('status') != 'complete':
        return
    structure = item.get('structure')
    if not isinstance(structure, dict):
        raise ExportError('表格结构不完整，备份未通过完整性检查')
    encoded = json.dumps(structure, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if item.get('sha256') != hashlib.sha256(encoded).hexdigest():
        raise ExportError('表格结构哈希不匹配，备份未通过完整性检查')


def _validate_sheet_archive(item):
    text = item.get('text')
    if not isinstance(text, str) or item.get('sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest():
        raise ExportError('Sheets 文本哈希不匹配，备份未通过完整性检查')
    expected = normalize_material({'kind': 'sheet', 'text': text})['structure']
    structure = item.get('structure')
    if any(structure.get(key) != expected[key] for key in ('schema', 'rows', 'columns', 'cells')):
        raise ExportError('Sheets 结构与归档 CSV 不一致，备份未通过完整性检查')


def _validate_grid(item, index):
    structure = item.get('structure')
    if item.get('status') != 'complete' or not isinstance(structure, dict):
        return
    if structure.get('schema') != 'grid-v1':
        raise ExportError('表格结构版本无效，备份未通过完整性检查')
    try:
        rows, columns = int(structure.get('rows')), int(structure.get('columns'))
    except (TypeError, ValueError):
        raise ExportError('表格行列无效，备份未通过完整性检查') from None
    if rows < 0 or columns < 0 or not isinstance(structure.get('cells'), list):
        raise ExportError('表格行列无效，备份未通过完整性检查')
    cells = structure['cells']
    if cells and isinstance(cells[0], list):
        if len(cells) != rows or any(len(row) != columns for row in cells):
            raise ExportError('表格单元格数量不完整，备份未通过完整性检查')
    else:
        coordinates = set()
        for cell in cells:
            if not isinstance(cell, dict):
                raise ExportError('表格单元格结构无效，备份未通过完整性检查')
            coordinate = (cell.get('row'), cell.get('column'))
            if (not all(isinstance(value, int) for value in coordinate)
                    or not 0 <= coordinate[0] < rows or not 0 <= coordinate[1] < columns
                    or coordinate in coordinates):
                raise ExportError('表格单元格坐标无效，备份未通过完整性检查')
            coordinates.add(coordinate)
            for node in cell.get('content', []):
                _validate_cell_node(item, node, index)
        if rows * columns != len(coordinates):
            raise ExportError('表格单元格数量不完整，备份未通过完整性检查')
    for merge in structure.get('merge_info', []):
        if not isinstance(merge, dict):
            raise ExportError('表格合并区域无效，备份未通过完整性检查')
        try:
            row, column = int(merge['row']), int(merge['column'])
            row_span, column_span = int(merge.get('row_span', 1)), int(merge.get('column_span', 1))
        except (KeyError, TypeError, ValueError):
            raise ExportError('表格合并区域无效，备份未通过完整性检查') from None
        if row < 0 or column < 0 or row_span < 1 or column_span < 1 or row + row_span > rows or column + column_span > columns:
            raise ExportError('表格合并区域越界，备份未通过完整性检查')


def _validate_cell_node(table, node, index):
    if not isinstance(node, dict) or node.get('kind') not in {'text', 'code', 'media', 'sheet'}:
        raise ExportError('表格单元格包含不支持的节点，备份未通过完整性检查')
    if node['kind'] in {'text', 'code'}:
        if not isinstance(node.get('text'), str):
            raise ExportError('表格文字节点不完整，备份未通过完整性检查')
        return
    resource = index.get((table.get('role'), str(node.get('block_id'))))
    if resource is None or resource.get('kind') != node['kind']:
        raise ExportError('表格单元格引用的附件不存在，备份未通过完整性检查')


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
