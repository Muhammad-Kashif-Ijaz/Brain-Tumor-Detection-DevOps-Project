const state = {
  busy: false,
  cameraReady: false,
  cameraStream: null,
  sourceObjectUrl: null,
  animationFrame: null,
  studyName: "Unassigned MRI",
};

const tabs = document.querySelectorAll(".mode-tab");
const panes = document.querySelectorAll(".mode-pane");
const viewModeButtons = document.querySelectorAll(".view-mode");
const singleForm = document.getElementById("singleForm");
const volumeForm = document.getElementById("volumeForm");
const scanInput = document.getElementById("scanInput");
const dropzone = document.getElementById("dropzone");
const singleFileName = document.getElementById("singleFileName");
const sourceImage = document.getElementById("sourceImage");
const sourceVideo = document.getElementById("sourceVideo");
const sourceEmpty = document.getElementById("sourceEmpty");
const sourceStatus = document.getElementById("sourceStatus");
const resultImage = document.getElementById("resultImage");
const resultStatus = document.getElementById("resultStatus");
const resultNarrative = document.getElementById("resultNarrative");
const resultFeed = document.getElementById("resultFeed");
const subregionList = document.getElementById("subregionList");
const viewerTitle = document.getElementById("viewerTitle");
const scanChip = document.getElementById("scanChip");
const metricStatus = document.getElementById("metricStatus");
const metricRegions = document.getElementById("metricRegions");
const metricLatency = document.getElementById("metricLatency");
const boardBoundary = document.getElementById("boardBoundary");
const boardLocation = document.getElementById("boardLocation");
const boardState = document.getElementById("boardState");
const comparisonGrid = document.getElementById("comparisonGrid");
const viewerPanel = document.getElementById("viewerPanel");
const processingLabel = document.getElementById("processingLabel");
const downloadResult = document.getElementById("downloadResult");
const newStudy = document.getElementById("newStudy");
const focusViewer = document.getElementById("focusViewer");
const stageAcquire = document.getElementById("stageAcquire");
const stageAnalyze = document.getElementById("stageAnalyze");
const stageReview = document.getElementById("stageReview");
const toast = document.getElementById("toast");
const cameraFeed = document.getElementById("cameraFeed");
const cameraEmpty = document.getElementById("cameraEmpty");
const cameraHelp = document.getElementById("cameraHelp");
const cameraCaptureInput = document.getElementById("cameraCaptureInput");
const captureCanvas = document.getElementById("captureCanvas");
const startCamera = document.getElementById("startCamera");
const captureFrame = document.getElementById("captureFrame");
const mriCanvas = document.getElementById("mriCanvas");
const ctx = mriCanvas.getContext("2d");

