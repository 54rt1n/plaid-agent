import os
import re
import subprocess
import tarfile
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def test_source_distribution_builds_website_without_repository_assets(tmp_path):
    source = Path(__file__).resolve().parents[1] / "packages/plaid-link"
    environment = {**os.environ, "NPM_CONFIG_OFFLINE": "true"}

    def build(package, target, output):
        subprocess.run(
            [
                "uv",
                "build",
                "--offline",
                "--no-sources",
                str(package),
                target,
                "--out-dir",
                str(output),
            ],
            env=environment,
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )

    build(source, "--sdist", tmp_path / "source")
    archive = next((tmp_path / "source").glob("*.tar.gz"))
    with tarfile.open(archive) as distribution:
        names = distribution.getnames()
        assert any(name.endswith("/frontend/src/App.svelte") for name in names)
        assert any(name.endswith("/frontend/package-lock.json") for name in names)
        assert any(name.endswith("/frontend/LICENSE") for name in names)
        assert any(name.endswith("/LICENSE") and "/frontend/" not in name for name in names)
        assert not any("/static/" in name or "/node_modules/" in name for name in names)
        distribution.extractall(tmp_path / "unpacked", filter="data")
    package = next((tmp_path / "unpacked").iterdir())
    build(package, "--wheel", tmp_path / "wheel")
    with ZipFile(next((tmp_path / "wheel").glob("*.whl"))) as wheel:
        metadata_path = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        assert metadata["License-Expression"] == "LicenseRef-PolyForm-Internal-Use-1.0.0"
        assert metadata["License-File"] == "LICENSE"
        assert "THIRD_PARTY_NOTICES" in metadata.get_all("License-File")
        license_path = metadata_path.removesuffix("METADATA") + "licenses/LICENSE"
        assert wheel.read(license_path) == (source / "LICENSE").read_bytes()
        notices_path = metadata_path.removesuffix("METADATA") + "licenses/THIRD_PARTY_NOTICES"
        assert wheel.read(notices_path) == (source / "THIRD_PARTY_NOTICES").read_bytes()
        index = wheel.read("plaid_link/static/index.html").decode()
        references = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index)
        assert references and any(name.endswith(".js") for name in references)
        for reference in references:
            assert wheel.read("plaid_link/static" + reference)
        assert not any(
            "node_modules" in name or name.startswith("frontend/") for name in wheel.namelist()
        )
