const state = {
  busy: false,
  cameraReady: false,
  cameraStream: null,
  sourceObjectUrl: null,
  animationFrame: null,
};

const tabs = document.querySelectorAll(".mode-tab");
const panes = document.querySelectorAll(".mode-pane");
const singleForm = document.getElementById("singleForm");
const volumeForm = document.getElementById("volumeForm");
const scanInput = document.getElementById("scanInput");
const dropzone = document.getElementById("dropzone");
const singleFileName = document.getElementById("singleFileName");
const sourceImage = document.getElementById("sourceImage");
const sourceEmpty = document.getElementById("sourceEmpty");
const sourceStatus = document.getElementById("sourceStatus");
const resultImage = document.getElementById("resultImage");
const resultStatus = document.getElementById("resultStatus");
const resultNarrative = document.getElementById("resultNarrative");
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
    showToast("Live camera needs HTTPS or localhost. Upload MRI scans normally from the MRI image tab.");
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
    ? "Camera is active. Capture a frame to generate a thermal review."
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
  showToast.timeout = window.setTimeout(() => toast.classList.remove("show"), 3600);
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
    metricRegions.textContent = "Reviewing";
    metricLatency.textContent = "Processing";
    viewerTitle.textContent = "Scanning study";
    resultStatus.textContent = "Generating map";
    modelChip.textContent = "Thermal scan running";
    resultNarrative.textContent = "The scan is being reviewed and the thermal tumor map is being prepared.";
  }
}

function resetResultPreview() {
  resultImage.hidden = true;
  resultImage.removeAttribute("src");
  mriCanvas.hidden = false;
  resultStatus.textContent = "Pending review";
  viewerTitle.textContent = "Waiting for scan";
  modelChip.textContent = "Scanner ready";
  metricStatus.textContent = "Ready";
  metricRegions.textContent = "Overlay waiting";
  metricLatency.textContent = "Review pending";
  resultNarrative.textContent = "Upload a scan to generate a side-by-side thermal review.";
  resultFeed.innerHTML = findingCard("No scan loaded", "Possible tumor locations will be listed here after analysis.", true);
}

function showSourceImage(url, statusText) {
  sourceImage.src = url;
  sourceImage.hidden = false;
  sourceEmpty.hidden = true;
  sourceStatus.textContent = statusText;
}

function showSourcePlaceholder(title, statusText) {
  sourceImage.hidden = true;
  sourceImage.removeAttribute("src");
  sourceEmpty.hidden = false;
  sourceEmpty.innerHTML = `<span></span><strong>${escapeHtml(title)}</strong>`;
  sourceStatus.textContent = statusText;
}

function setSourceFromFile(file) {
  if (state.sourceObjectUrl) {
    URL.revokeObjectURL(state.sourceObjectUrl);
    state.sourceObjectUrl = null;
  }

  if (!file) {
    showSourcePlaceholder("Original image appears here", "No scan loaded");
    return;
  }

  if (file.type.startsWith("image/")) {
    state.sourceObjectUrl = URL.createObjectURL(file);
    showSourceImage(state.sourceObjectUrl, "Image loaded");
    return;
  }

  if (file.type.startsWith("video/")) {
    showSourcePlaceholder("Video study selected", "Frames will be sampled");
    return;
  }

  showSourcePlaceholder("Study selected", "Volume study loaded");
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

  metricStatus.textContent = ok ? "Complete" : "Review needed";
  metricRegions.textContent = findings.length ? "Thermal focus marked" : "No focus marked";
  metricLatency.textContent = findings.length ? "Doctor review advised" : "Continue review";
  modelChip.textContent = ok ? "Thermal review ready" : "Review needed";
  viewerTitle.textContent = ok ? "Thermal map ready" : "Review needed";
  resultStatus.textContent = ok ? "Thermal image ready" : "Check result";

  if (data.overlay_url) {
    resultImage.src = `${data.overlay_url}?t=${Date.now()}`;
    resultImage.hidden = false;
    mriCanvas.hidden = true;
  } else if (ok) {
    renderError("The scan finished, but the thermal image was not returned.");
    return;
  }

  if (!findings.length) {
    resultNarrative.textContent = "No clear tumor focus was highlighted. Continue clinical review if symptoms or scan history require it.";
    resultFeed.innerHTML = findingCard("No highlighted tumor region", data.message || "No result message returned.", true);
  } else {
    const firstFinding = findings[0].label || "possible tumor region";
    resultNarrative.textContent = `${capitalizeSentence(firstFinding)}. The thermal image marks the review area side by side with the original scan.`;
    resultFeed.innerHTML = findings
      .map((finding) => {
        const detail = finding.area_ratio > 0.035
          ? "A broader warm focus is marked on the thermal map for careful review."
          : "A focused warm region is marked on the thermal map.";
        return findingCard(finding.label, detail, false);
      })
      .join("");
  }

  showToast(data.message || "Thermal review complete");
}

