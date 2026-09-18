"""Shared application limits and lossless JSON input partitioning."""

import json

MODEL_INPUT_CHAR_LIMIT = 120_000
OUTPUT_TOKEN_MIN = 128
OUTPUT_TOKEN_MAX = 8192
ORIGINAL_LIMIT = 30
VARIANT_LIMIT = 3


def input_size(value):
    return len(json.dumps(value, ensure_ascii=False))


def require_input_budget(context):
    size = input_size(context)
    if size > MODEL_INPUT_CHAR_LIMIT:
        raise ValueError(f'本次资料共 {size} 字符，超过单次输入预算 {MODEL_INPUT_CHAR_LIMIT}；'
                         '请减少本次题量或整理过长的原文。未截断资料，也未调用模型。')


def split_inputs(base, field, items, *, max_items=None, output_fits=None):
    """Never split an individual question or silently omit an item."""
    batches, current = [], []
    require_input_budget({**base, field: []})
    for item in items:
        proposed = [*current, item]
        fits = (input_size({**base, field: proposed}) <= MODEL_INPUT_CHAR_LIMIT
                and (max_items is None or len(proposed) <= max_items)
                and (output_fits is None or output_fits(proposed)))
        if not fits and current:
            batches.append({**base, field: current})
            current = []
        if not current:
            require_input_budget({**base, field: [item]})
            if output_fits is not None and not output_fits([item]):
                raise ValueError('输出预算不足以返回一个题目标识，请提高本环节的输出上限。')
        current.append(item)
    if current:
        batches.append({**base, field: current})
    return batches