function setMode(mode) {
  tabs.forEach((tab) => {
    const active = tab.dataset.mode === mode;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  panes.forEach((pane) => pane.classList.toggle("active", pane.dataset.pane === mode));
  if (mode === "live") {
    refreshCameraState();
  }
}

function setViewMode(mode) {
  viewModeButtons.forEach((button) => button.classList.toggle("active", button.dataset.view === mode));
  comparisonGrid.classList.toggle("result-only", mode === "result");
}

function cameraCanRun() {
  return Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
}

function refreshCameraState() {
  const liveAvailable = cameraCanRun();
  startCamera.disabled = state.busy;
  captureFrame.disabled = state.busy || !state.cameraReady;

  if (!liveAvailable) {
    startCamera.textContent = "Open device camera";
    cameraHelp.textContent = "Live preview needs HTTPS. Device capture remains available here.";
    if (!state.cameraReady) {
      cameraEmpty.hidden = false;
      cameraEmpty.querySelector("strong").textContent = "Device camera capture";
    }
    return;
  }

  startCamera.textContent = state.cameraReady ? "Restart camera" : "Start camera";
  cameraHelp.textContent = state.cameraReady
    ? "Camera is active. Frame the MRI display and analyze one capture."
    : "Start the live preview, then capture one frame for review.";
  cameraEmpty.hidden = state.cameraReady;
  if (!state.cameraReady) {
    cameraEmpty.querySelector("strong").textContent = "Camera preview";
  }
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("show"), 3800);
}

function setProgress(step) {
  const order = [stageAcquire, stageAnalyze, stageReview];
  const activeIndex = Math.max(0, order.indexOf(step));
  order.forEach((item, index) => {
    item.classList.toggle("active", index === activeIndex);
    item.classList.toggle("complete", index < activeIndex);
  });
}

function setBusy(isBusy, label = "Reviewing") {
  state.busy = isBusy;
  document.body.classList.toggle("is-analyzing", isBusy);
  processingLabel.hidden = !isBusy;

  document.querySelectorAll("button").forEach((button) => {
    if (button.hasAttribute("data-keep-enabled") || button.classList.contains("view-mode")) {
      return;
    }
    button.disabled = isBusy;
  });
  refreshCameraState();

  if (isBusy) {
    metricStatus.textContent = label;
    metricRegions.textContent = "Mapping in progress";
    metricLatency.textContent = "Await result";
    scanChip.innerHTML = "<i></i> Reviewing study";
    resultStatus.textContent = "Preparing thermal map";
    resultNarrative.textContent = "The study is being reviewed and a marked comparison image is being prepared.";
    boardBoundary.textContent = "Mapping";
    boardLocation.textContent = "Reviewing study";
    boardState.textContent = "Processing";
    setProgress(stageAnalyze);
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

function showSourceImage(url, statusText) {
  hideSourceMedia();
  sourceImage.src = url;
  sourceImage.hidden = false;
  sourceEmpty.hidden = true;
  sourceStatus.textContent = statusText;
}

function showSourceVideo(url, statusText) {
  hideSourceMedia();
  sourceVideo.src = url;
  sourceVideo.hidden = false;
  sourceEmpty.hidden = true;
  sourceStatus.textContent = statusText;
}

function showSourcePlaceholder(title, statusText) {
  hideSourceMedia();
  sourceEmpty.hidden = false;
  sourceEmpty.innerHTML = `
    <span class="empty-scan" aria-hidden="true"><i></i></span>
    <strong>${escapeHtml(title)}</strong>
    <small>The source preview will appear when the study is ready.</small>
  `;
  sourceStatus.textContent = statusText;
}

function setStudyName(name) {
  state.studyName = name || "Unassigned MRI";
  viewerTitle.textContent = state.studyName;
}

function setSourceFromFile(file) {
  revokeSourceUrl();
  if (!file) {
    setStudyName("Unassigned MRI");
    showSourcePlaceholder("Source viewport", "No study loaded");
    return;
  }

  setStudyName(file.name);
  if (file.type.startsWith("image/")) {
    state.sourceObjectUrl = URL.createObjectURL(file);
    showSourceImage(state.sourceObjectUrl, "Image ready for review");
  } else if (file.type.startsWith("video/")) {
    state.sourceObjectUrl = URL.createObjectURL(file);
    showSourceVideo(state.sourceObjectUrl, "Video ready for sampling");
  } else {
    showSourcePlaceholder("Volume study selected", "MRI series selected");
  }
  setProgress(stageAcquire);
}

function clearResultPreview() {
  resultImage.hidden = true;
  resultImage.classList.remove("result-reveal");
  resultImage.removeAttribute("src");
  mriCanvas.hidden = false;
  downloadResult.classList.add("disabled");
  downloadResult.setAttribute("aria-disabled", "true");
  downloadResult.href = "#";
  resultStatus.textContent = "Awaiting analysis";
  scanChip.innerHTML = "<i></i> Ready for acquisition";
  metricStatus.textContent = "Ready";
  metricRegions.textContent = "Not generated";
  metricLatency.textContent = "Analyze study";
  resultNarrative.textContent = "Load a study to begin the review.";
  boardBoundary.textContent = "Pending";
  boardLocation.textContent = "Awaiting study";
  boardState.textContent = "Open";
  resultFeed.innerHTML = findingCard("No study loaded", "Possible tumor locations will appear here after analysis.", true);
  subregionList.innerHTML = "<span>Source not reviewed</span>";
  setProgress(stageAcquire);
}

async function readResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    throw new Error("The analysis service returned an unreadable response.");
  }
  const data = await response.json();
  if (!response.ok || data.status !== "ok") {
    throw new Error(data.message || "The study could not be analyzed.");
  }
  return data;
}

async function postForm(formData, label = "Reviewing") {
  setBusy(true, label);
  try {
    const response = await fetch("/api/analyze", { method: "POST", body: formData });
    renderResult(await readResponse(response));
  } catch (error) {
    renderError(error.message);
  } finally {
    setBusy(false);
  }
}

async function postLiveFrame(frame) {
  setBusy(true, "Reviewing frame");
  try {
    const response = await fetch("/api/live-frame", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frame }),
    });
    renderResult(await readResponse(response));
  } catch (error) {
    renderError(error.message);
  } finally {
    setBusy(false);
  }
}

