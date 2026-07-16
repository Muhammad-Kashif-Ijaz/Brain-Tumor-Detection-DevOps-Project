const state = {
  busy: false,
  cameraReady: false,
  cameraStream: null,
  sourceObjectUrl: null,
  liveTimer: null,
  animationFrame: null,
  resultAvailable: false,
};

const singleForm = document.getElementById("singleForm");
const scanInput = document.getElementById("scanInput");
const dropzone = document.getElementById("dropzone");
const singleFileName = document.getElementById("singleFileName");
const sourceStatus = document.getElementById("sourceStatus");
const sourceLine = document.querySelector(".source-line");
const sourceImage = document.getElementById("sourceImage");
const sourceVideo = document.getElementById("sourceVideo");
const sourceEmpty = document.getElementById("sourceEmpty");
const sourceCaption = document.getElementById("sourceCaption");
const resultImage = document.getElementById("resultImage");
const resultEmpty = document.getElementById("resultEmpty");
const resultFrame = document.querySelector(".result-frame");
const resultStatus = document.getElementById("resultStatus");
const resultNarrative = document.getElementById("resultNarrative");
const scanState = document.getElementById("scanState");
const processingLabel = document.getElementById("processingLabel");
const downloadResult = document.getElementById("downloadResult");
const analyzeButton = document.getElementById("analyzeButton");
const newStudy = document.getElementById("newStudy");
const viewerPanel = document.getElementById("viewerPanel");
const toast = document.getElementById("toast");
const cameraFeed = document.getElementById("cameraFeed");
const cameraEmpty = document.getElementById("cameraEmpty");
const cameraCaptureInput = document.getElementById("cameraCaptureInput");
const captureCanvas = document.getElementById("captureCanvas");
const startCamera = document.getElementById("startCamera");
const captureFrame = document.getElementById("captureFrame");
const continuousToggle = document.getElementById("continuousToggle");
const liveIndicator = document.getElementById("liveIndicator");
const cameraHelp = document.getElementById("cameraHelp");
const mriCanvas = document.getElementById("mriCanvas");
const ctx = mriCanvas.getContext("2d");

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("show"), 4200);
}

function setScanState(message) {
  scanState.textContent = message;
}

function setBusy(isBusy) {
  state.busy = isBusy;
  document.body.classList.toggle("is-analyzing", isBusy);
  analyzeButton.disabled = isBusy;
  startCamera.disabled = isBusy;
  captureFrame.disabled = isBusy || !state.cameraReady;
  continuousToggle.disabled = isBusy || !state.cameraReady;
  processingLabel.hidden = !isBusy;
  resultFrame.classList.toggle("is-processing", isBusy);

  if (isBusy) {
    resultEmpty.hidden = true;
    resultStatus.textContent = "Reviewing image";
    resultNarrative.textContent = "Preparing a marked image for side-by-side review.";
    setScanState("Review in progress");
  }
}

function revokeSourceUrl() {
  if (state.sourceObjectUrl) {
    URL.revokeObjectURL(state.sourceObjectUrl);
    state.sourceObjectUrl = null;
  }
}

function hideSourceMedia() {
  sourceImage.hidden = true;
  sourceImage.removeAttribute("src");
  sourceVideo.pause();
  sourceVideo.hidden = true;
  sourceVideo.removeAttribute("src");
  sourceVideo.load();
}

function showSourcePlaceholder() {
  hideSourceMedia();
  sourceEmpty.hidden = false;
  sourceCaption.textContent = "Source view";
}

function showSourceImage(url, caption = "Image view") {
  hideSourceMedia();
  sourceImage.src = url;
  sourceImage.hidden = false;
  sourceEmpty.hidden = true;
  sourceCaption.textContent = caption;
}

function showSourceVideo(url, caption = "Video view") {
  hideSourceMedia();
  sourceVideo.src = url;
  sourceVideo.hidden = false;
  sourceEmpty.hidden = true;
  sourceCaption.textContent = caption;
}

function setSourceFromFile(file) {
  revokeSourceUrl();
  clearResult();

  if (!file) {
    singleFileName.textContent = "Drop an MRI image here";
    sourceStatus.textContent = "No study selected";
    sourceLine.classList.remove("has-source");
    showSourcePlaceholder();
    setScanState("Ready for an image");
    return;
  }

  singleFileName.textContent = file.name;
  sourceStatus.textContent = `${file.type.startsWith("video/") ? "Video" : "Image"} selected`;
  sourceLine.classList.add("has-source");
  state.sourceObjectUrl = URL.createObjectURL(file);

  if (file.type.startsWith("video/")) {
    showSourceVideo(state.sourceObjectUrl, "Original video");
  } else {
    showSourceImage(state.sourceObjectUrl, "Original image");
  }

  setScanState("Study ready for review");
}

