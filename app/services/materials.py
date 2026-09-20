"""Archive referenced material by content hash; old versions keep their own files."""

import csv
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from app.adapters.lark_cli import LarkCliError, LarkCliPermissionError
from app.parsers.block_tree import ordered_blocks
from app.parsers.docx import block_text
from app.storage.ids import new_id

_downloads = BoundedSemaphore(4)


def collect_materials(connection, source, blocks, drafts, client):
    referenced = {key for draft in drafts for key in (*draft.prompt_block_ids, *draft.reference_block_ids)}
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    root = Path(location).parent / "media" if location else None
    ordered = ordered_blocks(blocks)
    by_id = {block['block_id']: block for block in ordered}
    tables = [block for block in ordered if block['block_id'] in referenced and block.get('block_type') == 31]
    nested = {key for table in tables for key in _descendant_ids(table, by_id)}
    selected = [block for block in ordered if block['block_id'] in referenced | nested
                and ('image' in block or 'sheet' in block)]
    def read(block):
        with _downloads:
            return _collect_one(block, root, source, client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(read, selected))
    materials = {item['block_id']: item for item in items}
    for table in tables:
        item = _collect_table(table, by_id, materials)
        materials[item['block_id']] = item
    return materials


def _collect_one(block, root, source, client):
    block_id = block['block_id']
    kind = "media" if "image" in block else "sheet"
    token = str(block.get("image", block.get("sheet", {})).get("token", ""))
    item = {"kind": kind, "token": token, "block_id": block_id, "status": "pending"}
    try:
        if not token or root is None:
            raise LarkCliError("material token or persistent storage is missing")
        root.mkdir(parents=True, exist_ok=True)
        if kind == "media":
            data, extension = _read_image(client, token, root, source["identity"])
        else:
            text, structure = _read_sheet(client, token, source["identity"])
            if (text, structure) != _read_sheet(client, token, source["identity"]):
                raise LarkCliError("sheet changed while reading")
            data, extension = text.encode("utf-8"), ".csv"
            item["text"] = text
            item['structure'] = structure
        digest = hashlib.sha256(data).hexdigest()
        destination = root / (digest + extension)
        if not destination.exists():
            destination.write_bytes(data)
        item.update(status="complete", sha256=digest, path=destination.name)
    except LarkCliPermissionError:
        scope = "docs:document.media:download" if kind == "media" else "sheets:spreadsheet:read"
        item["error"] = f"飞书权限不足，请检查应用权限 {scope} 和文档访问权限"
    except (LarkCliError, OSError, ValueError, KeyError, TypeError) as error:
        item["error"] = type(error).__name__ + ": material could not be archived"
    return item


def _read_image(client, token, root, identity):
    with TemporaryDirectory(dir=root, prefix="download-") as temporary:
        target = Path(temporary) / "image.bin"
        client.download_media(token, target, identity)
        if target.stat().st_size > 32_000_000:
            raise LarkCliError("image exceeded 32 MB")
        data = target.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return data, ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return data, ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data, ".webp"
    raise LarkCliError("unsupported image format")


def _read_sheet(client, token, identity):
    spreadsheet, separator, sheet_id = token.partition("_")
    if not separator:
        raise LarkCliError("embedded sheet has no explicit sheet ID")
    common = ["--spreadsheet-token", spreadsheet, "--as", identity]
    info = client.run_read(["sheets", "+workbook-info", *common]).data
    sheet = next((item for item in info["sheets"] if item["sheet_id"] == sheet_id), None)
    if sheet is None:
        raise LarkCliError("embedded sheet was not found")
    rows, columns = int(sheet["row_count"]), int(sheet["column_count"])
    if not 0 < rows * columns <= 200_000:
        raise LarkCliError("sheet exceeds the supported archive size")
    cells = client.run_read(["sheets", "+csv-get", *common, "--sheet-id", sheet_id,
                             "--range", f"A1:{_column_name(columns)}{rows}", "--max-chars", "4000000"]).data
    if cells.get("has_more") is not False or not isinstance(cells.get("annotated_csv"), str):
        raise LarkCliError("sheet read was incomplete")
    text = cells["annotated_csv"]
    matrix = _parse_csv(text)
    if len(matrix) != rows or any(len(row) != columns for row in matrix):
        raise LarkCliError("sheet dimensions do not match the complete range")
    return text, {'schema': 'grid-v1', 'rows': rows, 'columns': columns, 'cells': matrix}


def _column_name(number):
    name = ""
    while number:
        number, digit = divmod(number - 1, 26)
        name = chr(65 + digit) + name
    return name


