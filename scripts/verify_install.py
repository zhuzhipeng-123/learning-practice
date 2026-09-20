import json
import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from app.main import app
from app.parsers.p0_samples import load_reviewed_samples

PACKAGED_RESOURCES = {
    "p0_samples": PROJECT_ROOT / "app" / "parsers" / "fixtures" / "p0_questions.json",
    "schema": PROJECT_ROOT / "app" / "storage" / "schema.sql",
    "base_template": PROJECT_ROOT / "app" / "templates" / "base.html",
    "stylesheet": PROJECT_ROOT / "app" / "static" / "app.css",
}


def verify() -> dict[str, object]:
    drafts, candidates = load_reviewed_samples()
    resources = {name: path.is_file() for name, path in PACKAGED_RESOURCES.items()}
    with (
        TemporaryDirectory(prefix="learning-check-") as temporary,
        patch.dict(os.environ, {"LEARNING_DATA_DIR": temporary}),
        TestClient(app) as client,
    ):
        routes = {
            path: client.get(path).status_code
            for path in (
                "/health",
                "/",
                "/sources",
                "/free-practice",
                "/review",
                "/interview",
                "/history",
                "/settings",
                "/docs",
            )
        }
    return {
        "python": sys.version.split()[0],
        "python_supported": sys.version_info[:2] == (3, 12),
        "uv_available": shutil.which("uv") is not None,
        "lark_cli_available": shutil.which("lark-cli") is not None,
        "reviewed_samples": len(drafts),
        "pending_samples": len(candidates),
        "routes": routes,
        "all_routes_ok": all(status == 200 for status in routes.values()),
        "resources": resources,
        "all_resources_present": all(resources.values()),
        "project_root": str(PROJECT_ROOT),
    }


def main() -> None:
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if (
        not result["python_supported"]
        or not result["all_routes_ok"]
        or not result["all_resources_present"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
