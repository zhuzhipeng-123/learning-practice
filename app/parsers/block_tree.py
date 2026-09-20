"""Restore document order from child links without changing stored source blocks."""


# Headings inside these layout/content containers describe a cell or an answer
# fragment.  They must not become document-level modules or questions.
CONTENT_CONTAINER_TYPES = {19, 31, 32, 34}


def ordered_blocks(blocks):
    by_id = {block['block_id']: block for block in blocks}
    children = {child for block in blocks for child in block.get('children', [])}
    ordered, visited, visiting = [], set(), set()

    def visit(key, in_content_container=False):
        if key in visiting or len(visiting) >= 128:
            raise ValueError('文档块存在循环或嵌套过深，不能确认解析完整性')
        if key in visited:
            return
        if key not in by_id:
            raise ValueError('文档缺少引用的子块，不能确认解析完整性')
        block = by_id[key]
        visiting.add(key)
        ordered.append({**block, '_content_container': in_content_container,
                        '_reference_container': in_content_container})
        nested = in_content_container or block.get('block_type') in CONTENT_CONTAINER_TYPES
        for child in block.get('children', []):
            visit(child, nested)
        visiting.remove(key)
        visited.add(key)

    for block in blocks:
        if block['block_id'] not in children:
            visit(block['block_id'])
    for block in blocks:
        if block['block_id'] not in visited:
            visit(block['block_id'])
    return ordered
