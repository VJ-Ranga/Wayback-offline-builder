(function () {
  const context = window.WOB_CONTEXT || {};
  const CSRF_TOKEN = context.csrfToken || "";

  const _rawFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const opts = init ? { ...init } : {};
    const method = String((opts.method || "GET")).toUpperCase();
    if (method === "POST" || method === "PUT" || method === "PATCH" || method === "DELETE") {
      const headers = new Headers(opts.headers || {});
      if (CSRF_TOKEN) headers.set("X-CSRF-Token", CSRF_TOKEN);
      opts.headers = headers;
    }
    const response = await _rawFetch(input, opts);
    if (response.status === 429) {
      console.warn("Too many active jobs. Wait for running jobs to finish.");
    }
    if (response.status === 403) {
      try {
        const payload = await response.clone().json();
        const message = String((payload || {}).error || "").toLowerCase();
        if (message.includes("csrf")) {
          alert("Security token expired. Reloading page...");
          window.setTimeout(function () {
            window.location.reload();
          }, 700);
        }
      } catch (_e) {
        // ignore non-json 403
      }
    }
    return response;
  };

  function applyScanPreset() {
    const modeEl = document.getElementById("scan-mode");
    const mode = (modeEl && modeEl.value) || "balanced";
    const displayInput = document.getElementById("display-limit-input");
    const cdxInput = document.getElementById("cdx-limit-input");
    if (!displayInput || !cdxInput) return;
    if (mode === "quick") {
      displayInput.value = "10";
      cdxInput.value = "1500";
    } else if (mode === "deep") {
      displayInput.value = "80";
      cdxInput.value = "12000";
    } else {
      displayInput.value = "30";
      cdxInput.value = "5000";
    }
  }

  function setAdvancedMode(enabled) {
    const inspectPanel = document.getElementById("inspect-advanced-panel");
    const analyzePanel = document.getElementById("analyze-advanced-panel");
    const batchPanel = document.getElementById("batch-advanced-panel");
    if (inspectPanel) inspectPanel.style.display = enabled ? "block" : "none";
    if (analyzePanel) analyzePanel.style.display = enabled ? "block" : "none";
    if (batchPanel) batchPanel.style.display = enabled ? "block" : "none";

    if (!enabled) {
      const displayInput = document.getElementById("display-limit-input");
      const cdxInput = document.getElementById("cdx-limit-input");
      const analyzeDepth = document.getElementById("analyze-depth");
      const batchCount = document.getElementById("analyze-count-input");
      const batchCdx = document.getElementById("analyze-cdx-input");
      if (displayInput) displayInput.value = "10";
      if (cdxInput) cdxInput.value = "1500";
      if (analyzeDepth) analyzeDepth.value = "12000";
      if (batchCount) batchCount.value = "100000";
      if (batchCdx) batchCdx.value = "12000";
    } else {
      applyScanPreset();
    }
  }

  const logContent = document.getElementById("log-content");
  const logCount = document.getElementById("log-count");
  let logEntries = 0;

  function addLog(message, type = "info") {
    if (!logContent || !logCount) return;
    const entry = document.createElement("div");
    entry.className = `log-entry ${type}`;
    entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
    logContent.appendChild(entry);
    logContent.scrollTop = logContent.scrollHeight;
    logEntries++;
    logCount.textContent = String(logEntries);
  }

  function clearLog() {
    if (!logContent || !logCount) return;
    logContent.innerHTML = "";
    logEntries = 0;
    logCount.textContent = "0";
  }

  async function readApiResult(response, fallbackMessage) {
    let data = null;
    try {
      data = await response.json();
    } catch (_e) {
      data = { ok: false, error: fallbackMessage || `Request failed (${response.status})` };
    }
    if (!response.ok || !data || data.ok === false) {
      const message = (data && data.error) ? data.error : (fallbackMessage || `Request failed (${response.status})`);
      if (response.status === 429) {
        addLog("Too many active jobs. Please wait for running jobs.", "warning");
      }
      throw new Error(message);
    }
    return data;
  }

  async function confirmProjectDelete(url) {
    const modal = document.getElementById("confirm-modal");
    const titleEl = document.getElementById("confirm-title");
    const textEl = document.getElementById("confirm-text");
    const checkWrap = document.getElementById("confirm-check-wrap");
    const checkEl = document.getElementById("confirm-check");
    const checkLabel = document.getElementById("confirm-check-label");
    const okBtn = document.getElementById("confirm-ok-btn");
    const cancelBtn = document.getElementById("confirm-cancel-btn");

    if (!modal || !titleEl || !textEl || !okBtn || !cancelBtn || !checkWrap || !checkEl || !checkLabel) {
      const ok = window.confirm(`Delete recent project and related local cache?\n\n${url}`);
      if (!ok) return Promise.resolve({ confirmed: false, deleteOutputFiles: false });
      const deleteOutputFiles = window.confirm("Also delete local output folder files for this project?");
      return Promise.resolve({ confirmed: true, deleteOutputFiles });
    }

    let preview = { deletable: [], skipped: [], invalid: [] };
    try {
      const res = await fetch("/recent-projects/delete-preview?target_url=" + encodeURIComponent(url));
      const data = await res.json();
      if (data && data.ok) {
        preview = {
          deletable: Array.isArray(data.deletable) ? data.deletable : [],
          skipped: Array.isArray(data.skipped) ? data.skipped : [],
          invalid: Array.isArray(data.invalid) ? data.invalid : []
        };
      }
    } catch (_e) {
      // preview unavailable, continue with generic text
    }

    titleEl.textContent = "Delete Project";
    const line1 = `Delete project data for:\n${url}`;
    const line2 = `Output folders linked: ${preview.deletable.length}`;
    const line3 = preview.skipped.length ? `Outside safe output root (will skip): ${preview.skipped.length}` : "";
    textEl.textContent = [line1, line2, line3].filter(Boolean).join("\n");
    checkWrap.style.display = "inline-flex";
    checkEl.checked = false;
    checkLabel.textContent = "Also delete local output folder files";

    return new Promise(function (resolve) {
      function cleanup() {
        modal.classList.remove("show");
        modal.setAttribute("aria-hidden", "true");
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onBackdrop);
      }
      function onOk() {
        const deleteOutputFiles = !!checkEl.checked;
        cleanup();
        resolve({ confirmed: true, deleteOutputFiles });
      }
      function onCancel() {
        cleanup();
        resolve({ confirmed: false, deleteOutputFiles: false });
      }
      function onBackdrop(ev) {
        if (ev.target === modal) onCancel();
      }

      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      modal.addEventListener("click", onBackdrop);
      modal.classList.add("show");
      modal.setAttribute("aria-hidden", "false");
    });
  }

  function formatElapsedSeconds(sec) {
    const safe = Math.max(0, Number(sec || 0));
    const m = Math.floor(safe / 60);
    const s = Math.floor(safe % 60);
    if (m <= 0) return `${s}s`;
    return `${m}m ${s}s`;
  }

  function detailFromProgress(progress, extras) {
    const p = progress || {};
    const stage = p.stage || "-";
    const elapsed = formatElapsedSeconds(p.elapsed_seconds || 0);
    const current = p.current_item || "-";
    const parts = [`stage ${stage}`, `elapsed ${elapsed}`, `item ${current}`];
    (extras || []).forEach(function (x) {
      if (x) parts.push(x);
    });
    return parts.join(" | ");
  }

  function applySnapshotToPicker(snapshot) {
    const sel = document.getElementById("selected-snapshot");
    if (!sel || !snapshot) return;
    const hasOption = Array.from(sel.options || []).some(function (opt) {
      return opt.value === snapshot;
    });
    if (!hasOption) return;
    sel.value = snapshot;
    sel.dispatchEvent(new Event("change"));
    addLog("Snapshot selected from local data: " + snapshot, "info");
  }

  function setFlowHint(el, text, warn) {
    if (!el) return;
    el.textContent = text;
    if (warn) el.classList.add("warn");
    else el.classList.remove("warn");
  }

  function applyFlowGuidance() {
    const hasInspect = !!context.hasInspect;
    const hasAnalysis = !!context.hasAnalysis;
    const hasCheck = !!context.hasCheck;
    const hasResult = !!context.hasResult;
    const checkMissing = Number(context.checkMissing || 0);

    const analyzeSubmitBtn = document.getElementById("analyze-submit-btn");
    const batchSubmitBtn = document.getElementById("batch-submit-btn");
    const downloadSubmitBtn = document.getElementById("download-submit-btn");
    const sitemapSubmitBtn = document.getElementById("sitemap-submit-btn");
    const checkSubmitBtn = document.getElementById("check-submit-btn");
    const missingSubmitBtn = document.getElementById("missing-submit-btn");

    const analyzeHint = document.getElementById("analyze-flow-hint");
    const downloadHint = document.getElementById("download-flow-hint");
    const checkHint = document.getElementById("check-manifest-hint");
    const missingHint = document.getElementById("missing-flow-hint");

    if (analyzeSubmitBtn) analyzeSubmitBtn.disabled = !hasInspect;
    if (batchSubmitBtn) batchSubmitBtn.disabled = !hasInspect;
    if (!hasInspect) {
      setFlowHint(analyzeHint, "Locked: run Step 1 (Find Snapshots) first.", true);
    } else {
      setFlowHint(analyzeHint, "Pick a snapshot, then run analysis.", false);
    }

    if (downloadSubmitBtn) downloadSubmitBtn.disabled = !hasAnalysis;
    if (sitemapSubmitBtn) sitemapSubmitBtn.disabled = !hasAnalysis;
    if (checkSubmitBtn) checkSubmitBtn.disabled = !hasAnalysis;
    if (!hasAnalysis) {
      setFlowHint(downloadHint, "Locked: run Step 2 (Analyze Snapshot) first.", true);
      if (checkHint) {
        checkHint.textContent = "Locked: run Step 2 (Analyze Snapshot), then Step 3 (Download Offline Copy).";
        checkHint.style.color = "#9a6f12";
      }
    } else {
      setFlowHint(downloadHint, "Run download to build your local offline copy.", false);
    }

    if (missingSubmitBtn) missingSubmitBtn.disabled = !hasCheck;
    if (!hasCheck) {
      setFlowHint(missingHint, "Locked: run Check Downloaded Files first.", true);
    } else if (checkMissing <= 0) {
      setFlowHint(missingHint, "No missing files detected in this check.", false);
    } else {
      setFlowHint(missingHint, "Use this to recover files still missing after download.", false);
    }

    const continueCard = document.getElementById("continue-card");
    const continueTitle = document.getElementById("continue-title");
    const continueText = document.getElementById("continue-text");
    const continueBtn = document.getElementById("continue-action-btn");
    if (!continueCard || !continueTitle || !continueText || !continueBtn) return;

    let label = "Open Next Step";
    let detail = "";
    let targetId = "inspect-form";

    if (!hasInspect) {
      detail = "Start by entering a URL and clicking Find Snapshots.";
      label = "Go to Step 1";
      targetId = "inspect-form";
    } else if (!hasAnalysis) {
      detail = "You already have snapshots. Choose one and run Analyze Selected Snapshot.";
      label = "Go to Step 2";
      targetId = "analyze-form";
    } else if (!hasResult) {
      detail = "Analysis is ready. Next, build your offline copy using Download Offline Copy.";
      label = "Go to Step 3";
      targetId = "download-form-simple";
    } else if (!hasCheck) {
      detail = "Offline copy exists. Next, run Check Downloaded Files to verify coverage.";
      label = "Go to Step 4";
      targetId = "check-form";
    } else if (checkMissing > 0) {
      detail = `Check found ${checkMissing} missing file(s). Run Recover Missing Files next.`;
      label = "Recover Missing Files";
      targetId = "missing-form";
    } else {
      detail = "Great progress. Your latest check shows no missing files.";
      label = "View Check Results";
      targetId = "check-results";
    }

    continueText.textContent = detail;
    continueBtn.textContent = label;
    continueCard.style.display = "block";
    continueBtn.onclick = function () {
      const el = document.getElementById(targetId);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  }

  function renderProjectSnapshotChips(container, rows) {
    if (!container) return;
    container.innerHTML = "";
    if (!rows || !rows.length) {
      container.innerHTML = '<div class="status-line">No local snapshot data yet</div>';
      return;
    }
    rows.forEach(function (row) {
      const ts = row.snapshot || "";
      if (!ts) return;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "snap-chip";
      b.textContent = `${ts} (${row.age_seconds || 0}s)`;
      b.title = `Use snapshot ${ts}`;
      b.addEventListener("click", function () {
        applySnapshotToPicker(ts);
      });
      container.appendChild(b);
    });
  }

  async function loadProjectDataStatus(url) {
    const card = document.getElementById("project-data-card");
    const summary = document.getElementById("project-data-summary");
    const inspectList = document.getElementById("project-inspect-list");
    const analyzeList = document.getElementById("project-analyze-list");
    const sitemapList = document.getElementById("project-sitemap-list");
    const checkList = document.getElementById("project-check-list");
    if (!card || !summary || !inspectList || !analyzeList || !sitemapList || !checkList) return;

    const target = (url || "").trim();
    if (!target) {
      card.style.display = "none";
      return;
    }

    try {
      const res = await fetch("/project/data-status?target_url=" + encodeURIComponent(target));
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "Could not load project status");
      const s = data.status || {};
      const project = s.project || {};
      const inspect = s.inspect || {};
      const analyze = s.analyze || { count: 0, snapshots: [] };
      const sitemap = s.sitemap || { count: 0, snapshots: [] };
      const check = s.check || { count: 0, snapshots: [] };

      card.style.display = "block";
      summary.textContent = `Snapshot searched first: ${inspect.first_found_snapshot || "-"} | latest found: ${inspect.latest_found_snapshot || "-"} | project last snapshot: ${project.last_snapshot || "-"} | cache-first mode active`;

      inspectList.innerHTML = "";
      const inspectItems = [
        `has data: ${inspect.has_data ? "yes" : "no"}`,
        `total captures: ${inspect.total_snapshots || 0}`,
        `ok captures: ${inspect.total_ok_snapshots || 0}`,
        `latest ok snapshot: ${inspect.latest_ok_snapshot || "-"}`,
        `last inspect cache age: ${inspect.age_seconds || 0}s`
      ];
      inspectItems.forEach(function (t) {
        const li = document.createElement("li");
        li.textContent = t;
        inspectList.appendChild(li);
      });

      renderProjectSnapshotChips(analyzeList, analyze.snapshots || []);
      renderProjectSnapshotChips(sitemapList, sitemap.snapshots || []);
      renderProjectSnapshotChips(checkList, check.snapshots || []);
    } catch (e) {
      addLog("Project data status error: " + e.message, "error");
    }
  }

  const stepOverlay = document.getElementById("step-overlay");
  const stepOverlayTitle = document.getElementById("step-overlay-title");
  const stepOverlayBar = document.getElementById("step-overlay-bar");
  const stepOverlayDetail = document.getElementById("step-overlay-detail");
  const stepOverlayList = document.getElementById("step-overlay-list");
  let stepOverlayTimer = null;

  function showStepOverlay(title, detail, steps) {
    if (!stepOverlay || !stepOverlayTitle || !stepOverlayDetail || !stepOverlayBar || !stepOverlayList) return;
    stepOverlayTitle.textContent = title || "Working...";
    stepOverlayDetail.textContent = detail || "Please wait...";
    let idx = 0;
    let pct = 10;

    stepOverlayList.innerHTML = (steps || []).map((s, i) => `<li class="${i === 0 ? "active" : ""}">${s}</li>`).join("");
    stepOverlayBar.style.width = pct + "%";
    stepOverlay.classList.add("show");
    stepOverlay.setAttribute("aria-hidden", "false");

    if (stepOverlayTimer) clearInterval(stepOverlayTimer);
    stepOverlayTimer = setInterval(() => {
      pct = Math.min(92, pct + 9);
      stepOverlayBar.style.width = pct + "%";
      if (stepOverlayList.children.length > 0) {
        idx = Math.min(idx + 1, stepOverlayList.children.length - 1);
        [...stepOverlayList.children].forEach((li, i) => li.classList.toggle("active", i === idx));
      }
    }, 700);
  }

  const targetInput = document.getElementById("target-url");
  if (targetInput) {
    targetInput.addEventListener("blur", function () {
      const url = this.value.trim();
      const outputInput = document.getElementById("output-root");

      if (url && outputInput && !outputInput.value) {
        try {
          const domain = new URL(url).hostname.replace(/^www\./, "");
          const date = new Date().toISOString().slice(0, 10);
          outputInput.value = `./output/${domain}_${date}`;
          addLog(`Auto-set output folder: ${outputInput.value}`);
        } catch (_e) {}
      }

      if (url) loadProjectDataStatus(url);
    });
  }

  const advancedModeToggle = document.getElementById("advanced-mode-toggle");
  if (advancedModeToggle) {
    setAdvancedMode(!!advancedModeToggle.checked);
    advancedModeToggle.addEventListener("change", function () {
      setAdvancedMode(!!advancedModeToggle.checked);
    });
  }

  const scanMode = document.getElementById("scan-mode");
  if (scanMode) {
    scanMode.addEventListener("change", function () {
      applyScanPreset();
    });
  }

  const analyzeCountAdvanced = document.getElementById("analyze-count-advanced");
  const analyzeCdxAdvanced = document.getElementById("analyze-cdx-advanced");
  const analyzeCountInput = document.getElementById("analyze-count-input");
  const analyzeCdxInput = document.getElementById("analyze-cdx-input");
  if (analyzeCountAdvanced && analyzeCountInput) {
    analyzeCountAdvanced.addEventListener("input", function () {
      analyzeCountInput.value = analyzeCountAdvanced.value || "100000";
    });
  }
  if (analyzeCdxAdvanced && analyzeCdxInput) {
    analyzeCdxAdvanced.addEventListener("change", function () {
      analyzeCdxInput.value = analyzeCdxAdvanced.value || "12000";
    });
  }

  document.querySelectorAll("[data-recent-url]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const url = btn.getAttribute("data-recent-url") || "";
      const out = btn.getAttribute("data-recent-output") || "";
      const openUrl = "/project/open?target_url=" + encodeURIComponent(url) + "&output_root=" + encodeURIComponent(out);
      window.location.href = openUrl;
    });
  });

  document.querySelectorAll("[data-delete-project-url]").forEach(function (btn) {
    btn.addEventListener("click", async function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      const url = btn.getAttribute("data-delete-project-url") || "";
      if (!url) return;
      const confirm = await confirmProjectDelete(url);
      if (!confirm.confirmed) return;
      const deleteOutputFiles = !!confirm.deleteOutputFiles;

      try {
        const res = await fetch("/recent-projects/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target_url: url, purge_related: true, delete_output_files: deleteOutputFiles })
        });
        const data = await readApiResult(res, "Delete failed");

        document.querySelectorAll(`[data-project-row="${CSS.escape(url)}"]`).forEach(function (row) {
          row.remove();
        });
        const out = data.output_deleted || {};
        const deletedCount = Array.isArray(out.deleted) ? out.deleted.length : 0;
        const skippedCount = Array.isArray(out.skipped) ? out.skipped.length : 0;
        const failedCount = Array.isArray(out.failed) ? out.failed.length : 0;
        if (deleteOutputFiles) {
          addLog(`Deleted recent project: ${url} | output deleted ${deletedCount}, skipped ${skippedCount}, failed ${failedCount}`, "warning");
        } else {
          addLog(`Deleted recent project: ${url}`, "warning");
        }
        window.setTimeout(function () {
          window.location.reload();
        }, 250);
      } catch (e) {
        addLog("Delete failed: " + e.message, "error");
      }
    });
  });

  document.querySelectorAll("form").forEach(function (form) {
    if (CSRF_TOKEN && !form.querySelector('input[name="csrf_token"]')) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      input.value = CSRF_TOKEN;
      form.appendChild(input);
    }
  });

  const pickFolderBtn = document.getElementById("pick-folder-btn");
  if (pickFolderBtn) {
    pickFolderBtn.addEventListener("click", async function () {
      const outputInput = document.getElementById("output-root");
      if (!outputInput) return;
      if (!window.showDirectoryPicker) {
        addLog("Folder picker is not supported in this browser; type path manually.", "warning");
        alert("Folder picker is not supported in this browser. Please type folder path manually.");
        return;
      }
      try {
        const handle = await window.showDirectoryPicker();
        outputInput.value = outputInput.value || `./output/${handle.name}`;
        addLog(`Folder selected: ${handle.name} (browser security hides full path)`, "info");
      } catch (_e) {
        addLog("Folder selection cancelled", "warning");
      }
    });
  }

  addLog("Application loaded. Waiting for URL input.");
  const initialTargetUrl = (document.getElementById("target-url") || {}).value || "";
  if (initialTargetUrl.trim()) {
    loadProjectDataStatus(initialTargetUrl.trim());
  }
  applyFlowGuidance();

  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
      this.classList.add("active");
      const tab = this.dataset.goTab;
      const map = {
        inspect: "inspect-results",
        analyze: "analyze-results",
        sitemap: "sitemap-results",
        check: "check-results",
        download: "download-results"
      };
      const el = document.getElementById(map[tab] || "inspect-results");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  const snapSelect = document.getElementById("selected-snapshot");
  const previewFrame = document.getElementById("preview-frame");
  const previewLink = document.getElementById("preview-link");
  const openCachedSnapshotBtn = document.getElementById("open-cached-snapshot-btn");
  const targetUrlVal = (document.getElementById("target-url") || {}).value || context.targetUrl || "";
  if (snapSelect && previewFrame && previewLink) {
    snapSelect.addEventListener("change", function () {
      const ts = this.value;
      const url = `https://web.archive.org/web/${ts}/${targetUrlVal}`;
      previewFrame.src = url;
      previewLink.href = url;
      addLog(`Selected snapshot: ${ts}`, "info");
    });

    document.querySelectorAll(".snapshot-chip").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const ts = btn.getAttribute("data-ts");
        if (!ts || !snapSelect) return;
        snapSelect.value = ts;
        snapSelect.dispatchEvent(new Event("change"));
        document.querySelectorAll(".snapshot-chip").forEach((x) => x.classList.remove("active"));
        btn.classList.add("active");
      });
    });
  }

  if (openCachedSnapshotBtn) {
    openCachedSnapshotBtn.addEventListener("click", function () {
      const targetInput = document.getElementById("target-url");
      const outputInput = document.getElementById("output-root");
      const selected = (snapSelect && snapSelect.value) || "";
      const target = (targetInput && targetInput.value) || targetUrlVal || "";
      const output = (outputInput && outputInput.value) || "";
      if (!target) {
        addLog("Please select a project URL first", "warning");
        return;
      }
      const openUrl =
        "/project/open?target_url=" +
        encodeURIComponent(target) +
        "&output_root=" +
        encodeURIComponent(output) +
        "&selected_snapshot=" +
        encodeURIComponent(selected);
      window.location.href = openUrl;
    });
  }

  window.WOB = {
    addLog,
    clearLog,
    readApiResult,
    detailFromProgress,
    showStepOverlay,
    applyScanPreset,
    context
  };
  window.clearLog = clearLog;
  window.addLog = addLog;
})();