function renderResult(data) {
  const findings = Array.isArray(data.findings) ? data.findings : [];
  const details = data.details || {};

  metricStatus.textContent = "Complete";
  metricRegions.textContent = findings.length ? "Review focus marked" : "No clear focus marked";
  metricLatency.textContent = "Clinician review";
  scanChip.innerHTML = "<i></i> Result ready";
  resultStatus.textContent = "Thermal image ready";
  boardBoundary.textContent = findings.length ? "Marked" : "No mark";
  boardLocation.textContent = findings.length ? "Location summarized" : "No clear focus";
  boardState.textContent = "Ready";

  if (data.source_preview_url) {
    showSourceImage(`${data.source_preview_url}?t=${Date.now()}`, "Volume preview ready");
  }

  if (!data.overlay_url) {
    renderError("The review completed without a result image.");
    return;
  }

  resultImage.src = `${data.overlay_url}?t=${Date.now()}`;
  resultImage.hidden = false;
  resultImage.classList.remove("result-reveal");
  void resultImage.offsetWidth;
  resultImage.classList.add("result-reveal");
  mriCanvas.hidden = true;

  downloadResult.href = data.overlay_url;
  downloadResult.download = "neuroscope-thermal-review";
  downloadResult.classList.remove("disabled");
  downloadResult.setAttribute("aria-disabled", "false");

  if (!findings.length) {
    resultNarrative.textContent = "No clear tumor focus was segmented in this view. Review the complete examination when clinical concern remains.";
    resultFeed.innerHTML = findingCard("No highlighted tumor region", data.message, true);
  } else {
    resultNarrative.textContent = "Possible tumor tissue is marked on the thermal image. Compare every highlighted area with the source study.";
    resultFeed.innerHTML = findings
      .map((finding) => findingCard(capitalizeSentence(finding.label), "Thermal boundary shown in the comparison viewer.", false))
      .join("");
  }

  const subregions = Array.isArray(details.subregions) ? details.subregions : [];
  const scope = details.review_scope ? [details.review_scope] : [];
  const labels = [...subregions, ...scope];
  subregionList.innerHTML = labels.length
    ? labels.map((label) => `<span>${escapeHtml(capitalizeSentence(label))}</span>`).join("")
    : "<span>Single view reviewed</span>";

  setProgress(stageReview);
  setViewMode("compare");
  showToast(data.message || "MRI review complete");
}

function renderError(message) {
  metricStatus.textContent = "Needs attention";
  metricRegions.textContent = "Result unavailable";
  metricLatency.textContent = "Check study";
  scanChip.innerHTML = "<i></i> Review stopped";
  resultStatus.textContent = "Result unavailable";
  resultNarrative.textContent = message;
  boardBoundary.textContent = "Unavailable";
  boardLocation.textContent = "Check files";
  boardState.textContent = "Attention";
  resultFeed.innerHTML = findingCard("Review could not complete", message, true);
  subregionList.innerHTML = "<span>Study requires attention</span>";
  setProgress(stageAcquire);
  showToast(message);
}

