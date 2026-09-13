"""Archive referenced material by content hash; old versions keep their own files."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from app.adapters.lark_cli import LarkCliError, LarkCliPermissionError
from app.storage.ids import new_id

_downloads = BoundedSemaphore(4)


def collect_materials(connection, source, blocks, drafts, client):
    referenced = {key for draft in drafts for key in (*draft.prompt_block_ids, *draft.reference_block_ids)}
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    root = Path(location).parent / "media" if location else None
    selected = [block for block in blocks if block['block_id'] in referenced and ('image' in block or 'sheet' in block)]
    def read(block):
        with _downloads:
            return _collect_one(block, root, source, client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(read, selected))
    return {item['block_id']: item for item in items}


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
            text = _read_sheet(client, token, source["identity"])
            if text != _read_sheet(client, token, source["identity"]):
                raise LarkCliError("sheet changed while reading")
            data, extension = text.encode("utf-8"), ".txt"
            item["text"] = text
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
    return cells["annotated_csv"]


def _column_name(number):
    name = ""
    while number:
        number, digit = divmod(number - 1, 26)
        name = chr(65 + digit) + name
    return name


def apply_materials(draft, materials):
    selected = []
    prompt, reference = draft.prompt, draft.reference_text or ""
    for role, ids in (("prompt", draft.prompt_block_ids), ("reference", draft.reference_block_ids)):
        for block_id in ids:
            if block_id not in materials:
                continue
            item = {**materials[block_id], "role": role}
            selected.append(item)
            if item["kind"] == "sheet" and item["status"] == "complete":
                if role == "prompt":
                    prompt += "\n" + item["text"]
                else:
                    reference += "\n" + item["text"]
    incomplete = any(item["status"] != "complete" for item in selected)
    status = "media_required" if incomplete else "complete"
    if draft.material_status == 'incomplete_reference':
        status = 'incomplete_reference'
    if not selected and draft.material_status == "media_required":
        status = "media_required"
    return replace(draft, prompt=prompt, reference_text=reference or None,
                   material_status=status, materials=tuple(selected))


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
