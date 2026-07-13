import os
import subprocess
import sys
from pathlib import Path


def main():
    bundle_dir = Path(os.getenv("MODEL_BUNDLE_DIR", "instance/models"))
    bundle_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "monai.bundle",
            "download",
            "--name",
            "brats_mri_segmentation",
            "--bundle_dir",
            str(bundle_dir),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
