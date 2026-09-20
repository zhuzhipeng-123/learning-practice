"""Stable, source-qualified theory module scopes for daily practice."""

import hashlib
import json

from app.parsers.block_tree import ordered_blocks
from app.parsers.docx import block_text, classify_theory_headings, heading_level
from app.services.wiki_sources import source_prefix

ROOT_ANCHOR = "__source__"


def module_id(source_id, anchor):
    return f"{source_id}::{anchor}"


def theory_catalog(connection):
    nodes, question_modules, snapshots = [], {}, []
    rows = connection.execute(
        "SELECT s.id source_id,s.document_id,s.last_error,ss.id snapshot_id,ss.complete,ss.blocks_json "
        "FROM source s JOIN source_sync_state st ON st.source_id=s.id "
        "JOIN source_snapshot ss ON ss.id=st.snapshot_id "
        "WHERE s.enabled=1 AND (s.question_type='theory' OR EXISTS(SELECT 1 FROM source_binding b "
        "JOIN question q ON q.id=b.question_id WHERE b.source_id=s.id AND q.question_type='theory')) ORDER BY s.id"
    ).fetchall()
    for row in rows:
        source_nodes, mapping = _source_catalog(connection, row)
        nodes.extend(source_nodes)
        question_modules.update(mapping)
        snapshots.append({"source_id": row["source_id"], "snapshot_id": row["snapshot_id"]})
    legacy_paths = {}
    for row in connection.execute(
        "SELECT b.source_id,b.main_anchor_block_id,v.category_path FROM source_binding b "
        "JOIN question q ON q.id=b.question_id JOIN question_version v ON v.id=q.current_version_id "
        "WHERE q.question_type='theory' AND b.active=1"
    ):
        node_id = question_modules.get(module_id(row['source_id'], row['main_anchor_block_id']))
        if node_id and row['category_path']:
            legacy_paths.setdefault(row['category_path'], []).append(node_id)
    legacy_paths = {path: sorted(set(ids)) for path, ids in legacy_paths.items()}
    value = {"nodes": nodes, "question_modules": question_modules, "legacy_paths": legacy_paths,
             "snapshots": snapshots,
             "complete": all(row["complete"] and not row["last_error"] for row in rows)}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {**value, "version": hashlib.sha256(raw).hexdigest()}


def _source_catalog(connection, row):
    source_id = row["source_id"]
    blocks = ordered_blocks(json.loads(row["blocks_json"]))
    roles = classify_theory_headings(blocks).roles
    root_id = module_id(source_id, ROOT_ANCHOR)
    root_label = source_prefix(connection, source_id) or row["document_id"]
    nodes = [{"id": root_id, "source_id": source_id, "anchor": ROOT_ANCHOR,
              "parent_id": None, "label": root_label, "path": root_label}]
    stack, question_modules = [], {}
    paths = {root_id: root_label}
    for block in blocks:
        level = heading_level(block)
        if level is None:
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        anchor = str(block["block_id"])
        role = roles.get(anchor)
        if role == "module":
            parent_id = stack[-1][1] if stack else root_id
            node_id = module_id(source_id, anchor)
            label = block_text(block)
            path = " > ".join(filter(None, (paths[parent_id], label)))
            nodes.append({"id": node_id, "source_id": source_id, "anchor": anchor,
                          "parent_id": parent_id, "label": label, "path": path})
            paths[node_id] = path
            stack.append((level, node_id))
        elif role in {"question", "ambiguous_question"}:
            question_modules[module_id(source_id, anchor)] = stack[-1][1] if stack else root_id
    for binding in connection.execute(
        "SELECT main_anchor_block_id FROM source_binding WHERE source_id=? AND active=1",
        (source_id,),
    ):
        question_modules.setdefault(module_id(source_id, binding['main_anchor_block_id']), root_id)
    return nodes, question_modules


def resolve_scope(connection, selected_ids, catalog_version, theory_target, previous=None):
    catalog = theory_catalog(connection)
    if catalog_version and catalog_version != catalog["version"]:
        raise ValueError("八股目录已变化，请重新查看模块范围后再确认")
    previous = previous or {}
    explicit_selection = selected_ids is not None
    if selected_ids is None:
        selected_ids = previous.get("selected_ids") if previous.get('selection_explicit', True) else None
    if selected_ids is None:
        selected_ids = [node["id"] for node in catalog["nodes"] if node["parent_id"] is None]
    known = {node["id"] for node in catalog["nodes"]}
    missing = set(selected_ids) - known
    if missing and catalog["complete"]:
        raise ValueError("已选八股模块已删除或身份不明，请重新选择范围")
    if missing:
        old_nodes = {node["id"]: node for node in previous.get("nodes", [])}
        if not missing <= old_nodes.keys():
            raise ValueError("部分目录不可用且无法确认原模块，请保留当前题单并稍后重试")
        catalog["nodes"].extend(old_nodes[item] for item in sorted(missing))
        catalog["question_modules"].update(previous.get("question_modules", {}))
    selected = _remove_descendants(list(dict.fromkeys(selected_ids)), catalog["nodes"])
    if theory_target > 0 and not selected and (explicit_selection or catalog['nodes']):
        raise ValueError("八股题量大于 0 时至少选择一个模块")
    return {"selected_ids": selected, "selection_explicit": explicit_selection or
            previous.get('selection_explicit', False), **catalog}


def canonical_quotas(quotas, scope):
    nodes = scope.get("nodes", [])
    by_id = {node["id"]: node for node in nodes}
    resolved = {}
    for key, count in quotas.items():
        if key in by_id:
            matches = [key]
        else:
            matches = [node["id"] for node in nodes if node["path"] == key or
                       node["path"].split(" > ", 1)[-1] == key]
            if not matches:
                matches = scope.get('legacy_paths', {}).get(key, [])
        if len(matches) != 1:
            raise ValueError("旧模块路径无法唯一映射，请重新选择八股范围")
        resolved[matches[0]] = count
    allowed = allowed_modules(scope)
    if any(key not in allowed for key in resolved):
        raise ValueError("模块配额位于已排除范围，请调整后重试")
    return resolved


def allowed_modules(scope):
    parents = {node["id"]: node["parent_id"] for node in scope.get("nodes", [])}
    selected = set(scope.get("selected_ids", []))
    return {node_id for node_id in parents if _has_ancestor(node_id, selected, parents)}


def question_allowed(question_id, source_id, anchor, scope):
    key = module_id(source_id, anchor)
    module = scope.get("question_modules", {}).get(key)
    return module in allowed_modules(scope) if module else False


def is_descendant(left, right, scope):
    parents = {node["id"]: node["parent_id"] for node in scope.get("nodes", [])}
    return left != right and _has_ancestor(left, {right}, parents)


def _remove_descendants(selected, nodes):
    parents = {node["id"]: node["parent_id"] for node in nodes}
    chosen = set(selected)
    return [item for item in selected if not _has_ancestor(parents.get(item), chosen, parents)]


def _has_ancestor(node_id, selected, parents):
    while node_id:
        if node_id in selected:
            return True
        node_id = parents.get(node_id)
    return False