function renderError(message) {
  metricStatus.textContent = "Error";
  metricRegions.textContent = "Not available";
  metricLatency.textContent = "Try again";
  viewerTitle.textContent = "Analysis stopped";
  modelChip.textContent = "Check input";
  resultStatus.textContent = "Image unavailable";
  resultNarrative.textContent = message;
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

function capitalizeSentence(value) {
  const text = String(value || "").trim();
  if (!text) {
    return "";
  }
  return text.charAt(0).toUpperCase() + text.slice(1);
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
  bg.addColorStop(0.58, "#f0f8f2");
  bg.addColorStop(1, "#e5eee8");
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
    ctx.strokeStyle = i % 4 === 0 ? "rgba(46, 118, 84, 0.24)" : "rgba(71, 85, 105, 0.15)";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  const brain = ctx.createRadialGradient(-28, -36, 28, 0, 0, 300);
  brain.addColorStop(0, "#ffffff");
  brain.addColorStop(0.46, "#dae8df");
  brain.addColorStop(0.84, "#a2b5aa");
  brain.addColorStop(1, "#778b80");
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
  heat.addColorStop(0.24, "rgba(240, 106, 42, 0.76)");
  heat.addColorStop(0.58, "rgba(198, 51, 34, 0.44)");
  heat.addColorStop(1, "rgba(240, 106, 42, 0)");
  ctx.fillStyle = heat;
  ctx.beginPath();
  ctx.ellipse(88, -44, 94 + pulse * 10, 68 + pulse * 8, 0.4, 0, Math.PI * 2);
  ctx.fill();

  ctx.beginPath();
  ctx.ellipse(88, -44, 54 + pulse * 7, 38 + pulse * 5, 0.4, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(240, 106, 42, 0.86)";
  ctx.lineWidth = 4;
  ctx.stroke();

  ctx.restore();

  ctx.fillStyle = "rgba(16, 32, 25, 0.68)";
  ctx.font = "700 18px Inter, system-ui, sans-serif";
  ctx.fillText("THERMAL REVIEW MAP", 34, 48);
  ctx.fillStyle = "rgba(46, 118, 84, 0.85)";
  ctx.fillText("side-by-side ready", 34, 78);

  state.animationFrame = requestAnimationFrame(drawMriPreview);
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => setMode(tab.dataset.mode));
});

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
  const missing = ["t1c", "t1", "t2", "flair"].filter((name) => !formData.get(name) || !formData.get(name).name);
  if (missing.length) {
    showToast(`Missing ${missing.join(", ")} volume.`);
    return;
  }
  showSourcePlaceholder("Volume MRI study selected", "Volume study loaded");
  postForm(formData);
});

scanInput.addEventListener("change", () => {
  const file = scanInput.files[0];
  singleFileName.textContent = file ? file.name : "Choose MRI image or video";
  setSourceFromFile(file);
  resetResultPreview();
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
    setSourceFromFile(file);
    resetResultPreview();
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
  const frame = captureCanvas.toDataURL("image/jpeg", 0.9);
  showSourceImage(frame, "Camera frame loaded");
  resetResultPreview();
  postLiveFrame(frame);
});

resultImage.addEventListener("error", () => {
  renderError("The thermal result image could not be loaded. Please scan again.");
});

refreshCameraState();
requestAnimationFrame(drawMriPreview);