def apply_materials(draft, materials, blocks=None):
    selected = []
    prompt, reference = draft.prompt, draft.reference_text or ""
    for role, ids in (("prompt", draft.prompt_block_ids), ("reference", draft.reference_block_ids)):
        for block_id in ids:
            if block_id not in materials:
                continue
            item = {**normalize_material(materials[block_id]), "role": role}
            selected.append(item)
            if item["kind"] in {'sheet', 'table'} and item["status"] == "complete":
                text = item.get('text', '')
                if item['kind'] == 'table':
                    text = '[结构化表格]\n' + json.dumps(
                        item.get('structure', {}), ensure_ascii=False, sort_keys=True,
                    )
                if role == "prompt":
                    prompt += "\n" + text
                else:
                    reference += "\n" + text
    if blocks is not None:
        selected.extend(_ordered_content(draft, blocks))
    incomplete = any(item.get("status") != "complete" for item in material_closure(selected))
    status = "media_required" if incomplete else "complete"
    if draft.material_status == 'incomplete_reference':
        status = 'incomplete_reference'
    if not selected and draft.material_status == "media_required":
        status = "media_required"
    return replace(draft, prompt=prompt, reference_text=reference or None,
                   material_status=status, materials=tuple(selected))


def material_closure(items):
    """Yield nested material records with the owning prompt/reference role."""
    pending = [(item, None) for item in items if isinstance(item, dict)]
    seen = set()
    while pending:
        item, inherited_role = pending.pop(0)
        role = item.get('role') or inherited_role
        identity = (item.get('kind'), item.get('block_id'), item.get('path'), item.get('sha256'), role)
        if identity in seen:
            continue
        seen.add(identity)
        value = dict(item)
        if role:
            value['role'] = role
        yield value
        attachments = item.get('attachments', [])
        if isinstance(attachments, list):
            pending.extend((child, role) for child in attachments if isinstance(child, dict))


def materials_for_role(items, role):
    """Return only top-level records explicitly owned by one visible region.

    Keeping the original nesting is important for table-cell attachments.  Records
    without an explicit role are intentionally omitted: guessing their ownership
    could leak a reference attachment on a question-only endpoint.
    """
    if role not in {'prompt', 'reference'}:
        raise ValueError('unsupported material role')
    return [dict(item) for item in items
            if isinstance(item, dict) and item.get('role') == role]


def _ordered_content(draft, blocks):
    by_id = {str(block['block_id']): block for block in ordered_blocks(blocks)}
    records = []
    for role, block_ids in (('prompt', draft.prompt_block_ids), ('reference', draft.reference_block_ids)):
        nodes = _ordered_nodes(block_ids, by_id)
        if nodes:
            encoded = json.dumps(nodes, ensure_ascii=False, sort_keys=True).encode('utf-8')
            records.append({'kind': 'ordered_content', 'role': role, 'status': 'complete',
                            'schema': 'ordered-v1', 'nodes': nodes,
                            'sha256': hashlib.sha256(encoded).hexdigest()})
    return records


def _ordered_nodes(block_ids, by_id):
    covered_by_table = set()
    for block_id in block_ids:
        block = by_id.get(str(block_id))
        if block and block.get('block_type') == 31:
            covered_by_table.update(_descendant_ids(block, by_id))
    nodes = []
    for block_id in block_ids:
        key = str(block_id)
        if key in covered_by_table:
            continue
        block = by_id.get(key)
        if block is None:
            continue
        kind = block.get('block_type')
        if kind == 27 or 'image' in block:
            nodes.append({'kind': 'media', 'block_id': key})
        elif kind == 31:
            nodes.append({'kind': 'table', 'block_id': key})
        elif 'sheet' in block:
            nodes.append({'kind': 'sheet', 'block_id': key})
        elif kind == 14:
            code = block.get('code') if isinstance(block.get('code'), dict) else {}
            style = code.get('style') if isinstance(code.get('style'), dict) else {}
            nodes.append({'kind': 'code', 'block_id': key, 'text': _rich_text(code),
                          'language': style.get('language') or code.get('language') or ''})
        else:
            text = block_text(block)
            if text:
                nodes.append({'kind': 'text', 'block_id': key, 'text': text})
    return nodes


def _rich_text(content):
    pieces = []
    for element in content.get('elements', []):
        value = element.get('text_run') or element.get('equation') or {}
        pieces.append(str(value.get('content') or ''))
    return ''.join(pieces).rstrip('\r\n')


