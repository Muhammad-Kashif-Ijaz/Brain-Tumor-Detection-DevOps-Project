const state = {
  busy: false,
  cameraStream: null,
  cameraReady: false,
  animationFrame: null,
};

const tabs = document.querySelectorAll(".mode-tab");
const panes = document.querySelectorAll(".mode-pane");
const singleForm = document.getElementById("singleForm");
const volumeForm = document.getElementById("volumeForm");
const scanInput = document.getElementById("scanInput");
const dropzone = document.getElementById("dropzone");
const singleFileName = document.getElementById("singleFileName");
const resultImage = document.getElementById("resultImage");
const viewerTitle = document.getElementById("viewerTitle");
const modelChip = document.getElementById("modelChip");
const metricStatus = document.getElementById("metricStatus");
const metricRegions = document.getElementById("metricRegions");
const metricLatency = document.getElementById("metricLatency");
const resultFeed = document.getElementById("resultFeed");
const toast = document.getElementById("toast");
const cameraFeed = document.getElementById("cameraFeed");
const cameraEmpty = document.getElementById("cameraEmpty");
const cameraHelp = document.getElementById("cameraHelp");
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

  if (mode === "live" && !cameraCanRun()) {
    showToast("Live camera needs HTTPS or localhost. Upload MRI scans normally from the MRI scan tab.");
  }
}

function cameraCanRun() {
  return Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
}

function refreshCameraState() {
  const canRun = cameraCanRun();
  startCamera.disabled = state.busy || !canRun;
  captureFrame.disabled = state.busy || !canRun || !state.cameraReady;

  if (!canRun) {
    cameraEmpty.hidden = false;
    cameraEmpty.textContent = "HTTPS required for live camera";
    cameraHelp.textContent = "Browsers block camera access on plain HTTP. Use HTTPS or localhost for live capture.";
    return;
  }

  cameraHelp.textContent = state.cameraReady
    ? "Camera is active. Capture a frame to generate a thermal overlay."
    : "Start the camera and capture one frame for review.";
  cameraEmpty.hidden = state.cameraReady;
  if (!state.cameraReady) {
    cameraEmpty.textContent = "Camera ready";
  }
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("show"), 3400);
}

function setBusy(isBusy, label = "Analyzing") {
  state.busy = isBusy;
  document.querySelectorAll("button").forEach((button) => {
    if (!button.classList.contains("mode-tab")) {
      button.disabled = isBusy;
    }
  });
  refreshCameraState();

  if (isBusy) {
    metricStatus.textContent = label;
    viewerTitle.textContent = "Scanning";
    modelChip.textContent = "Thermal map running";
  }
}

async function postForm(formData) {
  setBusy(true);
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.message || "Analysis failed");
    }
    renderResult(data);
  } catch (error) {
    renderError(error.message);
  } finally {
    setBusy(false);
  }
}

async function postLiveFrame(frame) {
  setBusy(true, "Live frame");
  try {
    const response = await fetch("/api/live-frame", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frame }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.message || "Live analysis failed");
    }
    renderResult(data);
  } catch (error) {
    renderError(error.message);
  } finally {
    setBusy(false);
  }
}

function renderResult(data) {
  const ok = data.status === "ok";
  const findings = data.findings || [];

  metricStatus.textContent = ok ? "Complete" : data.status || "Review";
  metricRegions.textContent = findings.length ? "Highlighted" : "Clear";
  metricLatency.textContent = findings.length ? "Clinician review" : "No urgent marker";
  modelChip.textContent = data.model_name || "Model complete";
  viewerTitle.textContent = ok ? "Thermal overlay ready" : "Review needed";

  if (data.overlay_url) {
    resultImage.src = `${data.overlay_url}?t=${Date.now()}`;
    resultImage.hidden = false;
  }

  if (!findings.length) {
    resultFeed.innerHTML = findingCard("No highlighted tumor region", data.message || "No result message returned.", true);
  } else {
    resultFeed.innerHTML = findings
      .map((finding) => {
        const detail = finding.area_ratio > 0.035
          ? "A larger thermal focus was highlighted for careful review."
          : "A focused warm region was highlighted on the scan.";
        return findingCard(finding.label, detail, false);
      })
      .join("");
  }

  showToast(data.message || "Analysis complete");
}

function renderError(message) {
  metricStatus.textContent = "Error";
  viewerTitle.textContent = "Analysis stopped";
  modelChip.textContent = "Check input";
  resultFeed.innerHTML = findingCard("Analysis failed", message, true);
  showToast(message);
}

function findingCard(title, detail, empty) {
  return `
    <article class="finding-card ${empty ? "empty" : ""}">
      <span></span>
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(detail)}</p>
      </div>
    </article>
  `;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => {
    const entities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    };
    return entities[char];
  });
}

