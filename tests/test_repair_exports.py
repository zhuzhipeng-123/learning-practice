import json
import zipfile

import pytest

from app.services.exports import ExportError, export_learning_data, verify_export


@pytest.mark.parametrize("missing", ["learning.db", "media/nested/example.png"])
def test_bad_archive_never_uses_prior_restore_files(database, tmp_path, missing):
    media = tmp_path / "media"
    (media / "nested").mkdir(parents=True)
    (media / "nested/example.png").write_bytes(b"safe")
    archive = tmp_path / "good.zip"
    export_learning_data(database, archive, media)
    restore = tmp_path / "restore"
    assert verify_export(archive, restore)["media_count"] == 1
    with zipfile.ZipFile(archive) as good, zipfile.ZipFile(tmp_path / "bad.zip", "w") as bad:
        manifest = json.loads(good.read("manifest.json"))
        assert "nested/example.png" in manifest["media"]
        for name in good.namelist():
            if name != missing:
                bad.writestr(name, good.read(name))
    with pytest.raises(ExportError):
        verify_export(tmp_path / "bad.zip", restore)