def normalize_material(item):
    """Read legacy text-only sheets and current structured materials uniformly."""
    value = dict(item)
    if value.get('kind') == 'sheet' and not isinstance(value.get('structure'), dict):
        matrix = _parse_csv(value.get('text', ''))
        columns = max((len(row) for row in matrix), default=0)
        matrix = [row + [''] * (columns - len(row)) for row in matrix]
        value['structure'] = {'schema': 'grid-v1', 'rows': len(matrix),
                              'columns': columns, 'cells': matrix}
    return value


def _parse_csv(text):
    if not isinstance(text, str):
        raise TypeError('sheet archive is not text')
    return [list(row) for row in csv.reader(io.StringIO(text, newline=''))]


def _descendant_ids(block, by_id):
    result = []
    pending = list(block.get('children', []))
    while pending:
        key = pending.pop(0)
        if key in result or key not in by_id:
            continue
        result.append(key)
        pending[0:0] = by_id[key].get('children', [])
    return result


def _collect_table(table, by_id, resources):
    table_data = table.get('table') if isinstance(table.get('table'), dict) else {}
    properties = table_data.get('property') if isinstance(table_data.get('property'), dict) else table_data
    rows = _positive_int(properties.get('row_size') or properties.get('rows'))
    columns = _positive_int(properties.get('column_size') or properties.get('columns'))
    cell_ids = [key for key in table.get('children', []) if by_id.get(key, {}).get('block_type') == 32]
    errors = []
    if not rows or not columns or len(cell_ids) != rows * columns:
        errors.append('表格行列或单元格数量不完整')
    cells = []
    attachments = []
    for index, cell_id in enumerate(cell_ids):
        content, cell_attachments, gaps = _cell_content(by_id[cell_id], by_id, resources)
        attachments.extend(cell_attachments)
        errors.extend(gaps)
        cells.append({'row': index // columns if columns else 0, 'column': index % columns if columns else index,
                      'content': content})
    structure = {'schema': 'grid-v1', 'rows': rows, 'columns': columns, 'cells': cells,
                 'merge_info': properties.get('merge_info', table_data.get('merge_info', []))}
    encoded = json.dumps(structure, ensure_ascii=False, sort_keys=True).encode('utf-8')
    text = '\n'.join('\t'.join(_cell_plain_text(cell) for cell in cells[row * columns:(row + 1) * columns])
                     for row in range(rows)) if rows and columns else ''
    item = {'kind': 'table', 'token': '', 'block_id': table['block_id'],
            'status': 'incomplete' if errors else 'complete', 'sha256': hashlib.sha256(encoded).hexdigest(),
            'structure': structure, 'text': text, 'attachments': attachments}
    if errors:
        item['error'] = '；'.join(dict.fromkeys(errors))
    return item


def _cell_content(cell, by_id, resources):
    content, attachments, gaps = [], [], []
    for key in _descendant_ids(cell, by_id):
        block = by_id[key]
        text = block_text(block)
        if text:
            node = {'kind': 'code' if block.get('block_type') == 14 else 'text',
                    'block_id': key, 'text': text}
            if block.get('block_type') == 14:
                code = block.get('code') if isinstance(block.get('code'), dict) else {}
                style = code.get('style') if isinstance(code.get('style'), dict) else {}
                node['text'] = _rich_text(code)
                node['language'] = style.get('language') or code.get('language') or ''
            content.append(node)
        elif 'image' in block or 'sheet' in block:
            resource = resources.get(key)
            if resource is None:
                gaps.append(f'单元格附件 {key} 未归档')
                continue
            normalized = normalize_material(resource)
            attachments.append(normalized)
            content.append({'kind': normalized['kind'], 'block_id': key})
            if normalized.get('status') != 'complete':
                gaps.append(f'单元格附件 {key} 未完整归档')
        elif block.get('block_type') not in {1, 2, 19, 22, 24, 25, 31, 32, 34}:
            gaps.append(f'单元格块 {key} 暂不支持')
    return content, attachments, gaps


def _cell_plain_text(cell):
    return '\n'.join(node['text'] for node in cell['content'] if node['kind'] in {'text', 'code'})


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def store_dependencies(connection, source_id, materials):
    connection.execute("UPDATE source_dependency SET status='obsolete' WHERE source_id=?", (source_id,))
    for item in materials.values():
        subresource = item["block_id"]
        connection.execute(
            "INSERT INTO source_dependency VALUES (?,?,?,?,?,NULL,?,?,?) "
            "ON CONFLICT(source_id,kind,token,subresource_id) DO UPDATE SET "
            "content_hash=excluded.content_hash,status=excluded.status,last_error=excluded.last_error",
            (new_id("dependency"), source_id, item["kind"], item["token"], subresource,
             item.get("sha256"), item["status"], item.get("error")),
        )
