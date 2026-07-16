import hashlib
import os
import shutil
import time
from pathlib import Path
from urllib.request import Request, urlopen


MONAI_BUNDLE = "brats_mri_segmentation"
MONAI_REVISION = "370f7f9d062745fbac445e7fe6d6616d35df04ec"
MONAI_MODEL_SHA256 = "860ccb3f1c21c99d0410ad8a1ac4ef6b8fab60cec0a503b0ba42675741a750ae"
MONAI_BASE_URL = (
    f"https://huggingface.co/MONAI/{MONAI_BUNDLE}/resolve/{MONAI_REVISION}/"
)
SLICE_MODEL_REPO = "kiselyovd/brain-mri-segmentation"
SLICE_MODEL_REVISION = "a813637"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(
    url: str,
    destination: Path,
    minimum_size: int,
    expected_sha256: str | None = None,
):
    if (
        destination.exists()
        and destination.stat().st_size >= minimum_size
        and (
            expected_sha256 is None
            or sha256_file(destination) == expected_sha256
        )
    ):
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "CerebraVue-model-builder/1.0"})
            with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)

            if temporary.stat().st_size < minimum_size:
                raise RuntimeError(f"Downloaded file is unexpectedly small: {url}")
            if expected_sha256 and sha256_file(temporary) != expected_sha256:
                raise RuntimeError(f"Checksum validation failed: {url}")

            temporary.replace(destination)
            return
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def download_monai_bundle(bundle_dir: Path):
    bundle_root = bundle_dir / MONAI_BUNDLE
    download_file(
        MONAI_BASE_URL + "configs/inference.json",
        bundle_root / "configs" / "inference.json",
        minimum_size=1_000,
    )
    download_file(
        MONAI_BASE_URL + "models/model.pt",
        bundle_root / "models" / "model.pt",
        minimum_size=10_000_000,
        expected_sha256=MONAI_MODEL_SHA256,
    )
    download_file(
        MONAI_BASE_URL + "LICENSE",
        bundle_root / "LICENSE",
        minimum_size=500,
    )


def download_slice_model(bundle_dir: Path):
    from huggingface_hub import snapshot_download

    destination = bundle_dir / "brain_mri_segformer"
    snapshot_download(
        repo_id=SLICE_MODEL_REPO,
        revision=SLICE_MODEL_REVISION,
        local_dir=destination,
        allow_patterns=[
            "config.json",
            "preprocessor_config.json",
            "model.safetensors",
            "README.md",
        ],
    )
    checkpoint = destination / "model.safetensors"
    if not checkpoint.exists() or checkpoint.stat().st_size < 90_000_000:
        raise RuntimeError("The MRI slice checkpoint did not download correctly.")
    shutil.rmtree(destination / ".cache", ignore_errors=True)


def main():
    bundle_dir = Path(os.getenv("MODEL_BUNDLE_DIR", "models"))
    bundle_dir.mkdir(parents=True, exist_ok=True)
    download_monai_bundle(bundle_dir)
    download_slice_model(bundle_dir)


if __name__ == "__main__":
    main()
