import json
import re


class ModelJSONError(ValueError):
    """A model response cannot be decoded as one complete JSON value."""


def parse_model_json(text):
    if not isinstance(text, str):
        raise ModelJSONError('模型输出格式错误：没有返回文本，未采用结果')
    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ModelJSONError('模型输出格式错误（JSON 引号、转义或结构不合法），未采用结果；请重试') from error


def complete_json(model, messages, max_tokens):
    """Production adapters request JSON mode; simple injected clients remain usable."""
    complete = getattr(model, 'complete_json', None) or model.complete
    return complete(messages, max_tokens=max_tokens)
