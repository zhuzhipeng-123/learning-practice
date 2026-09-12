from datetime import datetime
from zoneinfo import ZoneInfo


def local_today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()
