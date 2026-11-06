"""Build the Link website for wheels; carry frontend source in source distributions."""

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

FRONTEND_FILES = (
    "src",
    "LICENSE",
    "index.html",
    "package.json",
    "package-lock.json",
    "svelte.config.js",
    "tsconfig.json",
    "vite.config.ts",
)


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        frontend = root / "frontend"
        if not frontend.is_dir():
            frontend = root.parent / "plaid-web"
        if not (frontend / "package-lock.json").is_file():
            raise RuntimeError("Plaid Link package source is missing the frontend and npm lockfile")
        if self.target_name == "sdist":
            for name in FRONTEND_FILES:
                build_data["force_include"][str(frontend / name)] = f"frontend/{name}"
            return
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("Building Plaid Link from source requires Node.js 22.12+ and npm")
        assets = root / "src/plaid_link/static"
        subprocess.run(
            [npm, "ci", "--include=dev", "--no-audit", "--no-fund"], cwd=frontend, check=True
        )
        subprocess.run(
            [npm, "run", "build", "--", "--outDir", str(assets)], cwd=frontend, check=True
        )
        build_data["force_include"][str(assets)] = "plaid_link/static"
