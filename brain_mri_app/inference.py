import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
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
LOGGER = logging.getLogger(__name__)


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
    source_preview_filename: Optional[str] = None
    overlay_filename: Optional[str] = None
    findings: List[Finding] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)

    def to_dict(self):
        payload = asdict(self)
        payload["findings"] = [asdict(finding) for finding in self.findings]
        return payload


class BrainTumorInference:
    """Research inference service for 2D MRI review and 3D BraTS segmentation."""

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
        self.monai_bundle_version = "0.5.4"
        self.monai_bundle_root = self.model_bundle_dir / self.monai_model_name

        self.slice_model_root = self.model_bundle_dir / "brain_mri_segformer"
        self.slice_weights_path = self.slice_model_root / "model.safetensors"
        self.quick_model_name = "SegFormer-B2 single-slice tumor segmentation"
        self._slice_model = None
        self._slice_processor = None
        self._slice_model_lock = threading.Lock()
        self._slice_inference_lock = threading.Lock()

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
            message="This file type is not supported. Choose an MRI image, video, or four NIfTI volumes.",
            inference_ms=0,
            model_loaded=self.slice_weights_path.exists(),
        )

    def analyze_live_frame(self, data_url: str) -> AnalysisResult:
        started = time.time()
        try:
            _, encoded = data_url.split(",", 1)
            rgb = self._image_bytes_to_rgb(base64.b64decode(encoded))
            overlay, findings = self._analyze_rgb_array(rgb)
            filename = self._save_overlay(overlay, "camera-review", ".jpg")
            return AnalysisResult(
                status="ok",
                mode="live",
                input_type="camera-frame",
                model_name=self.quick_model_name,
                message=self._slice_mode_message(findings),
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=True,
                overlay_filename=filename,
                findings=findings,
                details={"review_scope": "single captured frame"},
            )
        except Exception as exc:
            return self._error_result("live", "camera-frame", started, exc)

    def analyze_image(self, file_path: Path) -> AnalysisResult:
        started = time.time()
        try:
            rgb = np.array(Image.open(file_path).convert("RGB"))
            overlay, findings = self._analyze_rgb_array(rgb)
            filename = self._save_overlay(overlay, file_path.stem, ".jpg")
            return AnalysisResult(
                status="ok",
                mode="single-image",
                input_type=file_path.suffix.lower(),
                model_name=self.quick_model_name,
                message=self._slice_mode_message(findings),
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=True,
                overlay_filename=filename,
                findings=findings,
                details={
                    "review_scope": "single MRI slice",
                    "recommended_mode": "Use aligned T1c, T1, T2, and FLAIR volumes for volumetric segmentation.",
                },
            )
        except Exception as exc:
            return self._error_result("single-image", file_path.suffix.lower(), started, exc)

    def analyze_video(self, file_path: Path) -> AnalysisResult:
        started = time.time()
        cap = cv2.VideoCapture(str(file_path))
        if not cap.isOpened():
            return self._error_result("video", file_path.suffix.lower(), started, ValueError("The video could not be opened."))

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        sample_indexes = self._sample_indexes(frame_count, self.max_video_frames)
        overlays: List[Tuple[int, np.ndarray]] = []
        all_findings: List[Finding] = []
        current_index = 0
        sample_position = 0

        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if sample_position < len(sample_indexes) and current_index >= sample_indexes[sample_position]:
                    overlay, findings = self._analyze_rgb_array(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    frame_index = sample_indexes[sample_position]
                    overlays.append((frame_index, overlay))
                    for finding in findings[:2]:
                        area = finding.label.rsplit(" in the ", 1)[-1]
                        finding.label = f"possible tumor region in a reviewed video frame ({area})"
                    all_findings.extend(findings[:2])
                    sample_position += 1
                current_index += 1
                if sample_position >= len(sample_indexes):
                    break
        except Exception as exc:
            return self._error_result("video", file_path.suffix.lower(), started, exc)
        finally:
            cap.release()

        if not overlays:
            return self._error_result("video", file_path.suffix.lower(), started, ValueError("No readable frames were found."))

        filename = self._save_overlay(self._make_contact_sheet(overlays), file_path.stem, ".jpg")
        return AnalysisResult(
            status="ok",
            mode="video",
            input_type=file_path.suffix.lower(),
            model_name=self.quick_model_name,
            message=self._slice_mode_message(all_findings),
            inference_ms=int((time.time() - started) * 1000),
            model_loaded=True,
            overlay_filename=filename,
            findings=all_findings[:10],
            details={"review_scope": "sampled video frames"},
        )

    def analyze_multimodal_nifti(self, modality_paths: Dict[str, Path]) -> AnalysisResult:
        started = time.time()
        missing = [name for name in ("t1c", "t1", "t2", "flair") if name not in modality_paths]
        if missing:
            return AnalysisResult(
                status="missing-inputs",
                mode="3d-mri",
                input_type="nifti",
                model_name="MONAI BraTS MRI segmentation",
                message=f"Add the missing MRI series: {', '.join(missing)}.",
                inference_ms=0,
                model_loaded=self._monai_bundle_ready(),
            )

        if not self._ensure_monai_bundle():
            return AnalysisResult(
                status="model-not-ready",
                mode="3d-mri",
                input_type="nifti",
                model_name="MONAI BraTS MRI segmentation",
                message="The volumetric analysis package is unavailable in this deployment. Redeploy the current container image.",
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=False,
            )

        output_dir = self.result_folder / f".monai-{uuid.uuid4().hex}"
        try:
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
                "--dataloader#num_workers",
                "0",
                "--evaluator#amp",
                "false",
            ]
            completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=1200)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "unknown inference error").strip().splitlines()[-1]
                raise RuntimeError(f"Volumetric analysis did not complete: {detail}")

            segmentation_path = self._find_segmentation(output_dir)
            source_filename, overlay_filename, findings, subregions = self._render_segmentation_preview(
                flair_path=modality_paths["flair"],
                segmentation_path=segmentation_path,
            )
            message = (
                "Possible tumor regions were segmented across the MRI volume and marked on the review image."
                if findings
                else "No tumor region was segmented in the submitted MRI volume."
            )
            return AnalysisResult(
                status="ok",
                mode="3d-mri",
                input_type="nifti",
                model_name="MONAI BraTS MRI segmentation",
                message=message,
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=True,
                source_preview_filename=source_filename,
                overlay_filename=overlay_filename,
                findings=findings,
                details={
                    "modalities": ["T1c", "T1", "T2", "FLAIR"],
                    "subregions": subregions,
                    "planes": ["sagittal", "coronal", "axial"],
                    "review_scope": "aligned multimodal MRI volume",
                },
            )
        except Exception as exc:
            return self._error_result("3d-mri", "nifti", started, exc, "MONAI BraTS MRI segmentation")
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def _monai_bundle_ready(self) -> bool:
        config = self.monai_bundle_root / "configs" / "inference.json"
        checkpoint = self.monai_bundle_root / "models" / "model.pt"
        scripted = self.monai_bundle_root / "models" / "model.ts"
        return config.exists() and (checkpoint.exists() or scripted.exists())

    def slice_model_ready(self) -> bool:
        """Verify the deployed slice checkpoint can be loaded before accepting traffic."""
        try:
            self._load_slice_model()
            return True
        except Exception as exc:
            LOGGER.warning("The deployed MRI slice model could not be loaded: %s", exc)
            return False

    def _ensure_monai_bundle(self) -> bool:
        if self._monai_bundle_ready():
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
            "--version",
            self.monai_bundle_version,
            "--bundle_dir",
            str(self.model_bundle_dir),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=1200)
        return self._monai_bundle_ready()

    def _load_slice_model(self):
        if self._slice_model is not None:
            return self._slice_model
        if not self.slice_weights_path.exists():
            raise RuntimeError("The MRI slice model is unavailable in this deployment. Redeploy the current container image.")

        with self._slice_model_lock:
            if self._slice_model is not None:
                return self._slice_model
            import torch
            from transformers import AutoImageProcessor, AutoModelForSemanticSegmentation

            processor = AutoImageProcessor.from_pretrained(
                self.slice_model_root,
                local_files_only=True,
            )
            model = AutoModelForSemanticSegmentation.from_pretrained(
                self.slice_model_root,
                local_files_only=True,
                use_safetensors=True,
            )
            model.eval()
            torch.set_num_threads(max(1, min(2, os.cpu_count() or 1)))
            self._slice_processor = processor
            self._slice_model = model
        return self._slice_model

    def _analyze_rgb_array(self, rgb: np.ndarray) -> Tuple[np.ndarray, List[Finding]]:
        import torch
        import torch.nn.functional as functional

        self._validate_slice_input(rgb)
        resized, scale = self._fit_image(rgb, max_side=1400)
        model_image, mapping, brain_mask = self._prepare_slice_image(resized)
        model = self._load_slice_model()
        inputs = self._slice_processor(images=Image.fromarray(model_image), return_tensors="pt")
        pixel_values = inputs["pixel_values"]
        with self._slice_inference_lock, torch.inference_mode():
            logits = model(pixel_values=pixel_values).logits
            flipped_logits = model(pixel_values=torch.flip(pixel_values, dims=[3])).logits
            logits = (logits + torch.flip(flipped_logits, dims=[3])) * 0.5
            logits = functional.interpolate(
                logits,
                size=model_image.shape[:2],
                mode="bilinear",
                align_corners=False,
            )
            probability_small = torch.softmax(logits, dim=1)[0, 1].detach().cpu().numpy()

        probability = self._restore_slice_prediction(probability_small, mapping, resized.shape[:2])
        mask = self._clean_slice_mask(probability >= 0.54, brain_mask)
        findings = self._components_to_findings(mask, resized.shape[:2])
        overlay = self._thermal_overlay(resized, mask, probability)

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

    def _validate_slice_input(self, rgb: np.ndarray) -> None:
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError("The submitted image is not a supported RGB MRI view.")
        if min(rgb.shape[:2]) < 128:
            raise ValueError("The submitted image is too small for reliable MRI review.")

        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        low, high = np.percentile(gray, [2, 98])
        if float(high - low) < 18.0:
            raise ValueError("The submitted image does not contain enough contrast for reliable MRI review.")

    def _prepare_slice_image(self, rgb: np.ndarray):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        nonzero = gray[gray > 4]
        threshold = max(6.0, float(np.percentile(nonzero, 12))) if nonzero.size else 6.0
        brain_mask = (gray >= threshold).astype(np.uint8)
        brain_mask = cv2.morphologyEx(brain_mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
        brain_mask = self._largest_component(brain_mask)

        image_area = gray.shape[0] * gray.shape[1]
        if brain_mask.sum() < image_area * 0.025:
            raise ValueError("The uploaded frame does not contain a clear MRI slice.")

        x, y, width, height = cv2.boundingRect(brain_mask)
        margin = max(8, int(max(width, height) * 0.08))
        x = max(0, x - margin)
        y = max(0, y - margin)
        width = min(rgb.shape[1] - x, width + margin * 2)
        height = min(rgb.shape[0] - y, height + margin * 2)
        crop = rgb[y : y + height, x : x + width]

        side = max(width, height)
        pad_left = (side - width) // 2
        pad_top = (side - height) // 2
        square = np.zeros((side, side, 3), dtype=np.uint8)
        square[pad_top : pad_top + height, pad_left : pad_left + width] = crop
        model_image = cv2.resize(square, (512, 512), interpolation=cv2.INTER_AREA)
        mapping = {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "side": side,
            "pad_left": pad_left,
            "pad_top": pad_top,
        }
        return model_image, mapping, brain_mask

    def _restore_slice_prediction(self, probability: np.ndarray, mapping: Dict[str, int], shape: Tuple[int, int]):
        square = cv2.resize(probability.astype(np.float32), (mapping["side"], mapping["side"]), interpolation=cv2.INTER_LINEAR)
        crop = square[
            mapping["pad_top"] : mapping["pad_top"] + mapping["height"],
            mapping["pad_left"] : mapping["pad_left"] + mapping["width"],
        ]
        restored = np.zeros(shape, dtype=np.float32)
        restored[
            mapping["y"] : mapping["y"] + mapping["height"],
            mapping["x"] : mapping["x"] + mapping["width"],
        ] = crop
        return restored

    def _clean_slice_mask(self, mask: np.ndarray, brain_mask: np.ndarray) -> np.ndarray:
        cleaned = mask.astype(np.uint8)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=1)
        guard = cv2.dilate(brain_mask.astype(np.uint8), np.ones((11, 11), np.uint8), iterations=1)
        cleaned &= guard

        count, labels, stats, _ = cv2.connectedComponentsWithStats(cleaned, 8)
        filtered = np.zeros_like(cleaned)
        image_area = cleaned.shape[0] * cleaned.shape[1]
        brain_area = max(1, int(brain_mask.sum()))
        minimum = max(18, int(image_area * 0.00012))
        maximum = int(brain_area * 0.42)
        for index in range(1, count):
            area = int(stats[index, cv2.CC_STAT_AREA])
            if minimum <= area <= maximum:
                filtered[labels == index] = 1
        return filtered

    def _write_monai_datalist(self, modality_paths: Dict[str, Path], output_dir: Path) -> Path:
        case_dir = output_dir / "case"
        case_dir.mkdir(parents=True, exist_ok=True)
        ordered = []
        for name in ("t1c", "t1", "t2", "flair"):
            source = modality_paths[name]
            target = case_dir / f"{name}{''.join(source.suffixes)}"
            shutil.copyfile(source, target)
            ordered.append(str(target))
        datalist_path = output_dir / "datalist.json"
        datalist_path.write_text(json.dumps({"testing": [{"image": ordered}]}), encoding="utf-8")
        return datalist_path

    def _find_segmentation(self, output_dir: Path) -> Path:
        candidates = sorted(output_dir.rglob("*.nii*"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            if "seg" in path.name.lower() or "pred" in path.name.lower():
                return path
        raise FileNotFoundError("The volumetric analysis completed without a segmentation result.")

    def _render_segmentation_preview(self, flair_path: Path, segmentation_path: Path):
        try:
            import nibabel as nib
        except ImportError as exc:
            raise RuntimeError("The NIfTI preview component is unavailable.") from exc

        flair = nib.load(str(flair_path)).get_fdata()
        segmentation = self._coerce_segmentation_mask(nib.load(str(segmentation_path)).get_fdata())
        if flair.shape[:3] != segmentation.shape[:3]:
            minimum = tuple(min(a, b) for a, b in zip(flair.shape[:3], segmentation.shape[:3]))
            flair = flair[: minimum[0], : minimum[1], : minimum[2]]
            segmentation = segmentation[: minimum[0], : minimum[1], : minimum[2]]

        source_rgb, overlay, axial_mask = self._make_volume_montages(flair, segmentation)
        findings = self._components_to_findings(axial_mask > 0, axial_mask.shape)

        subregion_names = []
        for value, label in ((1, "tumor core"), (2, "whole tumor"), (4, "enhancing tumor")):
            if np.any(segmentation == value):
                subregion_names.append(label)

        source_filename = self._save_overlay(source_rgb, "volume-source", ".png")
        overlay_filename = self._save_overlay(overlay, "volume-segmentation", ".png")
        return source_filename, overlay_filename, findings[:8], subregion_names

    def _make_volume_montages(self, flair: np.ndarray, segmentation: np.ndarray):
        tumor = segmentation > 0
        indices = (
            int(np.argmax(np.sum(tumor, axis=(1, 2)))) if tumor.any() else flair.shape[0] // 2,
            int(np.argmax(np.sum(tumor, axis=(0, 2)))) if tumor.any() else flair.shape[1] // 2,
            int(np.argmax(np.sum(tumor, axis=(0, 1)))) if tumor.any() else flair.shape[2] // 2,
        )
        views = [
            ("SAGITTAL", np.rot90(flair[indices[0], :, :]), np.rot90(segmentation[indices[0], :, :])),
            ("CORONAL", np.rot90(flair[:, indices[1], :]), np.rot90(segmentation[:, indices[1], :])),
            ("AXIAL", np.rot90(flair[:, :, indices[2]]), np.rot90(segmentation[:, :, indices[2]])),
        ]

        source_tiles = []
        overlay_tiles = []
        axial_mask = views[-1][2]
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        for label, image, mask in views:
            normalized = clahe.apply(self._normalize_to_uint8(image))
            source = cv2.cvtColor(normalized, cv2.COLOR_GRAY2RGB)
            marked = self._thermal_overlay(source, mask > 0)
            source_tiles.append(self._volume_view_tile(source, label))
            overlay_tiles.append(self._volume_view_tile(marked, label))
        return np.hstack(source_tiles), np.hstack(overlay_tiles), axial_mask

    def _volume_view_tile(self, image: np.ndarray, label: str) -> np.ndarray:
        tile_size = 420
        height, width = image.shape[:2]
        scale = min((tile_size - 32) / max(1, width), (tile_size - 32) / max(1, height))
        resized = cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
        tile = np.full((tile_size + 46, tile_size, 3), (7, 15, 18), dtype=np.uint8)
        x = (tile_size - resized.shape[1]) // 2
        y = 46 + (tile_size - resized.shape[0]) // 2
        tile[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
        cv2.putText(tile, label, (18, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (201, 226, 222), 2, cv2.LINE_AA)
        return tile

    def _coerce_segmentation_mask(self, segmentation: np.ndarray) -> np.ndarray:
        segmentation = np.squeeze(segmentation)
        if segmentation.ndim == 3:
            if float(np.nanmax(segmentation)) <= 1.0:
                return (segmentation > 0.5).astype(np.uint8)
            return np.rint(segmentation).astype(np.uint8)
        if segmentation.ndim != 4:
            raise ValueError(f"Unsupported segmentation shape: {segmentation.shape}")

        channel_axes = [index for index, size in enumerate(segmentation.shape) if size in (3, 4)]
        if not channel_axes:
            raise ValueError(f"Unsupported segmentation channel layout: {segmentation.shape}")
        channels = np.moveaxis(segmentation, channel_axes[0], 0)

        label_map = np.zeros(channels.shape[1:], dtype=np.uint8)
        whole_tumor = channels[1] > 0.5 if channels.shape[0] > 1 else channels[0] > 0.5
        tumor_core = channels[0] > 0.5
        enhancing_tumor = channels[2] > 0.5 if channels.shape[0] > 2 else np.zeros_like(tumor_core)
        label_map[whole_tumor] = 2
        label_map[tumor_core] = 1
        label_map[enhancing_tumor] = 4
        return label_map

    def _thermal_overlay(
        self,
        rgb: np.ndarray,
        mask: np.ndarray,
        probability: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        base = cv2.cvtColor(cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray), cv2.COLOR_GRAY2RGB)
        mask_uint = (mask > 0).astype(np.uint8)
        if mask_uint.sum() == 0:
            return base

        smallest_side = max(1, min(mask_uint.shape[:2]))
        blur_size = max(21, int(smallest_side * 0.055))
        if blur_size % 2 == 0:
            blur_size += 1
        if probability is not None and probability.shape == mask_uint.shape:
            heat_source = np.clip(probability, 0.0, 1.0) * mask_uint
            heat_source = (heat_source * 255).astype(np.uint8)
        else:
            heat_source = mask_uint * 255
        heat = cv2.GaussianBlur(heat_source, (blur_size, blur_size), 0)
        heat = cv2.normalize(heat, None, 0, 255, cv2.NORM_MINMAX)
        color_map = getattr(cv2, "COLORMAP_TURBO", cv2.COLORMAP_JET)
        thermal = cv2.cvtColor(cv2.applyColorMap(heat, color_map), cv2.COLOR_BGR2RGB)
        alpha = np.clip(heat.astype(np.float32) / 255.0, 0.0, 0.88)[:, :, None]
        overlay = (base * (1.0 - alpha) + thermal * alpha).astype(np.uint8)

        contours, _ = cv2.findContours(mask_uint, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        glow = cv2.dilate(mask_uint, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)), iterations=1)
        glow_contours, _ = cv2.findContours(glow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, glow_contours, -1, (255, 102, 46), 5)
        cv2.drawContours(overlay, contours, -1, (255, 245, 214), 2)
        return overlay

    def _components_to_findings(self, mask: np.ndarray, shape: Tuple[int, int]) -> List[Finding]:
        count, _, stats, centroids = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        image_area = float(max(1, shape[0] * shape[1]))
        findings = []
        for index in range(1, count):
            x, y, width, height, area = stats[index]
            area_ratio = float(area) / image_area
            if area_ratio < 0.00012 or area_ratio > 0.42:
                continue
            center_x, center_y = centroids[index]
            region = self._scan_region_name(int(center_x), int(center_y), shape[1], shape[0])
            findings.append(
                Finding(
                    label=f"possible tumor region in the {region}",
                    confidence=0.0,
                    x=int(x),
                    y=int(y),
                    width=int(width),
                    height=int(height),
                    area_ratio=area_ratio,
                )
            )
        findings.sort(key=lambda item: item.area_ratio, reverse=True)
        return findings

    def _scan_region_name(self, x: int, y: int, width: int, height: int) -> str:
        vertical = "upper" if y < height * 0.38 else "lower" if y > height * 0.62 else "middle"
        horizontal = "left" if x < width * 0.38 else "right" if x > width * 0.62 else "central"
        if horizontal == "central":
            return f"{vertical} central scan area"
        if vertical == "middle":
            return f"{horizontal} scan area"
        return f"{vertical}-{horizontal} scan area"

    def _largest_component(self, mask: np.ndarray) -> np.ndarray:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        if count <= 1:
            return mask.astype(np.uint8)
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        return (labels == largest).astype(np.uint8)

    def _fit_image(self, rgb: np.ndarray, max_side: int) -> Tuple[np.ndarray, float]:
        height, width = rgb.shape[:2]
        largest = max(height, width)
        if largest <= max_side:
            return rgb.copy(), 1.0
        scale = max_side / float(largest)
        return cv2.resize(rgb, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA), scale

    def _image_bytes_to_rgb(self, raw: bytes) -> np.ndarray:
        from io import BytesIO

        return np.array(Image.open(BytesIO(raw)).convert("RGB"))

    def _normalize_to_uint8(self, image: np.ndarray) -> np.ndarray:
        values = image.astype(np.float32)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return np.zeros(values.shape, dtype=np.uint8)
        lo, hi = np.percentile(finite, [1, 99])
        if hi <= lo:
            hi = lo + 1.0
        return (np.clip((values - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)

    def _save_overlay(self, rgb: np.ndarray, stem: str, suffix: str) -> str:
        safe_stem = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in stem)[:60]
        filename = f"{safe_stem}-{uuid.uuid4().hex[:10]}{suffix}"
        Image.fromarray(rgb).save(self.result_folder / filename)
        return filename

    def _sample_indexes(self, frame_count: int, maximum: int) -> List[int]:
        if frame_count <= 0:
            return list(range(maximum))
        count = min(maximum, frame_count)
        return sorted(set(int(value) for value in np.linspace(0, frame_count - 1, count)))

    def _make_contact_sheet(self, overlays: Iterable[Tuple[int, np.ndarray]]) -> np.ndarray:
        tiles = []
        for index, image in overlays:
            tile = cv2.resize(image, (360, 250), interpolation=cv2.INTER_AREA)
            cv2.rectangle(tile, (0, 0), (136, 38), (20, 29, 34), -1)
            cv2.putText(tile, f"Frame {index}", (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.64, (245, 249, 248), 2)
            tiles.append(tile)
        columns = min(3, len(tiles))
        rows = int(np.ceil(len(tiles) / columns))
        sheet = np.full((rows * 250, columns * 360, 3), 12, dtype=np.uint8)
        for index, tile in enumerate(tiles):
            row, column = divmod(index, columns)
            sheet[row * 250 : (row + 1) * 250, column * 360 : (column + 1) * 360] = tile
        return sheet

    def _slice_mode_message(self, findings: List[Finding]) -> str:
        if findings:
            return "Possible tumor tissue was segmented and marked for clinical review."
        return "No tumor tissue was segmented in this view. Review the full MRI study when clinical concern remains."

    def _error_result(
        self,
        mode: str,
        input_type: str,
        started: float,
        exc: Exception,
        model_name: Optional[str] = None,
    ) -> AnalysisResult:
        LOGGER.error("MRI review failed for %s input: %s", mode, exc)
        return AnalysisResult(
            status="error",
            mode=mode,
            input_type=input_type,
            model_name=model_name or self.quick_model_name,
            message="Unable to complete this review. Verify that the input is a usable brain MRI study and try again.",
            inference_ms=int((time.time() - started) * 1000),
            model_loaded=self._slice_model is not None,
        )