function drawMriPreview(time) {
  const width = mriCanvas.width;
  const height = mriCanvas.height;
  const cx = width / 2;
  const cy = height / 2 + 10;

  ctx.clearRect(0, 0, width, height);

  const bg = ctx.createLinearGradient(0, 0, width, height);
  bg.addColorStop(0, "#ffffff");
  bg.addColorStop(0.58, "#eef8f7");
  bg.addColorStop(1, "#e6eeee");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, width, height);

  ctx.save();
  ctx.translate(cx, cy);

  const pulse = Math.sin(time / 650) * 0.5 + 0.5;

  for (let i = 0; i < 18; i += 1) {
    const offset = (i - 8.5) * 24;
    ctx.beginPath();
    ctx.ellipse(
      offset,
      Math.sin(time / 900 + i) * 8,
      22 + (i % 4) * 6,
      220 - Math.abs(i - 9) * 11,
      0.1 * Math.sin(i),
      0,
      Math.PI * 2,
    );
    ctx.strokeStyle = i % 4 === 0 ? "rgba(15, 118, 110, 0.24)" : "rgba(71, 85, 105, 0.16)";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  const brain = ctx.createRadialGradient(-28, -36, 28, 0, 0, 300);
  brain.addColorStop(0, "#ffffff");
  brain.addColorStop(0.46, "#d9e7eb");
  brain.addColorStop(0.84, "#9fb3bc");
  brain.addColorStop(1, "#748894");
  ctx.fillStyle = brain;
  ctx.beginPath();
  ctx.moveTo(0, -270);
  ctx.bezierCurveTo(172, -260, 260, -128, 242, 36);
  ctx.bezierCurveTo(224, 190, 112, 268, 0, 258);
  ctx.bezierCurveTo(-112, 268, -224, 190, -242, 36);
  ctx.bezierCurveTo(-260, -128, -172, -260, 0, -270);
  ctx.fill();

  const heat = ctx.createRadialGradient(88, -44, 8, 88, -44, 86 + pulse * 14);
  heat.addColorStop(0, "rgba(255, 245, 220, 0.94)");
  heat.addColorStop(0.24, "rgba(249, 115, 22, 0.76)");
  heat.addColorStop(0.58, "rgba(220, 38, 38, 0.44)");
  heat.addColorStop(1, "rgba(249, 115, 22, 0)");
  ctx.fillStyle = heat;
  ctx.beginPath();
  ctx.ellipse(88, -44, 94 + pulse * 10, 68 + pulse * 8, 0.4, 0, Math.PI * 2);
  ctx.fill();

  ctx.beginPath();
  ctx.ellipse(88, -44, 54 + pulse * 7, 38 + pulse * 5, 0.4, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(249, 115, 22, 0.86)";
  ctx.lineWidth = 4;
  ctx.stroke();

  ctx.restore();

  ctx.beginPath();
  ctx.arc(cx, cy, 302 + pulse * 8, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(15, 118, 110, 0.32)";
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "rgba(15, 23, 42, 0.68)";
  ctx.font = "700 18px Inter, system-ui, sans-serif";
  ctx.fillText("MRI THERMAL MAP", 34, 48);
  ctx.fillStyle = "rgba(15, 118, 110, 0.85)";
  ctx.fillText("review ready", 34, 78);

  state.animationFrame = requestAnimationFrame(drawMriPreview);
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => setMode(tab.dataset.mode));
});

singleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const formData = new FormData(singleForm);
  if (!formData.get("scan") || !formData.get("scan").name) {
    showToast("Choose an MRI image or video first.");
    return;
  }
  postForm(formData);
});

volumeForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const formData = new FormData(volumeForm);
  const missing = ["t1c", "t1", "t2", "flair"].filter((name) => !formData.get(name) || !formData.get(name).name);
  if (missing.length) {
    showToast(`Missing ${missing.join(", ")} volume.`);
    return;
  }
  postForm(formData);
});

scanInput.addEventListener("change", () => {
  const file = scanInput.files[0];
  singleFileName.textContent = file ? file.name : "Choose MRI image or video";
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
  if (file) {
    scanInput.files = event.dataTransfer.files;
    singleFileName.textContent = file.name;
  }
});

startCamera.addEventListener("click", async () => {
  if (!cameraCanRun()) {
    showToast("Live camera needs HTTPS or localhost.");
    refreshCameraState();
    return;
  }

  try {
    if (state.cameraStream) {
      state.cameraStream.getTracks().forEach((track) => track.stop());
    }
    state.cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
    cameraFeed.srcObject = state.cameraStream;
    await cameraFeed.play();
    state.cameraReady = true;
    refreshCameraState();
    showToast("Camera started");
  } catch (error) {
    state.cameraReady = false;
    refreshCameraState();
    showToast(`Camera unavailable: ${error.message}`);
  }
});

captureFrame.addEventListener("click", () => {
  if (!state.cameraReady || !state.cameraStream) {
    showToast("Start the camera first.");
    return;
  }
  const width = cameraFeed.videoWidth || 960;
  const height = cameraFeed.videoHeight || 540;
  if (!width || !height) {
    showToast("Camera is still starting.");
    return;
  }
  captureCanvas.width = width;
  captureCanvas.height = height;
  captureCanvas.getContext("2d").drawImage(cameraFeed, 0, 0, width, height);
  postLiveFrame(captureCanvas.toDataURL("image/jpeg", 0.9));
});

refreshCameraState();
requestAnimationFrame(drawMriPreview);
