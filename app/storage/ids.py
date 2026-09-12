import uuid


def new_id(prefix: str) -> str:
    """Create an opaque local identity that never depends on mutable source text."""
    return f"{prefix}_{uuid.uuid4().hex}"