function clearResult() {
  state.resultAvailable = false;
  resultImage.hidden = true;
  resultImage.removeAttribute("src");
  resultEmpty.hidden = false;
  mriCanvas.hidden = false;
  resultFrame.classList.remove("has-result", "is-processing");
  resultStatus.textContent = "Awaiting study";
  resultNarrative.textContent = "Add a study to create a thermal review image.";
  downloadResult.href = "#";
  downloadResult.removeAttribute("download");
  downloadResult.setAttribute("aria-disabled", "true");
  downloadResult.classList.add("is-disabled");
  startPreviewAnimation();
}

async function readResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    throw new Error("The review service returned an unreadable response.");
  }

  const data = await response.json();
  if (!response.ok || data.status !== "ok") {
    throw new Error(data.message || "The submitted study could not be reviewed.");
  }
  return data;
}

async function postForm(formData) {
  setBusy(true);
  try {
    const response = await fetch("/api/analyze", { method: "POST", body: formData });
    renderResult(await readResponse(response));
  } catch (error) {
    renderError(error.message);
  } finally {
    setBusy(false);
  }
}

async function postLiveFrame(frame, silent = false) {
  setBusy(true);
  try {
    const response = await fetch("/api/live-frame", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frame }),
    });
    renderResult(await readResponse(response), silent);
  } catch (error) {
    renderError(error.message);
    continuousToggle.checked = false;
    stopContinuousReview();
  } finally {
    setBusy(false);
  }
}

function resultNarrativeFor(data, findings) {
  if (findings.length) {
    return data.message || "Possible tumor region markings are visible on the thermal result image.";
  }
  return data.message || "No possible tumor region was highlighted in this submitted view.";
}

function renderResult(data, silent = false) {
  const findings = Array.isArray(data.findings) ? data.findings : [];

  if (!data.overlay_url) {
    renderError("The review finished without a result image. Please try the study again.");
    return;
  }

  if (data.source_preview_url) {
    showSourceImage(`${data.source_preview_url}?t=${Date.now()}`, "Volume preview");
  }

  stopPreviewAnimation();
  resultImage.src = `${data.overlay_url}?t=${Date.now()}`;
  resultImage.hidden = false;
  resultEmpty.hidden = true;
  mriCanvas.hidden = true;
  resultFrame.classList.add("has-result");
  state.resultAvailable = true;

  resultStatus.textContent = findings.length ? "Thermal result ready" : "Review complete";
  resultNarrative.textContent = resultNarrativeFor(data, findings);
  setScanState("Result ready for review");

  downloadResult.href = data.overlay_url;
  downloadResult.download = "cerebravue-thermal-review.jpg";
  downloadResult.setAttribute("aria-disabled", "false");
  downloadResult.classList.remove("is-disabled");

  if (!silent) {
    viewerPanel.scrollIntoView({ behavior: "smooth", block: "center" });
    showToast("Thermal review image is ready.");
  }
}

function renderError(message) {
  state.resultAvailable = false;
  resultStatus.textContent = "Review unavailable";
  resultNarrative.textContent = message;
  setScanState("Study needs attention");
  resultFrame.classList.remove("has-result");
  resultImage.hidden = true;
  resultEmpty.hidden = false;
  mriCanvas.hidden = false;
  startPreviewAnimation();
  showToast(message);
}

function cameraCanRun() {
  return Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
}

function refreshCameraState() {
  const available = cameraCanRun();
  startCamera.disabled = state.busy;
  captureFrame.disabled = state.busy || !state.cameraReady;
  continuousToggle.disabled = state.busy || !state.cameraReady;
  cameraEmpty.hidden = state.cameraReady;

  if (!available) {
    startCamera.textContent = "Choose camera image";
    cameraHelp.textContent = "Live preview needs HTTPS or localhost. You can still choose a camera image.";
    liveIndicator.classList.remove("active");
    liveIndicator.innerHTML = "<i></i> Device image";
    return;
  }

  startCamera.textContent = state.cameraReady ? "Restart camera" : "Start camera";
  cameraHelp.textContent = state.cameraReady
    ? "Preview is active. Capture one frame or enable continuing review."
    : "Camera capture is available on secure or local connections.";
  liveIndicator.classList.toggle("active", state.cameraReady);
  liveIndicator.innerHTML = `<i></i> ${state.cameraReady ? (continuousToggle.checked ? "Reviewing" : "Preview on") : "Standby"}`;
}

