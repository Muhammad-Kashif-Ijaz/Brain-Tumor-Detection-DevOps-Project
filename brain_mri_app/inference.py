import base64
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
NIFTI_EXTENSIONS = {".nii", ".gz"}


@dataclass
class Finding:
    label: str
    confidence: float
    x: int
    y: int
    width: int
    height: int
    area_ratio: float


@dataclass
class AnalysisResult:
    status: str
    mode: str
    input_type: str
    model_name: str
    message: str
    inference_ms: int
    model_loaded: bool
    overlay_filename: Optional[str] = None
    findings: List[Finding] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)

    def to_dict(self):
        payload = asdict(self)
        payload["findings"] = [asdict(finding) for finding in self.findings]
        return payload


class BrainTumorInference:
    """Inference facade for MRI volume segmentation and lightweight frame review."""

    def __init__(
        self,
        result_folder: Path,
        model_bundle_dir: Path,
        auto_download_model: bool = False,
        max_video_frames: int = 10,
    ):
        self.result_folder = result_folder
        self.model_bundle_dir = model_bundle_dir
        self.auto_download_model = auto_download_model
        self.max_video_frames = max_video_frames
        self.monai_model_name = "brats_mri_segmentation"
        self.monai_bundle_root = self.model_bundle_dir / self.monai_model_name
        self.quick_model_name = "MRI intensity and morphology triage overlay"

    def analyze_file(self, file_path: Path) -> AnalysisResult:
        suffix = file_path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return self.analyze_image(file_path)
        if suffix in VIDEO_EXTENSIONS:
            return self.analyze_video(file_path)
        return AnalysisResult(
            status="unsupported",
            mode="upload",
            input_type=suffix or "unknown",
            model_name=self.quick_model_name,
            message="Unsupported file type. Upload an image, video, or four NIfTI MRI volumes.",
            inference_ms=0,
            model_loaded=False,
        )

    def analyze_live_frame(self, data_url: str) -> AnalysisResult:
        start = time.time()
        try:
            _, encoded = data_url.split(",", 1)
            raw = base64.b64decode(encoded)
            rgb = self._image_bytes_to_rgb(raw)
            overlay, findings = self._analyze_rgb_array(rgb)
            filename = self._save_overlay(overlay, "live", ".jpg")
            elapsed = int((time.time() - start) * 1000)
            return AnalysisResult(
                status="ok",
                mode="live",
                input_type="webcam-frame",
                model_name=self.quick_model_name,
                message=self._quick_mode_message(findings),
                inference_ms=elapsed,
                model_loaded=True,
                overlay_filename=filename,
                findings=findings,
                details={"frame_source": "browser camera snapshot"},
            )
        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            return self._error_result("live", "webcam-frame", elapsed, exc)

    def analyze_image(self, file_path: Path) -> AnalysisResult:
        start = time.time()
        try:
            rgb = np.array(Image.open(file_path).convert("RGB"))
            overlay, findings = self._analyze_rgb_array(rgb)
            filename = self._save_overlay(overlay, file_path.stem, ".jpg")
            elapsed = int((time.time() - start) * 1000)
            return AnalysisResult(
                status="ok",
                mode="single-image",
                input_type=file_path.suffix.lower(),
                model_name=self.quick_model_name,
                message=self._quick_mode_message(findings),
                inference_ms=elapsed,
                model_loaded=True,
                overlay_filename=filename,
                findings=findings,
                details={
                    "clinical_note": "Single 2D images cannot provide diagnostic-grade tumor segmentation.",
                    "recommended_mode": "Use four aligned BraTS-style NIfTI MRI volumes for the MONAI model.",
                },
            )
        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            return self._error_result("single-image", file_path.suffix.lower(), elapsed, exc)

    def analyze_video(self, file_path: Path) -> AnalysisResult:
        start = time.time()
        cap = cv2.VideoCapture(str(file_path))
        if not cap.isOpened():
            return AnalysisResult(
                status="error",
                mode="video",
                input_type=file_path.suffix.lower(),
                model_name=self.quick_model_name,
                message="The video could not be opened.",
                inference_ms=0,
                model_loaded=False,
            )

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        sample_indexes = self._sample_indexes(frame_count, self.max_video_frames)
        overlays = []
        all_findings = []

        current_index = 0
        sample_position = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if sample_position < len(sample_indexes) and current_index >= sample_indexes[sample_position]:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                overlay, findings = self._analyze_rgb_array(rgb)
                overlays.append((sample_indexes[sample_position], overlay))
                for finding in findings:
                    finding.label = f"frame {sample_indexes[sample_position]}: {finding.label}"
                all_findings.extend(findings[:2])
                sample_position += 1
            current_index += 1
            if sample_position >= len(sample_indexes):
                break

        cap.release()
        if not overlays:
            elapsed = int((time.time() - start) * 1000)
            return AnalysisResult(
                status="error",
                mode="video",
                input_type=file_path.suffix.lower(),
                model_name=self.quick_model_name,
                message="No readable frames were found in the video.",
                inference_ms=elapsed,
                model_loaded=False,
            )

        sheet = self._make_contact_sheet(overlays)
        filename = self._save_overlay(sheet, file_path.stem, ".jpg")
        elapsed = int((time.time() - start) * 1000)
        return AnalysisResult(
            status="ok",
            mode="video",
            input_type=file_path.suffix.lower(),
            model_name=self.quick_model_name,
            message=self._quick_mode_message(all_findings),
            inference_ms=elapsed,
            model_loaded=True,
            overlay_filename=filename,
            findings=all_findings[:10],
            details={
                "sampled_frames": [index for index, _ in overlays],
                "clinical_note": "Video review samples frames and is not a clinical MRI segmentation workflow.",
            },
        )

    def analyze_multimodal_nifti(self, modality_paths: Dict[str, Path]) -> AnalysisResult:
        start = time.time()
        missing = [name for name in ("t1c", "t1", "t2", "flair") if name not in modality_paths]
        if missing:
            return AnalysisResult(
                status="missing-inputs",
                mode="3d-mri",
                input_type="nifti",
                model_name="MONAI brats_mri_segmentation",
                message=f"Missing required MRI modalities: {', '.join(missing)}.",
                inference_ms=0,
                model_loaded=self.monai_bundle_root.exists(),
            )

        model_ready = self._ensure_monai_bundle()
        if not model_ready:
            elapsed = int((time.time() - start) * 1000)
            return AnalysisResult(
                status="model-not-ready",
                mode="3d-mri",
                input_type="nifti",
                model_name="MONAI brats_mri_segmentation",
                message=(
                    "The MONAI BraTS model bundle is not present yet. "
                    "Run scripts/download_model.py once, or set AUTO_DOWNLOAD_MODEL=true."
                ),
                inference_ms=elapsed,
                model_loaded=False,
                details={
                    "expected_bundle_path": str(self.monai_bundle_root),
                    "required_modalities": ["t1c", "t1", "t2", "flair"],
                },
            )

        try:
            output_dir = self.result_folder / f"monai-{uuid.uuid4().hex}"
            output_dir.mkdir(parents=True, exist_ok=True)
            datalist_path = self._write_monai_datalist(modality_paths, output_dir)
            cmd = [
                sys.executable,
                "-m",
                "monai.bundle",
                "run",
                "--config_file",
                str(self.monai_bundle_root / "configs" / "inference.json"),
                "--bundle_root",
                str(self.monai_bundle_root),
                "--dataset_dir",
                str(output_dir),
                "--data_list_file_path",
                str(datalist_path),
                "--output_dir",
                str(output_dir),
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900)
            segmentation_path = self._find_latest_segmentation(output_dir)
            overlay_filename, findings = self._render_segmentation_preview(
                flair_path=modality_paths["flair"],
                segmentation_path=segmentation_path,
            )
            elapsed = int((time.time() - start) * 1000)
            return AnalysisResult(
                status="ok",
                mode="3d-mri",
                input_type="nifti",
                model_name="MONAI brats_mri_segmentation",
                message="Volume tumor segmentation completed. Review the overlay with a qualified clinician.",
                inference_ms=elapsed,
                model_loaded=True,
                overlay_filename=overlay_filename,
                findings=findings,
                details={
                    "segmentation_file": str(segmentation_path),
                    "modalities": sorted(modality_paths.keys()),
                    "labels": {
                        "1": "tumor core",
                        "2": "whole tumor",
                        "4": "enhancing tumor",
                    },
                },
            )
        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            return self._error_result("3d-mri", "nifti", elapsed, exc, "MONAI brats_mri_segmentation")

    def _ensure_monai_bundle(self) -> bool:
        inference_config = self.monai_bundle_root / "configs" / "inference.json"
        if inference_config.exists():
            return True
        if not self.auto_download_model:
            return False
        cmd = [
            sys.executable,
            "-m",
            "monai.bundle",
            "download",
            "--name",
            self.monai_model_name,
            "--bundle_dir",
            str(self.model_bundle_dir),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900)
        return inference_config.exists()

    def _write_monai_datalist(self, modality_paths: Dict[str, Path], output_dir: Path) -> Path:
        case_dir = output_dir / "case"
        case_dir.mkdir(parents=True, exist_ok=True)
        ordered = []
        for name in ("t1c", "t1", "t2", "flair"):
            source = modality_paths[name]
            target = case_dir / f"{name}{''.join(source.suffixes)}"
            if source.resolve() != target.resolve():
                target.write_bytes(source.read_bytes())
            ordered.append(str(target))
        datalist = {"testing": [{"image": ordered}]}
        datalist_path = output_dir / "datalist.json"
        datalist_path.write_text(json.dumps(datalist), encoding="utf-8")
        return datalist_path

    def _find_latest_segmentation(self, output_dir: Path) -> Path:
        candidates = sorted(output_dir.rglob("*.nii*"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            if "seg" in path.name.lower() or "pred" in path.name.lower():
                return path
        if candidates:
            return candidates[0]
        raise FileNotFoundError("MONAI completed without a NIfTI segmentation output.")

    def _render_segmentation_preview(self, flair_path: Path, segmentation_path: Path) -> Tuple[str, List[Finding]]:
        try:
            import nibabel as nib
        except ImportError as exc:
            raise RuntimeError("nibabel is required to render 3D segmentation previews.") from exc

        flair = nib.load(str(flair_path)).get_fdata()
        seg = nib.load(str(segmentation_path)).get_fdata()
        seg = self._coerce_segmentation_mask(seg)
        if flair.shape[:3] != seg.shape[:3]:
            min_shape = tuple(min(a, b) for a, b in zip(flair.shape[:3], seg.shape[:3]))
            flair = flair[: min_shape[0], : min_shape[1], : min_shape[2]]
            seg = seg[: min_shape[0], : min_shape[1], : min_shape[2]]

        tumor_by_slice = np.sum(seg > 0, axis=(0, 1))
        z_index = int(np.argmax(tumor_by_slice)) if np.max(tumor_by_slice) > 0 else flair.shape[2] // 2
        image = flair[:, :, z_index]
        mask = seg[:, :, z_index]
        image = self._normalize_to_uint8(image)
        rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        overlay = self._thermal_overlay(rgb, mask > 0)
        contours_mask = (mask > 0).astype(np.uint8) * 255
        contours, _ = cv2.findContours(contours_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, contours, -1, (255, 229, 180), 2)

        findings = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area_ratio = float(cv2.contourArea(contour)) / float(mask.shape[0] * mask.shape[1])
            if area_ratio > 0:
                findings.append(Finding("segmented tumor region", 0.94, int(x), int(y), int(w), int(h), area_ratio))
        filename = self._save_overlay(overlay, "monai-segmentation", ".png")
        return filename, findings[:8]

    def _coerce_segmentation_mask(self, seg: np.ndarray) -> np.ndarray:
        seg = np.squeeze(seg)
        if seg.ndim == 3:
            return seg
        if seg.ndim != 4:
            raise ValueError(f"Unsupported segmentation shape: {seg.shape}")

        if seg.shape[0] in (3, 4):
            channels = seg
        elif seg.shape[-1] in (3, 4):
            channels = np.moveaxis(seg, -1, 0)
        else:
            raise ValueError(f"Unsupported channel layout for segmentation: {seg.shape}")

        label_map = np.zeros(channels.shape[1:], dtype=np.uint8)
        # MONAI BraTS output channels are TC, WT, ET. Convert to BraTS labels 1, 2, 4.
        for channel_index, label in enumerate((1, 2, 4)):
            if channel_index < channels.shape[0]:
                label_map[channels[channel_index] > 0.5] = label
        return label_map

    def _analyze_rgb_array(self, rgb: np.ndarray) -> Tuple[np.ndarray, List[Finding]]:
        resized, scale = self._fit_image(rgb, max_side=1100)
        gray = cv2.cvtColor(resized, cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        brain_mask = gray > max(8, np.percentile(gray, 18))
        brain_mask = self._largest_component(brain_mask.astype(np.uint8))
        if brain_mask.sum() == 0:
            brain_mask = np.ones_like(gray, dtype=np.uint8)

        brain_pixels = gray[brain_mask > 0]
        high_threshold = np.percentile(brain_pixels, 96.5) if brain_pixels.size else 245
        low_threshold = np.percentile(brain_pixels, 4) if brain_pixels.size else 5
        bright = (gray >= high_threshold) & (brain_mask > 0)
        dark_halo = (gray <= low_threshold) & (brain_mask > 0)

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        suspicious = cv2.morphologyEx(bright.astype(np.uint8), cv2.MORPH_CLOSE, kernel, iterations=2)
        suspicious = cv2.morphologyEx(suspicious, cv2.MORPH_OPEN, kernel, iterations=1)

        findings = self._components_to_findings(suspicious, gray.shape)
        if not findings:
            suspicious = cv2.morphologyEx(dark_halo.astype(np.uint8), cv2.MORPH_OPEN, kernel, iterations=1)
            findings = self._components_to_findings(suspicious, gray.shape, label="possible abnormal low-intensity region")

        overlay = self._thermal_overlay(resized, suspicious > 0)

        if scale != 1.0:
            findings = [
                Finding(
                    finding.label,
                    finding.confidence,
                    int(finding.x / scale),
                    int(finding.y / scale),
                    int(finding.width / scale),
                    int(finding.height / scale),
                    finding.area_ratio,
                )
                for finding in findings
            ]
        return overlay, findings[:8]

    def _thermal_overlay(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        base = clahe.apply(gray)
        base_rgb = cv2.cvtColor(base, cv2.COLOR_GRAY2RGB)

        mask_uint = (mask > 0).astype(np.uint8)
        if mask_uint.sum() == 0:
            return base_rgb

        smallest_side = max(1, min(mask_uint.shape[:2]))
        kernel_size = max(21, int(smallest_side * 0.055))
        if kernel_size % 2 == 0:
            kernel_size += 1

        heat = (mask_uint * 255).astype(np.uint8)
        heat = cv2.GaussianBlur(heat, (kernel_size, kernel_size), 0)
        if int(heat.max()) > 0:
            heat = cv2.normalize(heat, None, 0, 255, cv2.NORM_MINMAX)

        color_map = getattr(cv2, "COLORMAP_INFERNO", cv2.COLORMAP_JET)
        thermal = cv2.applyColorMap(heat, color_map)
        thermal = cv2.cvtColor(thermal, cv2.COLOR_BGR2RGB)
        alpha = np.clip(heat.astype(np.float32) / 255.0, 0.0, 0.72)[:, :, None]
        overlay = (base_rgb * (1 - alpha) + thermal * alpha).astype(np.uint8)

        contours, _ = cv2.findContours(mask_uint, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, contours, -1, (255, 232, 188), 2)
        return overlay

    def _components_to_findings(self, mask: np.ndarray, shape: Tuple[int, int], label: str = "possible tumor region") -> List[Finding]:
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        image_area = float(shape[0] * shape[1])
        findings = []
        for index in range(1, count):
            x, y, w, h, area = stats[index]
            area_ratio = float(area) / image_area
            if area_ratio < 0.00035 or area_ratio > 0.18:
                continue
            compactness = min(1.0, area / max(1.0, float(w * h)))
            confidence = max(0.15, min(0.88, 0.28 + area_ratio * 12 + compactness * 0.35))
            findings.append(Finding(label, round(confidence, 3), int(x), int(y), int(w), int(h), area_ratio))
        findings.sort(key=lambda item: item.confidence * item.area_ratio, reverse=True)
        return findings

    def _largest_component(self, mask: np.ndarray) -> np.ndarray:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if count <= 1:
            return mask
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        return (labels == largest).astype(np.uint8)

    def _fit_image(self, rgb: np.ndarray, max_side: int) -> Tuple[np.ndarray, float]:
        height, width = rgb.shape[:2]
        largest = max(height, width)
        if largest <= max_side:
            return rgb.copy(), 1.0
        scale = max_side / float(largest)
        resized = cv2.resize(rgb, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
        return resized, scale

    def _image_bytes_to_rgb(self, raw: bytes) -> np.ndarray:
        return np.array(Image.open(self._bytes_io(raw)).convert("RGB"))

    def _bytes_io(self, raw: bytes):
        from io import BytesIO

        return BytesIO(raw)

    def _normalize_to_uint8(self, image: np.ndarray) -> np.ndarray:
        values = image.astype(np.float32)
        lo, hi = np.percentile(values[np.isfinite(values)], [1, 99])
        if hi <= lo:
            hi = lo + 1.0
        values = np.clip((values - lo) / (hi - lo), 0, 1)
        return (values * 255).astype(np.uint8)

    def _save_overlay(self, rgb: np.ndarray, stem: str, suffix: str) -> str:
        safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in stem)[:60]
        filename = f"{safe_stem}-{uuid.uuid4().hex[:10]}{suffix}"
        output_path = self.result_folder / filename
        Image.fromarray(rgb).save(output_path)
        return filename

    def _sample_indexes(self, frame_count: int, maximum: int) -> List[int]:
        if frame_count <= 0:
            return list(range(maximum))
        count = min(maximum, frame_count)
        return sorted(set(int(value) for value in np.linspace(0, frame_count - 1, count)))

    def _make_contact_sheet(self, overlays: Iterable[Tuple[int, np.ndarray]]) -> np.ndarray:
        tiles = []
        for index, image in overlays:
            tile = cv2.resize(image, (320, 220), interpolation=cv2.INTER_AREA)
            cv2.putText(tile, f"Frame {index}", (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            tiles.append(tile)
        columns = min(3, len(tiles))
        rows = int(np.ceil(len(tiles) / columns))
        sheet = np.full((rows * 220, columns * 320, 3), 18, dtype=np.uint8)
        for idx, tile in enumerate(tiles):
            row = idx // columns
            col = idx % columns
            sheet[row * 220 : (row + 1) * 220, col * 320 : (col + 1) * 320] = tile
        return sheet

    def _quick_mode_message(self, findings: List[Finding]) -> str:
        if findings:
            return "Possible abnormal region detected. Use this as a review aid, not a diagnosis."
        return "No obvious abnormal region was highlighted in this view."

    def _error_result(
        self,
        mode: str,
        input_type: str,
        inference_ms: int,
        exc: Exception,
        model_name: Optional[str] = None,
    ) -> AnalysisResult:
        return AnalysisResult(
            status="error",
            mode=mode,
            input_type=input_type,
            model_name=model_name or self.quick_model_name,
            message=f"Analysis failed: {exc}",
            inference_ms=inference_ms,
            model_loaded=False,
        )
