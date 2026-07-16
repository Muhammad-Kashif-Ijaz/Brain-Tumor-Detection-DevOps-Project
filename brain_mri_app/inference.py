import base64
import logging
import os
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
MAX_RESAMPLED_VOLUME_VOXELS = 20_000_000
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
        self.volume_model_name = "MONAI BraTS SegResNet 3D ensemble"
        # The published training workflow flips all three spatial axes. Evaluate
        # each mirrored orientation sequentially to improve consistency without
        # multiplying the peak memory used by one full-study request.
        self._volume_tta_axes = (
            (),
            (2,),
            (3,),
            (4,),
            (2, 3),
            (2, 4),
            (3, 4),
            (2, 3, 4),
        )

        self.slice_model_root = self.model_bundle_dir / "brain_mri_segformer"
        self.slice_weights_path = self.slice_model_root / "model.safetensors"
        self.quick_model_name = "SegFormer-B2 single-slice tumor segmentation"
        self._slice_model = None
        self._slice_processor = None
        self._slice_model_lock = threading.Lock()
        self._slice_inference_lock = threading.Lock()
        self._volume_model = None
        self._volume_model_lock = threading.Lock()
        self._volume_inference_lock = threading.Lock()

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
            panels = self._split_composite_mri_panels(rgb)
            reviewed_panels = []
            findings: List[Finding] = []
            for index, panel in enumerate(panels, start=1):
                overlay, panel_findings = self._analyze_rgb_array(panel)
                reviewed_panels.append((f"MRI VIEW {index}", panel, overlay))
                for finding in panel_findings:
                    findings.append(
                        Finding(
                            label=f"possible tumor region in MRI view {index}",
                            confidence=finding.confidence,
                            x=finding.x,
                            y=finding.y,
                            width=finding.width,
                            height=finding.height,
                            area_ratio=finding.area_ratio,
                        )
                    )

            source_filename = None
            if len(reviewed_panels) > 1:
                source_filename = self._save_overlay(
                    self._make_study_contact_sheet([(label, source) for label, source, _ in reviewed_panels]),
                    f"{file_path.stem}-source",
                    ".jpg",
                )
                overlay = self._make_study_contact_sheet([(label, marked) for label, _, marked in reviewed_panels])
            else:
                overlay = reviewed_panels[0][2]

            filename = self._save_overlay(overlay, file_path.stem, ".jpg")
            return AnalysisResult(
                status="ok",
                mode="single-image",
                input_type=file_path.suffix.lower(),
                model_name=self.quick_model_name,
                message=self._slice_mode_message(findings),
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=True,
                source_preview_filename=source_filename,
                overlay_filename=filename,
                findings=findings[:12],
                details={
                    "review_scope": "single MRI slice" if len(panels) == 1 else f"{len(panels)} MRI views in one image",
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
                model_name=self.volume_model_name,
                message=f"Add the missing MRI series: {', '.join(missing)}.",
                inference_ms=0,
                model_loaded=self._monai_bundle_ready(),
            )

        try:
            self._validate_multimodal_study(modality_paths)
        except ValueError as exc:
            return AnalysisResult(
                status="invalid-inputs",
                mode="3d-mri",
                input_type="nifti",
                model_name=self.volume_model_name,
                message=str(exc),
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=self._monai_bundle_ready(),
            )

        if not self._ensure_monai_bundle():
            return AnalysisResult(
                status="model-not-ready",
                mode="3d-mri",
                input_type="nifti",
                model_name=self.volume_model_name,
                message="The volumetric analysis package is unavailable in this deployment. Redeploy the current container image.",
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=False,
            )

        try:
            flair, segmentation = self._segment_multimodal_volume(modality_paths)
            source_filename, overlay_filename, findings, subregions = self._render_volume_arrays(flair, segmentation)
            message = (
                "Possible tumor regions were segmented across the MRI volume and marked on the review image."
                if findings
                else "No tumor region was segmented in the submitted MRI volume."
            )
            return AnalysisResult(
                status="ok",
                mode="3d-mri",
                input_type="nifti",
                model_name=self.volume_model_name,
                message=message,
                inference_ms=int((time.time() - started) * 1000),
                model_loaded=True,
                source_preview_filename=source_filename,
                overlay_filename=overlay_filename,
                findings=findings,
                details={
                    "modalities": ["T1c", "T1", "T2", "FLAIR"],
                    "subregions": subregions,
                    "planes": ["sagittal", "coronal", "axial", "all-regions overview"],
                    "review_scope": "aligned multimodal MRI volume",
                },
            )
        except Exception as exc:
            return self._error_result("3d-mri", "nifti", started, exc, self.volume_model_name)

    def _monai_bundle_ready(self) -> bool:
        checkpoint = self.monai_bundle_root / "models" / "model.pt"
        return checkpoint.exists() and checkpoint.stat().st_size >= 10_000_000

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

    def _load_volume_model(self):
        """Load the pinned BraTS network once instead of launching the bundle CLI per study."""
        if self._volume_model is not None:
            return self._volume_model

        checkpoint_path = self.monai_bundle_root / "models" / "model.pt"
        if not checkpoint_path.exists():
            raise RuntimeError("The BraTS MRI checkpoint is unavailable in this deployment.")

        with self._volume_model_lock:
            if self._volume_model is not None:
                return self._volume_model

            import torch
            from monai.networks.nets import SegResNet

            model = SegResNet(
                spatial_dims=3,
                init_filters=16,
                in_channels=4,
                out_channels=3,
                dropout_prob=0.2,
                blocks_down=(1, 2, 2, 4),
                blocks_up=(1, 1, 1),
            )
            try:
                checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
            except TypeError:
                checkpoint = torch.load(checkpoint_path, map_location="cpu")

            try:
                model.load_state_dict(self._volume_checkpoint_state(checkpoint), strict=True)
            except RuntimeError as exc:
                raise RuntimeError("The pinned BraTS MRI checkpoint could not be loaded.") from exc

            model.eval()
            torch.set_num_threads(max(1, min(2, os.cpu_count() or 1)))
            self._volume_model = model
        return self._volume_model

    @staticmethod
    def _volume_checkpoint_state(checkpoint):
        if isinstance(checkpoint, dict):
            for key in ("model", "state_dict", "network"):
                candidate = checkpoint.get(key)
                if isinstance(candidate, dict):
                    checkpoint = candidate
                    break
        if not isinstance(checkpoint, dict):
            raise RuntimeError("The BraTS MRI checkpoint has an unsupported format.")

        state = {}
        for key, value in checkpoint.items():
            normalized_key = str(key).removeprefix("module.").removeprefix("network.")
            state[normalized_key] = value
        return state

    def _segment_multimodal_volume(self, modality_paths: Dict[str, Path]) -> Tuple[np.ndarray, np.ndarray]:
        """Run the official BraTS architecture directly on four aligned MRI sequences."""
        try:
            import nibabel as nib
            import torch
            from nibabel.processing import resample_from_to, resample_to_output
            from monai.inferers import SlidingWindowInferer
        except ImportError as exc:
            raise RuntimeError("The volumetric MRI dependencies are unavailable in this deployment.") from exc

        source_images = {
            name: nib.as_closest_canonical(nib.load(str(modality_paths[name])))
            for name in ("t1c", "t1", "t2", "flair")
        }
        reference = source_images["t1c"]
        spacing = np.asarray(reference.header.get_zooms()[:3], dtype=np.float32)
        requires_resample = not np.allclose(spacing, (1.0, 1.0, 1.0), atol=0.05, rtol=0.0)
        if requires_resample:
            reference = resample_to_output(reference, voxel_sizes=(1.0, 1.0, 1.0), order=1)
        if int(np.prod(reference.shape[:3])) > MAX_RESAMPLED_VOLUME_VOXELS:
            raise ValueError("The submitted MRI volume is too large for this review service.")

        if requires_resample:
            target_grid = (reference.shape, reference.affine)
            source_images = {
                name: reference if name == "t1c" else resample_from_to(image, target_grid, order=1)
                for name, image in source_images.items()
            }

        source_volumes = {}
        model_channels = []
        # The MONAI bundle metadata fixes this channel order: T1c, T1, T2, FLAIR.
        for name in ("t1c", "t1", "t2", "flair"):
            volume = source_images[name].get_fdata(dtype=np.float32)
            source_volumes[name] = volume
            model_channels.append(self._normalize_brats_channel(volume))

        volume_tensor = torch.from_numpy(np.ascontiguousarray(np.stack(model_channels, axis=0))).unsqueeze(0)
        model = self._load_volume_model()
        roi_size = self._volume_roi_size(volume_tensor.shape[-3:])
        inferer = SlidingWindowInferer(
            roi_size=roi_size,
            sw_batch_size=1,
            overlap=0.5,
            mode="gaussian",
            padding_mode="constant",
        )
        with self._volume_inference_lock, torch.inference_mode():
            probability_sum = None
            for flip_axes in self._volume_tta_axes:
                augmented = torch.flip(volume_tensor, dims=flip_axes) if flip_axes else volume_tensor
                logits = inferer(augmented, model)
                if flip_axes:
                    logits = torch.flip(logits, dims=flip_axes)
                probabilities = torch.sigmoid(logits)
                probability_sum = probabilities if probability_sum is None else probability_sum + probabilities
            probabilities = (probability_sum / len(self._volume_tta_axes))[0].float().cpu().numpy()

        segmentation = self._brats_labels_from_probabilities(probabilities)
        return source_volumes["flair"], self._clean_volume_segmentation(segmentation)

    @staticmethod
    def _normalize_brats_channel(volume: np.ndarray) -> np.ndarray:
        """Match MONAI's channel-wise, non-zero intensity normalization."""
        values = np.asarray(volume, dtype=np.float32)
        normalized = np.zeros(values.shape, dtype=np.float32)
        foreground = np.isfinite(values) & (values != 0)
        if not np.any(foreground):
            return normalized

        foreground_values = values[foreground]
        standard_deviation = float(np.std(foreground_values))
        if standard_deviation < 1e-6:
            return normalized
        normalized[foreground] = (foreground_values - float(np.mean(foreground_values))) / standard_deviation
        return normalized

    @staticmethod
    def _volume_roi_size(shape: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Avoid padding a compact valid study to a huge 240 x 240 x 160 test window."""
        target = (240, 240, 160)
        sizes = []
        for dimension, preferred in zip(shape, target):
            limited = max(32, min(int(dimension), preferred))
            sizes.append(max(32, limited - (limited % 8)))
        return tuple(sizes)

    @staticmethod
    def _brats_labels_from_probabilities(probabilities: np.ndarray) -> np.ndarray:
        if probabilities.ndim != 4 or probabilities.shape[0] < 3:
            raise RuntimeError(f"The BraTS model returned an unexpected output shape: {probabilities.shape}")

        # The official bundle predicts tumor core, whole tumor, and enhancing tumor.
        segmentation = np.zeros(probabilities.shape[1:], dtype=np.uint8)
        segmentation[probabilities[1] >= 0.5] = 2
        segmentation[probabilities[0] >= 0.5] = 1
        segmentation[probabilities[2] >= 0.5] = 4
        return segmentation

    @staticmethod
    def _label_volume_regions(mask: np.ndarray):
        """Label every 3D candidate region without collapsing separate lesions."""
        try:
            from scipy.ndimage import generate_binary_structure, label
        except ImportError:
            return None, 0

        labels, count = label(
            np.asarray(mask, dtype=bool),
            structure=generate_binary_structure(3, 3),
        )
        return labels, int(count)

    def _clean_volume_segmentation(self, segmentation: np.ndarray) -> np.ndarray:
        """Discard only isolated voxel noise while retaining every credible lesion."""
        segmentation = np.asarray(segmentation, dtype=np.uint8)
        labels, count = self._label_volume_regions(segmentation > 0)
        if labels is None or count == 0:
            return segmentation

        component_sizes = np.bincount(labels.ravel())
        minimum_voxels = max(4, min(24, int(round(segmentation.size * 0.000001))))
        keep = component_sizes >= minimum_voxels
        keep[0] = False
        return np.where(keep[labels], segmentation, 0).astype(np.uint8)

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

        # A scanner's display window can vary widely. Normalize only the brain
        # pixels so the slice model sees a stable grayscale MRI representation.
        normalized_gray = self._window_mri_slice(gray, brain_mask)
        normalized_gray[brain_mask == 0] = 0
        normalized_rgb = cv2.cvtColor(normalized_gray, cv2.COLOR_GRAY2RGB)
        crop = normalized_rgb[y : y + height, x : x + width]

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

    @staticmethod
    def _window_mri_slice(gray: np.ndarray, brain_mask: np.ndarray) -> np.ndarray:
        values = gray[brain_mask > 0]
        if values.size == 0:
            return gray.copy()

        low, high = np.percentile(values, [1.0, 99.0])
        if float(high - low) < 1.0:
            return gray.copy()
        normalized = np.clip((gray.astype(np.float32) - low) / (high - low), 0.0, 1.0)
        return np.rint(normalized * 255.0).astype(np.uint8)

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
        # Never paint a candidate outside the detected brain field.
        cleaned &= brain_mask.astype(np.uint8)

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

    def _validate_multimodal_study(self, modality_paths: Dict[str, Path]) -> None:
        try:
            import nibabel as nib
        except ImportError as exc:
            raise ValueError("The NIfTI validation component is unavailable in this deployment.") from exc

        reference_shape = None
        reference_affine = None
        for name in ("t1c", "t1", "t2", "flair"):
            try:
                image = nib.load(str(modality_paths[name]))
            except Exception as exc:
                raise ValueError("One or more NIfTI MRI sequences could not be read.") from exc
            if len(image.shape) != 3:
                raise ValueError("Each MRI sequence must be one three-dimensional NIfTI volume.")
            if min(image.shape) < 32:
                raise ValueError("The submitted MRI volume is too small for volumetric review.")
            volume = image.get_fdata(dtype=np.float32)
            usable = volume[np.isfinite(volume) & (np.abs(volume) > 1e-6)]
            if usable.size < volume.size * 0.005:
                raise ValueError(f"The {name.upper()} sequence does not contain enough MRI signal for volumetric review.")
            low, high = np.percentile(usable, [1.0, 99.0])
            if float(high - low) < 1e-5:
                raise ValueError(f"The {name.upper()} sequence does not contain usable intensity variation.")
            if reference_shape is None:
                reference_shape = image.shape
                reference_affine = image.affine
                continue
            if image.shape != reference_shape or not np.allclose(image.affine, reference_affine, atol=1e-3):
                raise ValueError("The four MRI sequences must have matching dimensions and spatial alignment.")

    def _render_volume_arrays(self, flair: np.ndarray, segmentation: np.ndarray):
        flair = np.asarray(flair, dtype=np.float32)
        segmentation = self._coerce_segmentation_mask(segmentation)
        if flair.shape[:3] != segmentation.shape[:3]:
            minimum = tuple(min(a, b) for a, b in zip(flair.shape[:3], segmentation.shape[:3]))
            flair = flair[: minimum[0], : minimum[1], : minimum[2]]
            segmentation = segmentation[: minimum[0], : minimum[1], : minimum[2]]

        source_rgb, overlay, _ = self._make_volume_montages(flair, segmentation)
        findings = self._volume_component_findings(segmentation)

        subregion_names = []
        for value, label in ((1, "tumor core"), (2, "whole tumor"), (4, "enhancing tumor")):
            if np.any(segmentation == value):
                subregion_names.append(label)

        source_filename = self._save_overlay(source_rgb, "volume-source", ".png")
        overlay_filename = self._save_overlay(overlay, "volume-segmentation", ".png")
        return source_filename, overlay_filename, findings[:8], subregion_names

    def _volume_component_findings(self, segmentation: np.ndarray) -> List[Finding]:
        """Return one finding per 3D region, not one per flattened projection."""
        mask = np.asarray(segmentation > 0, dtype=bool)
        labels, count = self._label_volume_regions(mask)
        if labels is None:
            projection = np.max(mask, axis=2)
            return self._components_to_findings(projection, projection.shape)

        findings = []
        volume_size = float(max(1, mask.size))
        for index in range(1, count + 1):
            coordinates = np.where(labels == index)
            voxel_count = int(coordinates[0].size)
            if voxel_count == 0:
                continue

            y_start, y_end = int(coordinates[0].min()), int(coordinates[0].max()) + 1
            x_start, x_end = int(coordinates[1].min()), int(coordinates[1].max()) + 1
            center_x = int(np.mean(coordinates[1]))
            center_y = int(np.mean(coordinates[0]))
            region = self._scan_region_name(center_x, center_y, mask.shape[1], mask.shape[0])
            findings.append(
                Finding(
                    label=f"possible tumor region in the {region}",
                    confidence=0.0,
                    x=x_start,
                    y=y_start,
                    width=x_end - x_start,
                    height=y_end - y_start,
                    area_ratio=voxel_count / volume_size,
                )
            )
        findings.sort(key=lambda item: item.area_ratio, reverse=True)
        return findings

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

        # This projection preserves every segmented location, including lesions
        # outside the three representative cross-sectional slices above.
        overview_image = np.rot90(np.max(flair, axis=2))
        overview_mask = np.rot90(np.max(segmentation, axis=2))
        views.append(("ALL DETECTED REGIONS", overview_image, overview_mask))

        source_tiles = []
        overlay_tiles = []
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        for label, image, mask in views:
            normalized = clahe.apply(self._normalize_to_uint8(image))
            source = cv2.cvtColor(normalized, cv2.COLOR_GRAY2RGB)
            marked = self._volume_thermal_overlay(source, mask)
            source_tiles.append(self._volume_view_tile(source, label))
            overlay_tiles.append(self._volume_view_tile(marked, label))
        return np.hstack(source_tiles), np.hstack(overlay_tiles), overview_mask

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

    def _volume_thermal_overlay(self, rgb: np.ndarray, labels: np.ndarray) -> np.ndarray:
        """Keep tumor subregions visible with progressively hotter thermal intensity."""
        labels = np.asarray(labels, dtype=np.uint8)
        thermal_levels = np.zeros(labels.shape, dtype=np.float32)
        thermal_levels[labels == 2] = 0.46
        thermal_levels[labels == 1] = 0.72
        thermal_levels[labels == 4] = 1.0
        return self._thermal_overlay(rgb, labels > 0, thermal_levels)

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

    def _split_composite_mri_panels(self, rgb: np.ndarray) -> List[np.ndarray]:
        """Split a clearly separated MRI contact sheet without guessing a grid."""
        height, width = rgb.shape[:2]
        if min(height, width) < 256:
            return [rgb]

        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        x_cuts = self._panel_gutter_cuts(gray, axis=0)
        y_cuts = self._panel_gutter_cuts(gray, axis=1)
        if not x_cuts and not y_cuts:
            return [rgb]

        x_edges = [0, *x_cuts, width]
        y_edges = [0, *y_cuts, height]
        panel_count = (len(x_edges) - 1) * (len(y_edges) - 1)
        if panel_count < 2 or panel_count > 4:
            return [rgb]

        panels = []
        for row in range(len(y_edges) - 1):
            for column in range(len(x_edges) - 1):
                panel = rgb[y_edges[row] : y_edges[row + 1], x_edges[column] : x_edges[column + 1]]
                try:
                    self._validate_slice_input(panel)
                except ValueError:
                    return [rgb]
                panels.append(panel)
        return panels

    def _panel_gutter_cuts(self, gray: np.ndarray, axis: int) -> List[int]:
        """Locate full-height or full-width neutral gutters in a contact sheet."""
        dimension = gray.shape[1] if axis == 0 else gray.shape[0]
        if dimension < 256:
            return []

        reduce_axis = 0 if axis == 0 else 1
        dark_fraction = np.mean(gray <= 8, axis=reduce_axis)
        bright_fraction = np.mean(gray >= 247, axis=reduce_axis)
        flat = (dark_fraction >= 0.97) | (bright_fraction >= 0.97)
        minimum_gutter = max(2, int(dimension * 0.006))
        edge_margin = int(dimension * 0.12)

        candidates = []
        start = None
        for index, is_flat in enumerate(flat):
            if is_flat and start is None:
                start = index
            elif not is_flat and start is not None:
                if index - start >= minimum_gutter:
                    midpoint = (start + index - 1) // 2
                    if edge_margin < midpoint < dimension - edge_margin:
                        candidates.append(midpoint)
                start = None
        if start is not None and dimension - start >= minimum_gutter:
            midpoint = (start + dimension - 1) // 2
            if edge_margin < midpoint < dimension - edge_margin:
                candidates.append(midpoint)

        accepted = []
        minimum_panel = max(128, int(dimension * 0.18))
        for candidate in candidates:
            trial = sorted([*accepted, candidate])
            edges = [0, *trial, dimension]
            if min(right - left for left, right in zip(edges, edges[1:])) >= minimum_panel:
                accepted.append(candidate)
            if len(accepted) == 3:
                break
        return accepted

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

    def _make_study_contact_sheet(self, images: Iterable[Tuple[str, np.ndarray]]) -> np.ndarray:
        tile_width = 360
        tile_height = 250
        header_height = 38
        tiles = []
        for label, image in images:
            tile = np.full((tile_height, tile_width, 3), (7, 15, 18), dtype=np.uint8)
            height, width = image.shape[:2]
            scale = min((tile_width - 18) / max(1, width), (tile_height - header_height - 12) / max(1, height))
            resized = cv2.resize(
                image,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
            x = (tile_width - resized.shape[1]) // 2
            y = header_height + (tile_height - header_height - resized.shape[0]) // 2
            tile[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
            cv2.rectangle(tile, (0, 0), (tile_width, header_height), (20, 29, 34), -1)
            cv2.putText(tile, label, (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (245, 249, 248), 2, cv2.LINE_AA)
            tiles.append(tile)

        if not tiles:
            return np.full((tile_height, tile_width, 3), (7, 15, 18), dtype=np.uint8)
        columns = min(2, len(tiles))
        rows = int(np.ceil(len(tiles) / columns))
        sheet = np.full((rows * tile_height, columns * tile_width, 3), 12, dtype=np.uint8)
        for index, tile in enumerate(tiles):
            row, column = divmod(index, columns)
            sheet[row * tile_height : (row + 1) * tile_height, column * tile_width : (column + 1) * tile_width] = tile
        return sheet

    def _slice_mode_message(self, findings: List[Finding]) -> str:
        if findings:
            return "Every possible region detected in the submitted MRI view or views is marked for clinical review."
        return "No region was highlighted in this individual view. Use the aligned 3D MRI study for a complete review."

    def _error_result(
        self,
        mode: str,
        input_type: str,
        started: float,
        exc: Exception,
        model_name: Optional[str] = None,
    ) -> AnalysisResult:
        LOGGER.error(
            "MRI review failed for %s input: %s",
            mode,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        volume_model = model_name == self.volume_model_name
        return AnalysisResult(
            status="error",
            mode=mode,
            input_type=input_type,
            model_name=model_name or self.quick_model_name,
            message="Unable to complete this review. Verify that the input is a usable brain MRI study and try again.",
            inference_ms=int((time.time() - started) * 1000),
            model_loaded=self._monai_bundle_ready() if volume_model else self._slice_model is not None,
        )
