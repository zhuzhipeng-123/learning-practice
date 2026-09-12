import json
import re


class ModelJSONError(ValueError):
    """A model response cannot be decoded as one complete JSON value."""


def parse_model_json(text):
    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ModelJSONError("模型未返回完整 JSON；请检查输出上限和模型配置") from error