function captureCurrentFrame() {
  const width = cameraFeed.videoWidth;
  const height = cameraFeed.videoHeight;
  if (!width || !height) {
    throw new Error("The camera preview is still starting.");
  }
  captureCanvas.width = width;
  captureCanvas.height = height;
  captureCanvas.getContext("2d").drawImage(cameraFeed, 0, 0, width, height);
  return captureCanvas.toDataURL("image/jpeg", 0.92);
}

async function analyzeCurrentCameraFrame(silent = false) {
  if (!state.cameraReady || !state.cameraStream) {
    throw new Error("Start the camera preview first.");
  }
  const frame = captureCurrentFrame();
  revokeSourceUrl();
  clearResult();
  showSourceImage(frame, "Captured frame");
  singleFileName.textContent = "Captured camera frame";
  sourceStatus.textContent = "Camera frame selected";
  sourceLine.classList.add("has-source");
  setScanState("Camera frame ready for review");
  await postLiveFrame(frame, silent);
}

function stopContinuousReview() {
  window.clearTimeout(state.liveTimer);
  state.liveTimer = null;
  if (!state.cameraReady) {
    continuousToggle.checked = false;
  }
  refreshCameraState();
}

function scheduleContinuousReview(delay = 600) {
  window.clearTimeout(state.liveTimer);
  if (!continuousToggle.checked || !state.cameraReady) {
    refreshCameraState();
    return;
  }

  refreshCameraState();
  state.liveTimer = window.setTimeout(async () => {
    if (!continuousToggle.checked || !state.cameraReady) {
      stopContinuousReview();
      return;
    }

    if (!state.busy) {
      try {
        await analyzeCurrentCameraFrame(true);
      } catch (error) {
        continuousToggle.checked = false;
        stopContinuousReview();
        renderError(error.message);
        return;
      }
    }

    scheduleContinuousReview(4200);
  }, delay);
}

function stopCamera() {
  window.clearTimeout(state.liveTimer);
  state.liveTimer = null;
  continuousToggle.checked = false;

  if (state.cameraStream) {
    state.cameraStream.getTracks().forEach((track) => track.stop());
  }
  state.cameraStream = null;
  state.cameraReady = false;
  cameraFeed.srcObject = null;
  refreshCameraState();
}

function drawMriPreview(time) {
  const width = mriCanvas.width;
  const height = mriCanvas.height;
  const centerX = width / 2;
  const centerY = height / 2;
  const pulse = (Math.sin(time / 1450) + 1) / 2;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#13242d";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "rgba(150, 206, 255, 0.05)";
  ctx.lineWidth = 1;
  for (let x = 20; x < width; x += 32) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
  for (let y = 20; y < height; y += 32) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }

  ctx.save();
  ctx.translate(centerX, centerY + 8);
  const brainGradient = ctx.createRadialGradient(-68, -70, 16, 0, 0, 270);
  brainGradient.addColorStop(0, "rgba(223, 239, 255, 0.8)");
  brainGradient.addColorStop(0.48, "rgba(123, 153, 192, 0.7)");
  brainGradient.addColorStop(0.82, "rgba(39, 63, 94, 0.92)");
  brainGradient.addColorStop(1, "rgba(8, 18, 31, 0.98)");
  ctx.fillStyle = brainGradient;
  ctx.beginPath();
  ctx.moveTo(0, -238);
  ctx.bezierCurveTo(145, -235, 226, -124, 214, 33);
  ctx.bezierCurveTo(204, 172, 105, 231, 0, 224);
  ctx.bezierCurveTo(-105, 231, -204, 172, -214, 33);
  ctx.bezierCurveTo(-226, -124, -145, -235, 0, -238);
  ctx.fill();

  ctx.strokeStyle = "rgba(224, 240, 255, 0.13)";
  ctx.lineWidth = 2;
  for (let index = 0; index < 9; index += 1) {
    const offset = (index - 4) * 42;
    ctx.beginPath();
    ctx.ellipse(offset, Math.sin(time / 1600 + index) * 2, 21, 151 - Math.abs(index - 4) * 10, 0, 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.beginPath();
  ctx.ellipse(-42, -2, 31, 66, -0.09, 0, Math.PI * 2);
  ctx.ellipse(42, -2, 31, 66, 0.09, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(9, 23, 29, 0.7)";
  ctx.fill();

  ctx.strokeStyle = `rgba(111, 186, 255, ${0.2 + pulse * 0.22})`;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.ellipse(0, 0, 238 + pulse * 2, 263 + pulse * 2, 0, 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();

  const scanY = ((time / 6) % (height + 130)) - 65;
  const scanGradient = ctx.createLinearGradient(0, scanY - 30, 0, scanY + 30);
  scanGradient.addColorStop(0, "rgba(104, 181, 255, 0)");
  scanGradient.addColorStop(0.5, "rgba(104, 181, 255, 0.12)");
  scanGradient.addColorStop(1, "rgba(104, 181, 255, 0)");
  ctx.fillStyle = scanGradient;
  ctx.fillRect(0, scanY - 30, width, 60);

  state.animationFrame = requestAnimationFrame(drawMriPreview);
}

function startPreviewAnimation() {
  if (!state.animationFrame) {
    state.animationFrame = requestAnimationFrame(drawMriPreview);
  }
}

function stopPreviewAnimation() {
  if (state.animationFrame) {
    cancelAnimationFrame(state.animationFrame);
    state.animationFrame = null;
  }
}

singleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const file = scanInput.files[0];
  if (!file) {
    showToast("Choose an MRI image or video first.");
    return;
  }
  setSourceFromFile(file);
  postForm(new FormData(singleForm));
});

scanInput.addEventListener("change", () => setSourceFromFile(scanInput.files[0]));

["dragenter", "dragover"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
  });
});