function findingCard(title, detail, empty) {
  return `
    <article class="finding-card ${empty ? "empty" : ""}">
      <span class="finding-marker"></span>
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(detail || "Review the marked image with a qualified clinician.")}</p>
      </div>
    </article>
  `;
}

function capitalizeSentence(value) {
  const text = String(value || "").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[character]);
}

function drawMriPreview(time) {
  const width = mriCanvas.width;
  const height = mriCanvas.height;
  const centerX = width / 2;
  const centerY = height / 2 + 8;
  const pulse = Math.sin(time / 1100) * 0.5 + 0.5;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#071013";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "rgba(118, 199, 192, 0.05)";
  ctx.lineWidth = 1;
  for (let x = 0; x < width; x += 32) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
  for (let y = 0; y < height; y += 32) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }

  ctx.save();
  ctx.translate(centerX, centerY);

  const outer = ctx.createRadialGradient(-45, -60, 25, 0, 0, 305);
  outer.addColorStop(0, "rgba(229, 239, 237, 0.86)");
  outer.addColorStop(0.46, "rgba(149, 170, 167, 0.7)");
  outer.addColorStop(0.78, "rgba(66, 88, 88, 0.76)");
  outer.addColorStop(1, "rgba(23, 38, 41, 0.92)");
  ctx.fillStyle = outer;
  ctx.beginPath();
  ctx.moveTo(0, -285);
  ctx.bezierCurveTo(178, -278, 272, -130, 246, 54);
  ctx.bezierCurveTo(224, 208, 106, 286, 0, 270);
  ctx.bezierCurveTo(-106, 286, -224, 208, -246, 54);
  ctx.bezierCurveTo(-272, -130, -178, -278, 0, -285);
  ctx.fill();

  for (let index = 0; index < 12; index += 1) {
    const offset = (index - 5.5) * 32;
    ctx.beginPath();
    ctx.ellipse(offset, Math.sin(time / 1500 + index) * 4, 24 + (index % 3) * 5, 198 - Math.abs(index - 5.5) * 10, 0, 0, Math.PI * 2);
    ctx.strokeStyle = index % 3 === 0 ? "rgba(18, 58, 57, 0.28)" : "rgba(244, 249, 248, 0.17)";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  ctx.beginPath();
  ctx.ellipse(-48, -12, 38, 78, -0.12, 0, Math.PI * 2);
  ctx.ellipse(48, -12, 38, 78, 0.12, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(17, 31, 35, 0.74)";
  ctx.fill();

  ctx.strokeStyle = `rgba(108, 220, 208, ${0.24 + pulse * 0.2})`;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.ellipse(0, 0, 278 + pulse * 3, 318 + pulse * 3, 0, 0, Math.PI * 2);
  ctx.stroke();

  ctx.strokeStyle = "rgba(238, 98, 61, 0.24)";
  ctx.beginPath();
  ctx.moveTo(-300, 0);
  ctx.lineTo(300, 0);
  ctx.moveTo(0, -330);
  ctx.lineTo(0, 330);
  ctx.stroke();
  ctx.restore();

  ctx.fillStyle = "rgba(235, 247, 246, 0.72)";
  ctx.font = "700 17px Inter, system-ui, sans-serif";
  ctx.fillText("NEUROSCOPE MRI", 28, 39);
  ctx.fillStyle = "rgba(112, 208, 198, 0.7)";
  ctx.font = "600 14px Inter, system-ui, sans-serif";
  ctx.fillText("READY FOR STUDY", 28, 65);

  state.animationFrame = requestAnimationFrame(drawMriPreview);
}

tabs.forEach((tab) => tab.addEventListener("click", () => setMode(tab.dataset.mode)));
viewModeButtons.forEach((button) => button.addEventListener("click", () => setViewMode(button.dataset.view)));

singleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const formData = new FormData(singleForm);
  const file = formData.get("scan");
  if (!file || !file.name) {
    showToast("Choose an MRI image or video first.");
    return;
  }
  setSourceFromFile(file);
  postForm(formData);
});

volumeForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const formData = new FormData(volumeForm);
  const missing = ["t1c", "t1", "t2", "flair"].filter((name) => !formData.get(name)?.name);
  if (missing.length) {
    showToast(`Add the missing MRI series: ${missing.join(", ")}.`);
    return;
  }
  setStudyName("Multimodal brain MRI volume");
  showSourcePlaceholder("Volume study selected", "Four MRI series ready");
  clearResultPreview();
  setStudyName("Multimodal brain MRI volume");
  postForm(formData, "Reviewing volume");
});

scanInput.addEventListener("change", () => {
  const file = scanInput.files[0];
  singleFileName.textContent = file ? file.name : "Choose or drop an MRI study";
  setSourceFromFile(file);
  clearResultPreview();
  if (file) {
    setStudyName(file.name);
  }
});

volumeForm.querySelectorAll("input[type='file']").forEach((input) => {
  input.addEventListener("change", () => {
    const selected = [...volumeForm.querySelectorAll("input[type='file']")].filter((field) => field.files.length);
    sourceStatus.textContent = selected.length ? "MRI series being prepared" : "No study loaded";
  });
});

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
  singleFileName.textContent = file.name;
  setSourceFromFile(file);
  clearResultPreview();
  setStudyName(file.name);
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
  setSourceFromFile(file);
  clearResultPreview();
  setStudyName("Captured MRI frame");
  const formData = new FormData();
  formData.append("scan", file, file.name || "camera-frame.jpg");
  postForm(formData, "Reviewing capture");
});

captureFrame.addEventListener("click", () => {
  if (!state.cameraReady || !state.cameraStream) {
    showToast("Start the camera preview first.");
    return;
  }
  const width = cameraFeed.videoWidth;
  const height = cameraFeed.videoHeight;
  if (!width || !height) {
    showToast("The camera preview is still starting.");
    return;
  }
  captureCanvas.width = width;
  captureCanvas.height = height;
  captureCanvas.getContext("2d").drawImage(cameraFeed, 0, 0, width, height);
  const frame = captureCanvas.toDataURL("image/jpeg", 0.92);
  showSourceImage(frame, "Camera frame ready");
  clearResultPreview();
  setStudyName("Captured MRI frame");
  postLiveFrame(frame);
});

function stopCamera() {
  if (state.cameraStream) {
    state.cameraStream.getTracks().forEach((track) => track.stop());
  }
  state.cameraStream = null;
  state.cameraReady = false;
  cameraFeed.srcObject = null;
  refreshCameraState();
}

newStudy.addEventListener("click", () => {
  stopCamera();
  revokeSourceUrl();
  singleForm.reset();
  volumeForm.reset();
  cameraCaptureInput.value = "";
  singleFileName.textContent = "Choose or drop an MRI study";
  setStudyName("Unassigned MRI");
  showSourcePlaceholder("Source viewport", "No study loaded");
  clearResultPreview();
  document.querySelectorAll(".review-checklist input").forEach((input) => { input.checked = false; });
  setMode("single");
  setViewMode("compare");
  showToast("New study workspace ready.");
});

focusViewer.addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else if (viewerPanel.requestFullscreen) {
      await viewerPanel.requestFullscreen();
    } else {
      showToast("Full-screen viewing is not available in this browser.");
    }
  } catch (error) {
    showToast(`Unable to change viewer mode. ${error.message}`);
  }
});

document.addEventListener("fullscreenchange", () => {
  focusViewer.textContent = document.fullscreenElement ? "Exit focus" : "Focus viewer";
});

downloadResult.addEventListener("click", (event) => {
  if (downloadResult.getAttribute("aria-disabled") === "true") {
    event.preventDefault();
    showToast("Analyze a study before exporting a result.");
  }
});

resultImage.addEventListener("error", () => renderError("The thermal result image could not be loaded. Please analyze the study again."));
window.addEventListener("beforeunload", () => {
  stopCamera();
  revokeSourceUrl();
  cancelAnimationFrame(state.animationFrame);
});

refreshCameraState();
setProgress(stageAcquire);
requestAnimationFrame(drawMriPreview);
