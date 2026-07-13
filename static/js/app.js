const state = {
  cameraStream: null,
  animationTime: 0,
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
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("show"), 3400);
}

function setBusy(isBusy, label = "Analyzing") {
  document.querySelectorAll("button").forEach((button) => {
    if (!button.classList.contains("mode-tab")) {
      button.disabled = isBusy;
    }
  });
  if (isBusy) {
    metricStatus.textContent = label;
    viewerTitle.textContent = "Scanning...";
    modelChip.textContent = "Inference running";
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
  metricStatus.textContent = ok ? "Complete" : data.status || "Review";
  metricRegions.textContent = String((data.findings || []).length);
  metricLatency.textContent = `${data.inference_ms || 0} ms`;
  modelChip.textContent = data.model_name || "Model complete";
  viewerTitle.textContent = ok ? "Overlay generated" : "Review needed";

  if (data.overlay_url) {
    resultImage.src = `${data.overlay_url}?t=${Date.now()}`;
    resultImage.hidden = false;
  }

  const findings = data.findings || [];
  if (!findings.length) {
    resultFeed.innerHTML = findingCard("No highlighted region", data.message || "No result message returned.", true);
  } else {
    resultFeed.innerHTML = findings
      .map((finding, index) => {
        const confidence = Math.round((finding.confidence || 0) * 100);
        const detail = `Confidence ${confidence}% | x ${finding.x}, y ${finding.y}, ${finding.width} x ${finding.height}`;
        return findingCard(`${index + 1}. ${finding.label}`, detail, false);
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
  const cy = height / 2;
  ctx.clearRect(0, 0, width, height);

  const bg = ctx.createLinearGradient(0, 0, width, height);
  bg.addColorStop(0, "#ffffff");
  bg.addColorStop(0.54, "#eefaff");
  bg.addColorStop(1, "#e9eefb");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, width, height);

  ctx.save();
  ctx.translate(cx, cy + 6);
  ctx.rotate(Math.sin(time / 1800) * 0.015);

  for (let i = 0; i < 34; i += 1) {
    const alpha = 0.08 + i * 0.012;
    const rx = 190 + i * 3.6 + Math.sin(time / 900 + i) * 3;
    const ry = 252 + i * 2.8 + Math.cos(time / 1000 + i) * 2;
    ctx.beginPath();
    ctx.ellipse(0, 0, rx, ry, 0, 0, Math.PI * 2);
    ctx.strokeStyle = i % 3 === 0
      ? `rgba(19, 184, 166, ${Math.min(alpha, 0.3)})`
      : `rgba(31, 62, 102, ${Math.min(alpha, 0.3)})`;
    ctx.lineWidth = 1.1;
    ctx.stroke();
  }

  const brain = ctx.createRadialGradient(-30, -24, 30, 0, 0, 315);
  brain.addColorStop(0, "#ffffff");
  brain.addColorStop(0.45, "#d7e4ef");
  brain.addColorStop(0.78, "#9fb2c7");
  brain.addColorStop(1, "#6f8197");
  ctx.fillStyle = brain;
  ctx.beginPath();
  ctx.moveTo(0, -278);
  ctx.bezierCurveTo(178, -266, 276, -134, 256, 34);
  ctx.bezierCurveTo(238, 198, 120, 280, 0, 270);
  ctx.bezierCurveTo(-120, 280, -238, 198, -256, 34);
  ctx.bezierCurveTo(-276, -134, -178, -266, 0, -278);
  ctx.fill();

  ctx.globalCompositeOperation = "multiply";
  for (let i = 0; i < 17; i += 1) {
    const offset = (i - 8) * 23;
    ctx.beginPath();
    ctx.ellipse(offset, Math.sin(i) * 12, 26 + (i % 4) * 8, 228 - Math.abs(i - 8) * 12, 0.13 * Math.sin(i), 0, Math.PI * 2);
    ctx.strokeStyle = i % 4 === 0 ? "rgba(37, 99, 235, 0.17)" : "rgba(72, 92, 116, 0.16)";
    ctx.lineWidth = 3;
    ctx.stroke();
  }
  ctx.globalCompositeOperation = "source-over";

  const glow = 0.55 + Math.sin(time / 520) * 0.22;
  ctx.beginPath();
  ctx.ellipse(92, -46, 52 + glow * 8, 36 + glow * 6, 0.52, 0, Math.PI * 2);
  ctx.fillStyle = `rgba(239, 71, 111, ${0.38 + glow * 0.22})`;
  ctx.fill();
  ctx.beginPath();
  ctx.ellipse(92, -46, 88 + glow * 10, 62 + glow * 8, 0.52, 0, Math.PI * 2);
  ctx.strokeStyle = `rgba(245, 165, 36, ${0.45 + glow * 0.18})`;
  ctx.lineWidth = 4;
  ctx.stroke();

  ctx.restore();

  ctx.beginPath();
  ctx.arc(cx, cy, 292 + Math.sin(time / 760) * 10, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(19, 184, 166, 0.38)";
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(cx, cy, 330 + Math.cos(time / 980) * 8, -0.8, 1.65);
  ctx.strokeStyle = "rgba(124, 58, 237, 0.24)";
  ctx.lineWidth = 5;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(cx, cy, 348 + Math.sin(time / 1100) * 7, 2.15, 4.2);
  ctx.strokeStyle = "rgba(245, 165, 36, 0.28)";
  ctx.lineWidth = 5;
  ctx.stroke();

  ctx.fillStyle = "rgba(16, 24, 40, 0.68)";
  ctx.font = "700 18px Inter, sans-serif";
  ctx.fillText("NEURAL SCAN READY", 34, 48);
  ctx.fillStyle = "rgba(37, 99, 235, 0.8)";
  ctx.fillText(`slice ${(Math.floor(time / 80) % 144).toString().padStart(3, "0")}`, 34, 78);

  state.animationTime = requestAnimationFrame(drawMriPreview);
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
  singleFileName.textContent = file ? file.name : "Drop MRI image or video";
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
  try {
    state.cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
    cameraFeed.srcObject = state.cameraStream;
    cameraEmpty.hidden = true;
    showToast("Camera started");
  } catch (error) {
    showToast(`Camera unavailable: ${error.message}`);
  }
});

captureFrame.addEventListener("click", () => {
  if (!state.cameraStream) {
    showToast("Start the camera first.");
    return;
  }
  const width = cameraFeed.videoWidth || 960;
  const height = cameraFeed.videoHeight || 540;
  captureCanvas.width = width;
  captureCanvas.height = height;
  captureCanvas.getContext("2d").drawImage(cameraFeed, 0, 0, width, height);
  postLiveFrame(captureCanvas.toDataURL("image/jpeg", 0.9));
});

requestAnimationFrame(drawMriPreview);