dropzone.addEventListener("drop", (event) => {
  const [file] = event.dataTransfer.files;
  if (!file) {
    return;
  }

  const transfer = new DataTransfer();
  transfer.items.add(file);
  scanInput.files = transfer.files;
  setSourceFromFile(file);
});

startCamera.addEventListener("click", async () => {
  if (!cameraCanRun()) {
    cameraCaptureInput.click();
    return;
  }

  try {
    stopCamera();
    state.cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
    cameraFeed.srcObject = state.cameraStream;
    await cameraFeed.play();
    state.cameraReady = true;
    refreshCameraState();
    showToast("Camera preview started.");
  } catch (error) {
    state.cameraReady = false;
    refreshCameraState();
    showToast(`Camera unavailable. ${error.message}`);
  }
});

cameraCaptureInput.addEventListener("change", () => {
  const file = cameraCaptureInput.files[0];
  if (!file) {
    return;
  }

  revokeSourceUrl();
  state.sourceObjectUrl = URL.createObjectURL(file);
  clearResult();
  showSourceImage(state.sourceObjectUrl, "Captured image");
  singleFileName.textContent = "Captured camera image";
  sourceStatus.textContent = "Camera image selected";
  sourceLine.classList.add("has-source");
  setScanState("Camera image ready for review");

  const formData = new FormData();
  formData.append("scan", file, file.name || "camera-image.jpg");
  postForm(formData);
});

captureFrame.addEventListener("click", async () => {
  try {
    await analyzeCurrentCameraFrame();
  } catch (error) {
    showToast(error.message);
  }
});

continuousToggle.addEventListener("change", () => {
  if (!continuousToggle.checked) {
    stopContinuousReview();
    showToast("Continuous frame review paused.");
    return;
  }

  if (!state.cameraReady) {
    continuousToggle.checked = false;
    showToast("Start the camera preview first.");
    return;
  }

  scheduleContinuousReview();
  showToast("Continuous frame review started.");
});

newStudy.addEventListener("click", () => {
  stopCamera();
  revokeSourceUrl();
  singleForm.reset();
  cameraCaptureInput.value = "";
  showSourcePlaceholder();
  clearResult();
  singleFileName.textContent = "Drop an MRI image here";
  sourceStatus.textContent = "No study selected";
  sourceLine.classList.remove("has-source");
  setScanState("Ready for an image");
  document.getElementById("scan").scrollIntoView({ behavior: "smooth", block: "start" });
  showToast("New scan is ready.");
});

downloadResult.addEventListener("click", (event) => {
  if (downloadResult.getAttribute("aria-disabled") === "true") {
    event.preventDefault();
    showToast("Analyze a study before downloading a result.");
  }
});

resultImage.addEventListener("error", () => {
  renderError("The result image could not be loaded. Analyze the study again.");
});

window.addEventListener("beforeunload", () => {
  stopCamera();
  revokeSourceUrl();
  stopPreviewAnimation();
});

refreshCameraState();
showSourcePlaceholder();
clearResult();
startPreviewAnimation();
