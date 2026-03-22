"""Hatch build hook: compile the image-proc Rust binary and bundle it into the wheel."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:  # type: ignore[override]
        if self.target_name == "sdist":
            return  # sdist is source-only — no binary needed

        image_proc_dir = Path(__file__).parent / "image-proc"

        # Determine features to enable
        features = []
        if os.environ.get("IMAGE_PROC_FEATURE_RAW"):
            features.append("raw")
        if os.environ.get("IMAGE_PROC_FEATURE_HEIC"):
            features.append("heic")

        cmd = ["cargo", "build", "--release"]
        if features:
            cmd += ["--features", ",".join(features)]

        subprocess.run(cmd, cwd=image_proc_dir, check=True)

        # image-proc.exe on Windows, image-proc elsewhere
        binary_name = "image-proc.exe" if sys.platform == "win32" else "image-proc"
        src = image_proc_dir / "target" / "release" / binary_name

        bin_dir = Path(__file__).parent / "src" / "ouestcharlie_toolkit" / "bin"
        bin_dir.mkdir(exist_ok=True)
        dst = bin_dir / binary_name
        shutil.copy2(src, dst)

        if sys.platform != "win32":
            dst.chmod(dst.stat().st_mode | 0o111)

        # Mark the wheel as platform-specific (not pure Python)
        build_data["pure_python"] = False
        build_data["infer_tag"] = True
