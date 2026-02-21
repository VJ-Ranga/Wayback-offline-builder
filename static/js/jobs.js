(function () {
  const wob = window.WOB || {};
  const addLog = wob.addLog || function () {};
  const readApiResult = wob.readApiResult || (async function (r) { return r.json(); });
  const detailFromProgress = wob.detailFromProgress || function () { return ""; };
  const applyScanPreset = wob.applyScanPreset || function () {};
  const context = wob.context || {};

  let inspectJobId = "";
  let inspectTimer = null;
  const inspectPauseBtn = document.getElementById("inspect-pause-btn");
  const inspectResumeBtn = document.getElementById("inspect-resume-btn");
  const inspectStopBtn = document.getElementById("inspect-stop-btn");

  function setInspectButtons(state) {
    const active = !!inspectJobId;
    if (!inspectPauseBtn || !inspectResumeBtn || !inspectStopBtn) return;
    inspectPauseBtn.disabled = !active || state === "paused" || state === "done" || state === "error";
    inspectResumeBtn.disabled = !active || state !== "paused";
    inspectStopBtn.disabled = !active || state === "done" || state === "error" || state === "stopping";
  }

  const inspectForm = document.getElementById("inspect-form");
  if (inspectForm) {
    inspectForm.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      const form = ev.currentTarget;
      const advancedModeToggle = document.getElementById("advanced-mode-toggle");
      if (advancedModeToggle && advancedModeToggle.checked) applyScanPreset();
      const url = (document.getElementById("target-url") || {}).value || "";
      const live = document.getElementById("inspect-live");
      const bar = document.getElementById("inspect-live-bar");
      const pct = document.getElementById("inspect-live-percent");
      const text = document.getElementById("inspect-live-text");
      const detail = document.getElementById("inspect-live-detail");
      const status = document.getElementById("status-indicator");

      addLog(`Starting inspection for: ${url}`, "info");
      if (live) live.style.display = "block";
      if (status) {
        status.className = "status-pill running";
        status.textContent = "Running";
      }

      try {
        const startRes = await fetch("/inspect/start", { method: "POST", body: new FormData(form) });
        const start = await readApiResult(startRes, "Could not start inspect job");
        inspectJobId = start.job_id;
        if (start.cached) addLog("Inspect loaded from local cache", "success");
        addLog(`Inspect job started: ${inspectJobId}`, "info");
        setInspectButtons("running");

        let lastInspectLine = "";
        inspectTimer = setInterval(async function () {
          try {
            const res = await fetch("/inspect/status/" + inspectJobId);
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || "Status error");
            const progress = data.progress || {};
            const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
            if (bar) bar.style.width = percent + "%";
            if (pct) pct.textContent = percent + "%";
            if (text) text.textContent = progress.message || "Checking snapshots...";
            const cachePart = progress.cache_source ? `cache ${progress.cache_source} (${progress.cache_age_seconds || 0}s)` : "";
            if (detail) detail.textContent = detailFromProgress(progress, [`captures ${progress.total_captures || 0}`, cachePart]);
            setInspectButtons(data.state || "running");

            const inspectLine = `inspect ${percent}% | variants ${progress.variants_done || 0}/${progress.variants_total || 0} | captures ${progress.total_captures || 0}`;
            if (inspectLine !== lastInspectLine) {
              addLog(inspectLine, "info");
              lastInspectLine = inspectLine;
            }

            if (data.state === "done") {
              clearInterval(inspectTimer);
              inspectTimer = null;
              addLog("Inspect completed", "success");
              if (status) {
                status.className = "status-pill success";
                status.textContent = "Done";
              }
              setInspectButtons("done");
              window.location.href = "/inspect/result/" + inspectJobId;
            }
            if (data.state === "error") {
              clearInterval(inspectTimer);
              inspectTimer = null;
              throw new Error(data.error || "Inspect failed");
            }
          } catch (e) {
            clearInterval(inspectTimer);
            inspectTimer = null;
            addLog("Inspect error: " + e.message, "error");
            if (status) {
              status.className = "status-pill error";
              status.textContent = "Error";
            }
            if (detail) detail.textContent = e.message;
            setInspectButtons("error");
          }
        }, 900);
      } catch (e) {
        addLog("Inspect start failed: " + e.message, "error");
      }
    });
  }

  if (inspectPauseBtn) inspectPauseBtn.addEventListener("click", async function () { if (!inspectJobId) return; const d = await (await fetch("/inspect/pause/" + inspectJobId, { method: "POST" })).json(); if (d.ok) { addLog("Inspect paused", "warning"); setInspectButtons("paused"); } });
  if (inspectResumeBtn) inspectResumeBtn.addEventListener("click", async function () { if (!inspectJobId) return; const d = await (await fetch("/inspect/resume/" + inspectJobId, { method: "POST" })).json(); if (d.ok) { addLog("Inspect resumed", "info"); setInspectButtons("running"); } });
  if (inspectStopBtn) inspectStopBtn.addEventListener("click", async function () { if (!inspectJobId) return; const d = await (await fetch("/inspect/stop/" + inspectJobId, { method: "POST" })).json(); if (d.ok) { addLog("Inspect stopping requested", "warning"); setInspectButtons("stopping"); } });

  // Non-async forms overlay behavior
  document.querySelectorAll("form").forEach(function (form) {
    if (["inspect-form", "analyze-form", "analyze-batch-form", "sitemap-form", "check-form", "download-form-simple", "missing-form"].includes(form.id)) return;
    form.addEventListener("submit", function () {
      const action = form.getAttribute("action") || "";
      const target = (form.querySelector('[name="target_url"]') || {}).value || "";
      const snap = (form.querySelector('[name="selected_snapshot"]') || {}).value || "";
      const limit = (form.querySelector('[name="max_files"]') || form.querySelector('[name="missing_limit"]') || {}).value || "";
      if (!wob.showStepOverlay) return;
      if (action.includes("/analyze")) {
        addLog(`Analyze start | snapshot: ${snap || "latest"} | url: ${target}`, "warning");
        wob.showStepOverlay("Analyzing Snapshot", "Collecting structure, file inventory, and platform signals...", ["Read snapshot metadata", "Scan URLs and folders", "Detect CMS and signals", "Build analysis report"]);
      } else if (action.includes("/download-missing")) {
        addLog(`Missing download start | limit: ${limit || "-"} | snapshot: ${snap || "latest"}`, "warning");
        wob.showStepOverlay("Downloading Missing Files", "Finding missing assets and trying nearby snapshots...", ["Load manifest", "Find missing URLs", "Fetch missing files", "Update manifest"]);
      } else if (action.includes("/download")) {
        addLog(`Download start | max files: ${limit || "-"} | snapshot: ${snap || "latest"}`, "warning");
        wob.showStepOverlay("Building Offline Copy", "Downloading pages/assets and fixing links for offline use...", ["Queue pages and assets", "Download archive files", "Repair missing from older snaps", "Rewrite links and save report"]);
      } else if (action.includes("/check")) {
        addLog("Check start | comparing downloaded files vs expected", "warning");
        wob.showStepOverlay("Checking Have vs Missing", "Comparing output with archive inventory...", ["Load manifest", "Build expected inventory", "Compare have/missing", "Prepare report"]);
      }
    });
  });

  // Keep rest of job handlers from previous inline script
  const analyzeForm = document.getElementById("analyze-form");
  const analyzeLive = document.getElementById("analyze-live");
  const analyzeLiveBar = document.getElementById("analyze-live-bar");
  const analyzeLivePct = document.getElementById("analyze-live-percent");
  const analyzeLiveText = document.getElementById("analyze-live-text");
  const analyzeLiveDetail = document.getElementById("analyze-live-detail");
  const analyzePauseBtn = document.getElementById("analyze-pause-btn");
  const analyzeResumeBtn = document.getElementById("analyze-resume-btn");
  const analyzeStopBtn = document.getElementById("analyze-stop-btn");
  const snapSelect = document.getElementById("selected-snapshot");
  let analyzeJobId = "";
  let analyzeTimer = null;
  function setAnalyzeButtons(state) { const active = !!analyzeJobId; if (!analyzePauseBtn || !analyzeResumeBtn || !analyzeStopBtn) return; analyzePauseBtn.disabled = !active || state === "paused" || state === "done" || state === "error"; analyzeResumeBtn.disabled = !active || state !== "paused"; analyzeStopBtn.disabled = !active || state === "done" || state === "error" || state === "stopping"; }
  if (analyzeForm) analyzeForm.addEventListener("submit", async function (ev) { ev.preventDefault(); const ts = (snapSelect && snapSelect.value) || ""; addLog(`Analyzing snapshot: ${ts}`, "warning"); if (analyzeLive) analyzeLive.style.display = "block"; const d = await readApiResult(await fetch("/analyze/start", { method: "POST", body: new FormData(analyzeForm) }), "Analyze start failed"); analyzeJobId = d.job_id; setAnalyzeButtons("running"); analyzeTimer = setInterval(async function () { try { const s = await (await fetch("/analyze/status/" + analyzeJobId)).json(); if (!s.ok) throw new Error(s.error || "status failed"); const p = s.progress || {}; const pct = Math.max(0, Math.min(100, Number(p.percent || 0))); if (analyzeLiveBar) analyzeLiveBar.style.width = pct + "%"; if (analyzeLivePct) analyzeLivePct.textContent = pct + "%"; if (analyzeLiveText) analyzeLiveText.textContent = p.message || "Analyzing..."; if (analyzeLiveDetail) analyzeLiveDetail.textContent = detailFromProgress(p, [`${p.processed || 0}/${p.total || 0}`]); setAnalyzeButtons(s.state || "running"); if (s.state === "done") { clearInterval(analyzeTimer); analyzeTimer = null; addLog("Analyze finished", "success"); window.location.href = "/analyze/result/" + analyzeJobId; } if (s.state === "error") { clearInterval(analyzeTimer); analyzeTimer = null; addLog("Analyze error: " + (s.error || "unknown"), "error"); } } catch (e) { clearInterval(analyzeTimer); analyzeTimer = null; addLog("Analyze status error: " + e.message, "error"); } }, 1200); });
  if (analyzePauseBtn) analyzePauseBtn.addEventListener("click", async function () { if (!analyzeJobId) return; const d = await (await fetch("/analyze/pause/" + analyzeJobId, { method: "POST" })).json(); if (d.ok) { addLog("Analyze paused", "warning"); setAnalyzeButtons("paused"); } });
  if (analyzeResumeBtn) analyzeResumeBtn.addEventListener("click", async function () { if (!analyzeJobId) return; const d = await (await fetch("/analyze/resume/" + analyzeJobId, { method: "POST" })).json(); if (d.ok) { addLog("Analyze resumed", "info"); setAnalyzeButtons("running"); } });
  if (analyzeStopBtn) analyzeStopBtn.addEventListener("click", async function () { if (!analyzeJobId) return; const d = await (await fetch("/analyze/stop/" + analyzeJobId, { method: "POST" })).json(); if (d.ok) { addLog("Analyze stopping requested", "warning"); setAnalyzeButtons("stopping"); } });

  const analyzeBatchForm = document.getElementById("analyze-batch-form");
  const analyzeBatchLive = document.getElementById("analyze-batch-live");
  const analyzeBatchBar = document.getElementById("analyze-batch-bar");
  const analyzeBatchPct = document.getElementById("analyze-batch-percent");
  const analyzeBatchText = document.getElementById("analyze-batch-text");
  const analyzeBatchDetail = document.getElementById("analyze-batch-detail");
  const analyzeBatchPauseBtn = document.getElementById("analyze-batch-pause-btn");
  const analyzeBatchResumeBtn = document.getElementById("analyze-batch-resume-btn");
  const analyzeBatchStopBtn = document.getElementById("analyze-batch-stop-btn");
  let analyzeBatchJobId = "";
  let analyzeBatchTimer = null;
  function setAnalyzeBatchButtons(state) { const active = !!analyzeBatchJobId; if (!analyzeBatchPauseBtn || !analyzeBatchResumeBtn || !analyzeBatchStopBtn) return; analyzeBatchPauseBtn.disabled = !active || state === "paused" || state === "done" || state === "error"; analyzeBatchResumeBtn.disabled = !active || state !== "paused"; analyzeBatchStopBtn.disabled = !active || state === "done" || state === "error" || state === "stopping"; }
  if (analyzeBatchForm) analyzeBatchForm.addEventListener("submit", async function (ev) { ev.preventDefault(); if (analyzeBatchLive) analyzeBatchLive.style.display = "block"; addLog("Batch analyze started", "warning"); const d = await readApiResult(await fetch("/analyze-batch/start", { method: "POST", body: new FormData(analyzeBatchForm) }), "Batch analyze start failed"); analyzeBatchJobId = d.job_id; setAnalyzeBatchButtons("running"); analyzeBatchTimer = setInterval(async function () { try { const s = await (await fetch("/analyze-batch/status/" + analyzeBatchJobId)).json(); if (!s.ok) throw new Error(s.error || "status failed"); const p = s.progress || {}; const pct = Math.max(0, Math.min(100, Number(p.percent || 0))); if (analyzeBatchBar) analyzeBatchBar.style.width = pct + "%"; if (analyzeBatchPct) analyzeBatchPct.textContent = pct + "%"; if (analyzeBatchText) analyzeBatchText.textContent = p.message || "Batch analyzing..."; if (analyzeBatchDetail) analyzeBatchDetail.textContent = detailFromProgress(p, [`${p.done || 0}/${p.total || 0}`, p.last_site_type || ""]); setAnalyzeBatchButtons(s.state || "running"); if (s.state === "done") { clearInterval(analyzeBatchTimer); analyzeBatchTimer = null; addLog("Batch analyze completed", "success"); const snapshots = (((s.result || {}).snapshots) || []); const last = snapshots.length ? snapshots[snapshots.length - 1] : null; const lastSnapshot = (last && last.snapshot) || ""; const analyzeFormEl = document.getElementById("analyze-form"); const snapSelectEl = document.getElementById("selected-snapshot"); if (lastSnapshot && snapSelectEl) { const hasOpt = Array.from(snapSelectEl.options || []).some(function (opt) { return opt.value === lastSnapshot; }); if (hasOpt) snapSelectEl.value = lastSnapshot; } if (analyzeFormEl) { addLog("Opening analysis details from saved batch result...", "info"); analyzeFormEl.requestSubmit(); } } if (s.state === "error") { clearInterval(analyzeBatchTimer); analyzeBatchTimer = null; addLog("Batch analyze error: " + (s.error || "unknown"), "error"); } } catch (e) { clearInterval(analyzeBatchTimer); analyzeBatchTimer = null; addLog("Batch analyze status error: " + e.message, "error"); } }, 1200); });
  if (analyzeBatchPauseBtn) analyzeBatchPauseBtn.addEventListener("click", async function () { if (!analyzeBatchJobId) return; const d = await (await fetch("/analyze-batch/pause/" + analyzeBatchJobId, { method: "POST" })).json(); if (d.ok) { addLog("Batch analyze paused", "warning"); setAnalyzeBatchButtons("paused"); } });
  if (analyzeBatchResumeBtn) analyzeBatchResumeBtn.addEventListener("click", async function () { if (!analyzeBatchJobId) return; const d = await (await fetch("/analyze-batch/resume/" + analyzeBatchJobId, { method: "POST" })).json(); if (d.ok) { addLog("Batch analyze resumed", "info"); setAnalyzeBatchButtons("running"); } });
  if (analyzeBatchStopBtn) analyzeBatchStopBtn.addEventListener("click", async function () { if (!analyzeBatchJobId) return; const d = await (await fetch("/analyze-batch/stop/" + analyzeBatchJobId, { method: "POST" })).json(); if (d.ok) { addLog("Batch analyze stopping requested", "warning"); setAnalyzeBatchButtons("stopping"); } });

  const sitemapForm = document.getElementById("sitemap-form");
  const sitemapLive = document.getElementById("sitemap-live");
  const sitemapLiveBar = document.getElementById("sitemap-live-bar");
  const sitemapLivePct = document.getElementById("sitemap-live-percent");
  const sitemapLiveText = document.getElementById("sitemap-live-text");
  const sitemapLiveDetail = document.getElementById("sitemap-live-detail");
  const sitemapPauseBtn = document.getElementById("sitemap-pause-btn");
  const sitemapResumeBtn = document.getElementById("sitemap-resume-btn");
  const sitemapStopBtn = document.getElementById("sitemap-stop-btn");
  let sitemapJobId = "";
  let sitemapTimer = null;
  function setSitemapButtons(state) { const active = !!sitemapJobId; if (!sitemapPauseBtn || !sitemapResumeBtn || !sitemapStopBtn) return; sitemapPauseBtn.disabled = !active || state === "paused" || state === "done" || state === "error"; sitemapResumeBtn.disabled = !active || state !== "paused"; sitemapStopBtn.disabled = !active || state === "done" || state === "error" || state === "stopping"; }
  if (sitemapForm) sitemapForm.addEventListener("submit", async function (ev) { ev.preventDefault(); if (sitemapLive) sitemapLive.style.display = "block"; addLog("Sitemap build started", "warning"); const d = await readApiResult(await fetch("/sitemap/start", { method: "POST", body: new FormData(sitemapForm) }), "Sitemap start failed"); sitemapJobId = d.job_id; setSitemapButtons("running"); sitemapTimer = setInterval(async function () { try { const s = await (await fetch("/sitemap/status/" + sitemapJobId)).json(); if (!s.ok) throw new Error(s.error || "status failed"); const p = s.progress || {}; const pct = Math.max(0, Math.min(100, Number(p.percent || 0))); if (sitemapLiveBar) sitemapLiveBar.style.width = pct + "%"; if (sitemapLivePct) sitemapLivePct.textContent = pct + "%"; if (sitemapLiveText) sitemapLiveText.textContent = p.message || "Building sitemap..."; if (sitemapLiveDetail) sitemapLiveDetail.textContent = detailFromProgress(p, [`${p.processed || 0}/${p.total || 0}`]); setSitemapButtons(s.state || "running"); if (s.state === "done") { clearInterval(sitemapTimer); sitemapTimer = null; addLog("Sitemap build finished", "success"); window.location.href = "/sitemap/result/" + sitemapJobId; } if (s.state === "error") { clearInterval(sitemapTimer); sitemapTimer = null; addLog("Sitemap error: " + (s.error || "unknown"), "error"); } } catch (e) { clearInterval(sitemapTimer); sitemapTimer = null; addLog("Sitemap status error: " + e.message, "error"); } }, 1200); });
  if (sitemapPauseBtn) sitemapPauseBtn.addEventListener("click", async function () { if (!sitemapJobId) return; const d = await (await fetch("/sitemap/pause/" + sitemapJobId, { method: "POST" })).json(); if (d.ok) { addLog("Sitemap paused", "warning"); setSitemapButtons("paused"); } });
  if (sitemapResumeBtn) sitemapResumeBtn.addEventListener("click", async function () { if (!sitemapJobId) return; const d = await (await fetch("/sitemap/resume/" + sitemapJobId, { method: "POST" })).json(); if (d.ok) { addLog("Sitemap resumed", "info"); setSitemapButtons("running"); } });
  if (sitemapStopBtn) sitemapStopBtn.addEventListener("click", async function () { if (!sitemapJobId) return; const d = await (await fetch("/sitemap/stop/" + sitemapJobId, { method: "POST" })).json(); if (d.ok) { addLog("Sitemap stopping requested", "warning"); setSitemapButtons("stopping"); } });

  const checkForm = document.getElementById("check-form");
  const checkLive = document.getElementById("check-live");
  const checkLiveBar = document.getElementById("check-live-bar");
  const checkLivePct = document.getElementById("check-live-percent");
  const checkLiveText = document.getElementById("check-live-text");
  const checkLiveDetail = document.getElementById("check-live-detail");
  const checkPauseBtn = document.getElementById("check-pause-btn");
  const checkResumeBtn = document.getElementById("check-resume-btn");
  const checkStopBtn = document.getElementById("check-stop-btn");
  let checkJobId = "";
  let checkTimer = null;
  function setCheckButtons(state) { const active = !!checkJobId; if (!checkPauseBtn || !checkResumeBtn || !checkStopBtn) return; checkPauseBtn.disabled = !active || state === "paused" || state === "done" || state === "error"; checkResumeBtn.disabled = !active || state !== "paused"; checkStopBtn.disabled = !active || state === "done" || state === "error" || state === "stopping"; }
  if (checkForm) checkForm.addEventListener("submit", async function (ev) { ev.preventDefault(); if (checkLive) checkLive.style.display = "block"; addLog("Check started", "warning"); const d = await readApiResult(await fetch("/check/start", { method: "POST", body: new FormData(checkForm) }), "Check start failed"); checkJobId = d.job_id; setCheckButtons("running"); checkTimer = setInterval(async function () { try { const s = await (await fetch("/check/status/" + checkJobId)).json(); if (!s.ok) throw new Error(s.error || "status failed"); const p = s.progress || {}; const pct = Math.max(0, Math.min(100, Number(p.percent || 0))); if (checkLiveBar) checkLiveBar.style.width = pct + "%"; if (checkLivePct) checkLivePct.textContent = pct + "%"; if (checkLiveText) checkLiveText.textContent = p.message || "Checking..."; if (checkLiveDetail) checkLiveDetail.textContent = detailFromProgress(p, []); setCheckButtons(s.state || "running"); if (s.state === "done") { clearInterval(checkTimer); checkTimer = null; addLog("Check finished", "success"); window.location.href = "/check/result/" + checkJobId; } if (s.state === "error") { clearInterval(checkTimer); checkTimer = null; addLog("Check error: " + (s.error || "unknown"), "error"); } } catch (e) { clearInterval(checkTimer); checkTimer = null; addLog("Check status error: " + e.message, "error"); } }, 1200); });
  if (checkPauseBtn) checkPauseBtn.addEventListener("click", async function () { if (!checkJobId) return; const d = await (await fetch("/check/pause/" + checkJobId, { method: "POST" })).json(); if (d.ok) { addLog("Check paused", "warning"); setCheckButtons("paused"); } });
  if (checkResumeBtn) checkResumeBtn.addEventListener("click", async function () { if (!checkJobId) return; const d = await (await fetch("/check/resume/" + checkJobId, { method: "POST" })).json(); if (d.ok) { addLog("Check resumed", "info"); setCheckButtons("running"); } });
  if (checkStopBtn) checkStopBtn.addEventListener("click", async function () { if (!checkJobId) return; const d = await (await fetch("/check/stop/" + checkJobId, { method: "POST" })).json(); if (d.ok) { addLog("Check stopping requested", "warning"); setCheckButtons("stopping"); } });

  const downloadFormSimple = document.getElementById("download-form-simple");
  const downloadLive = document.getElementById("download-live");
  const downloadLiveBar = document.getElementById("download-live-bar");
  const downloadLivePct = document.getElementById("download-live-percent");
  const downloadLiveText = document.getElementById("download-live-text");
  const downloadLiveDetail = document.getElementById("download-live-detail");
  const downloadPauseBtn = document.getElementById("download-pause-btn");
  const downloadResumeBtn = document.getElementById("download-resume-btn");
  const downloadStopBtn = document.getElementById("download-stop-btn");
  let downloadJobId = "";
  let downloadTimer = null;
  function setDownloadButtons(state) { const active = !!downloadJobId; if (!downloadPauseBtn || !downloadResumeBtn || !downloadStopBtn) return; downloadPauseBtn.disabled = !active || state === "paused" || state === "done" || state === "error"; downloadResumeBtn.disabled = !active || state !== "paused"; downloadStopBtn.disabled = !active || state === "done" || state === "error" || state === "stopping"; }
  if (downloadFormSimple) downloadFormSimple.addEventListener("submit", async function (ev) { ev.preventDefault(); addLog("Starting download from analyzed snapshot", "warning"); if (downloadLive) downloadLive.style.display = "block"; const d = await readApiResult(await fetch("/download/start", { method: "POST", body: new FormData(downloadFormSimple) }), "Download start failed"); downloadJobId = d.job_id; setDownloadButtons("running"); downloadTimer = setInterval(async function () { try { const s = await (await fetch("/download/status/" + downloadJobId)).json(); if (!s.ok) throw new Error(s.error || "status failed"); const p = s.progress || {}; const pct = Math.max(0, Math.min(100, Number(p.percent || 0))); if (downloadLiveBar) downloadLiveBar.style.width = pct + "%"; if (downloadLivePct) downloadLivePct.textContent = pct + "%"; if (downloadLiveText) downloadLiveText.textContent = p.message || "Downloading..."; if (downloadLiveDetail) downloadLiveDetail.textContent = detailFromProgress(p, [`${p.files_downloaded || 0}/${p.max_files || 0} files`, `queue ${p.queue_size || 0}`]); setDownloadButtons(s.state || "running"); if (s.state === "done") { clearInterval(downloadTimer); downloadTimer = null; addLog("Download finished", "success"); window.location.reload(); } if (s.state === "error") { clearInterval(downloadTimer); downloadTimer = null; addLog("Download error: " + (s.error || "unknown"), "error"); } } catch (e) { clearInterval(downloadTimer); downloadTimer = null; addLog("Download status error: " + e.message, "error"); } }, 1200); });
  if (downloadPauseBtn) downloadPauseBtn.addEventListener("click", async function () { if (!downloadJobId) return; const d = await (await fetch("/download/pause/" + downloadJobId, { method: "POST" })).json(); if (d.ok) { addLog("Download paused", "warning"); setDownloadButtons("paused"); } });
  if (downloadResumeBtn) downloadResumeBtn.addEventListener("click", async function () { if (!downloadJobId) return; const d = await (await fetch("/download/resume/" + downloadJobId, { method: "POST" })).json(); if (d.ok) { addLog("Download resumed", "info"); setDownloadButtons("running"); } });
  if (downloadStopBtn) downloadStopBtn.addEventListener("click", async function () { if (!downloadJobId) return; const d = await (await fetch("/download/stop/" + downloadJobId, { method: "POST" })).json(); if (d.ok) { addLog("Download stopping requested", "warning"); setDownloadButtons("stopping"); } });

  const missingForm = document.getElementById("missing-form");
  const missingLive = document.getElementById("missing-live");
  const missingLiveBar = document.getElementById("missing-live-bar");
  const missingLivePct = document.getElementById("missing-live-percent");
  const missingLiveText = document.getElementById("missing-live-text");
  const missingLiveDetail = document.getElementById("missing-live-detail");
  const missingPauseBtn = document.getElementById("missing-pause-btn");
  const missingResumeBtn = document.getElementById("missing-resume-btn");
  const missingStopBtn = document.getElementById("missing-stop-btn");
  let missingJobId = "";
  let missingTimer = null;
  function setMissingButtons(state) { const active = !!missingJobId; if (!missingPauseBtn || !missingResumeBtn || !missingStopBtn) return; missingPauseBtn.disabled = !active || state === "paused" || state === "done" || state === "error"; missingResumeBtn.disabled = !active || state !== "paused"; missingStopBtn.disabled = !active || state === "done" || state === "error" || state === "stopping"; }
  if (missingForm) missingForm.addEventListener("submit", async function (ev) { ev.preventDefault(); if (missingLive) missingLive.style.display = "block"; addLog("Missing downloader started", "warning"); const d = await readApiResult(await fetch("/download-missing/start", { method: "POST", body: new FormData(missingForm) }), "Missing start failed"); missingJobId = d.job_id; setMissingButtons("running"); missingTimer = setInterval(async function () { try { const s = await (await fetch("/download-missing/status/" + missingJobId)).json(); if (!s.ok) throw new Error(s.error || "status failed"); const p = s.progress || {}; const pct = Math.max(0, Math.min(100, Number(p.percent || 0))); if (missingLiveBar) missingLiveBar.style.width = pct + "%"; if (missingLivePct) missingLivePct.textContent = pct + "%"; if (missingLiveText) missingLiveText.textContent = p.message || "Downloading missing..."; if (missingLiveDetail) missingLiveDetail.textContent = detailFromProgress(p, [`${p.added || 0} added`, `${p.failed || 0} failed`]); setMissingButtons(s.state || "running"); if (s.state === "done") { clearInterval(missingTimer); missingTimer = null; addLog("Missing downloader finished", "success"); window.location.reload(); } if (s.state === "error") { clearInterval(missingTimer); missingTimer = null; addLog("Missing downloader error: " + (s.error || "unknown"), "error"); } } catch (e) { clearInterval(missingTimer); missingTimer = null; addLog("Missing status error: " + e.message, "error"); } }, 1200); });
  if (missingPauseBtn) missingPauseBtn.addEventListener("click", async function () { if (!missingJobId) return; const d = await (await fetch("/download-missing/pause/" + missingJobId, { method: "POST" })).json(); if (d.ok) { addLog("Missing paused", "warning"); setMissingButtons("paused"); } });
  if (missingResumeBtn) missingResumeBtn.addEventListener("click", async function () { if (!missingJobId) return; const d = await (await fetch("/download-missing/resume/" + missingJobId, { method: "POST" })).json(); if (d.ok) { addLog("Missing resumed", "info"); setMissingButtons("running"); } });
  if (missingStopBtn) missingStopBtn.addEventListener("click", async function () { if (!missingJobId) return; const d = await (await fetch("/download-missing/stop/" + missingJobId, { method: "POST" })).json(); if (d.ok) { addLog("Missing stopping requested", "warning"); setMissingButtons("stopping"); } });

  // Step markers from server-rendered context
  if (context.hasInspect) { const el = document.querySelector('[data-step="1"]'); if (el) el.classList.add("completed"); addLog(`Found ${context.inspectTotal || 0} snapshots`, "success"); }
  if (context.hasAnalysis) { const el = document.querySelector('[data-step="2"]'); if (el) el.classList.add("completed"); addLog(`Analysis ready for snapshot ${context.analysisSnapshot || ""}`, "success"); }
  if (context.hasSitemap) { const el = document.querySelector('[data-step="3"]'); if (el) el.classList.add("completed"); addLog("Sitemap generated", "success"); }
  if (context.hasCheck) { const el = document.querySelector('[data-step="4"]'); if (el) el.classList.add("completed"); addLog("Missing check completed", "success"); }
  if (context.hasResult) { const el = document.querySelector('[data-step="5"]'); if (el) el.classList.add("completed"); addLog(`Download complete: ${context.resultFiles || 0} files`, "success"); }
})();
