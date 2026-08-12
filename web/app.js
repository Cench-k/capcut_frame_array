"use strict";

// 서버가 index.html 을 내보낼 때 심어준 토큰. 모든 API 호출에 붙인다.
const TOKEN = document.body.dataset.token;

const $ = (id) => document.getElementById(id);

const state = {
  ffmpegReady: false,
  folder: "",
  clip: null,        // 고른 영상 조각
  scanId: "",        // 분석 작업 번호. 임계값을 다시 판정할 때 쓴다
  curve: [],         // 점수 곡선 (화면 그리기용)
  cuts: [],          // 현재 임계값으로 판정된 컷
  keep: new Set(),   // 검수에서 살려둔 컷의 시각
  thumbs: new Map(), // 시각 -> {before, after}
};

// ------------------------------------------------------------------ 통신

async function api(path, body) {
  const options = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({ error: "응답을 읽지 못했습니다." }));
  if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

// 백그라운드 작업이 끝날 때까지 상태를 물어본다.
async function waitFor(jobId, onTick) {
  for (;;) {
    const job = await api(`/api/job?id=${jobId}`);
    onTick(job);
    if (job.state === "done") return job.result;
    if (job.state === "error") throw new Error(job.error);
    await new Promise((done) => setTimeout(done, 250));
  }
}

function showError(message) {
  const box = $("alert");
  box.textContent = message;
  box.className = "alert";
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

function clearError() { $("alert").hidden = true; }

function showProgress(id, fraction, text) {
  const box = $(id);
  box.hidden = false;
  box.querySelector(".bar").style.width = `${Math.round(fraction * 100)}%`;
  box.querySelector(".label").textContent = text;
}

// ------------------------------------------------------------------ 형식

const fmtTime = (us) => {
  const total = us / 1e6;
  const m = Math.floor(total / 60);
  const s = total - m * 60;
  return `${m}:${s.toFixed(2).padStart(5, "0")}`;
};

const fmtDur = (us) => `${(us / 1e6).toFixed(1)}초`;

// ------------------------------------------------------------ ffmpeg 준비

// ffmpeg 없이는 분석도 썸네일도 안 되므로, 없으면 맨 위에 배너를 띄우고 분석 버튼을 막는다.
async function checkFfmpeg() {
  try {
    const info = await api("/api/ffmpeg");
    state.ffmpegReady = info.ready;
    $("ffmpeg-banner").hidden = info.ready;
    if (!info.ready) {
      $("ffmpeg-manual").textContent =
        `직접 넣으려면: ffmpeg.exe 와 ffprobe.exe 를 ${info.bin_dir} 안에 두세요.`;
    }
    return info.ready;
  } catch (err) {
    showError(err.message);
    return false;
  }
}

async function installFfmpeg() {
  clearError();
  $("btn-install-ffmpeg").disabled = true;
  try {
    const { job } = await api("/api/ffmpeg/install", {});
    await waitFor(job, (j) =>
      showProgress("ffmpeg-progress", j.progress, j.message || "준비 중")
    );
    $("ffmpeg-progress").hidden = true;
    await checkFfmpeg();
    if (state.ffmpegReady) syncScanButton();
  } catch (err) {
    $("ffmpeg-progress").hidden = true;
    showError(err.message);
  } finally {
    $("btn-install-ffmpeg").disabled = false;
  }
}

// ------------------------------------------------------------------ 1단계

async function loadDrafts() {
  const list = $("draft-list");
  try {
    const data = await api("/api/drafts");
    list.innerHTML = '<option value="">— 최근 프로젝트 —</option>';
    for (const draft of data.drafts) {
      const option = document.createElement("option");
      option.value = draft.path;
      option.textContent = draft.name;
      list.append(option);
    }
    if (!data.drafts.length) {
      showError(`CapCut 프로젝트를 찾지 못했습니다.\n${data.root}`);
    }
  } catch (err) { showError(err.message); }
}

async function openDraft(folder) {
  if (!folder) return;
  clearError();
  try {
    const draft = await api(`/api/draft?folder=${encodeURIComponent(folder)}`);
    state.folder = folder;
    renderDraft(draft);
  } catch (err) { showError(err.message); }
}

function renderDraft(draft) {
  const tracks = draft.tracks.map((t) => `${t.type} ${t.segments}`).join(" · ");
  $("draft-info").hidden = false;
  $("draft-info").innerHTML =
    `<b>${draft.name}</b> — CapCut ${draft.app_version} · ${draft.width}×${draft.height} · ` +
    `${fmtDur(draft.duration_us)} · 트랙: ${tracks}`;

  const box = $("clip-list");
  box.innerHTML = "";
  state.clip = null;
  syncScanButton();

  if (!draft.clips.length) {
    box.innerHTML = '<p class="note">이 프로젝트에는 영상 조각이 없습니다. (이미지만 있는 프로젝트일 수 있습니다)</p>';
  }

  for (const clip of draft.clips) {
    const row = document.createElement("div");
    row.className = "clip" + (clip.exists ? "" : " gone");
    row.innerHTML =
      `<span class="name">${clip.name}</span>` +
      `<span class="time">${fmtTime(clip.target_start_us)} · ${fmtDur(clip.target_duration_us)}` +
      (clip.speed !== 1 ? ` · ${clip.speed.toFixed(2)}배속` : "") + `</span>`;
    if (clip.exists) {
      row.onclick = () => {
        for (const other of box.children) other.classList.remove("on");
        row.classList.add("on");
        // 조각이 바뀌면 예전 분석은 이 조각의 것이 아니다. 그대로 두면 A 를 분석해서
        // 검수한 컷이 B 에 적용된다.
        if (state.clip && state.clip.segment_id !== clip.segment_id) {
          state.scanId = "";
          state.cuts = [];
          state.curve = [];
          $("step-tune").hidden = true;
          invalidateReview();
        }
        state.clip = clip;
        syncScanButton();
      };
    }
    box.append(row);
  }

  $("step-clip").hidden = false;
  // 프로젝트를 바꾸면 이전 분석 결과는 더 이상 맞지 않는다.
  for (const id of ["step-tune", "step-review", "step-apply"]) $(id).hidden = true;

  const backups = draft.backups || [];
  $("restore-wrap").hidden = !backups.length;
  $("backup-list").innerHTML = backups.map((b) => `<option>${b}</option>`).join("");
}

// ------------------------------------------------------------------ 2단계

// 조각을 골랐고 ffmpeg 도 있어야 분석할 수 있다. 둘 중 무엇이 빠졌는지 알려준다.
function syncScanButton() {
  const ready = Boolean(state.clip) && state.ffmpegReady;
  $("btn-scan").disabled = !ready;
  if (!state.clip) {
    $("scan-note").textContent = "";
  } else if (!state.ffmpegReady) {
    $("scan-note").textContent = "먼저 위에서 ffmpeg 를 설치하세요.";
  } else {
    $("scan-note").textContent = `분석할 길이 ${fmtDur(state.clip.source_duration_us)}`;
  }
}

async function startScan() {
  clearError();
  $("btn-scan").disabled = true;
  try {
    const { job } = await api("/api/scan", {
      folder: state.folder,
      segment_id: state.clip.segment_id,
      whole_file: $("whole-file").checked,
    });
    const result = await waitFor(job, (j) =>
      showProgress("scan-progress", j.progress, `${j.message} ${Math.round(j.progress * 100)}%`)
    );
    state.scanId = job;
    $("scan-progress").hidden = true;
    applyScanResult(result);
  } catch (err) {
    $("scan-progress").hidden = true;
    showError(err.message);
  } finally {
    syncScanButton();
  }
}

function applyScanResult(result) {
  state.curve = result.curve;
  state.cuts = result.cuts;

  $("threshold").value = result.settings.threshold;
  $("minscene").value = result.settings.min_scene_us / 1000;
  $("adaptive").checked = result.settings.adaptive;
  syncOutputs();

  if (result.video.variable_fps) {
    showError(
      "이 영상은 가변 프레임레이트(VFR)입니다. 컷 위치는 ffmpeg 이 알려준 실제 시각을 쓰므로 " +
      "정확하지만, CapCut 에서 프레임 단위로 미세조정할 때 한 프레임 정도 어긋나 보일 수 있습니다."
    );
  }

  $("step-tune").hidden = false;
  drawCurve();
  renderCutCount();
  $("step-tune").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------------ 3단계

function syncOutputs() {
  $("out-threshold").textContent = Number($("threshold").value).toFixed(1);
  $("out-minscene").textContent = `${Number($("minscene").value)}ms`;
}

// 슬라이더를 움직일 때마다 서버를 부르면 요청이 밀린다. 잠깐 멈췄을 때만 보낸다.
let recutTimer = null;
function scheduleRecut() {
  syncOutputs();
  drawCurve();
  clearTimeout(recutTimer);
  recutTimer = setTimeout(recut, 160);
}

async function recut() {
  if (!state.scanId) return;
  try {
    const data = await api("/api/cuts", {
      scan_id: state.scanId,
      threshold: Number($("threshold").value),
      adaptive: $("adaptive").checked,
      min_scene_us: Number($("minscene").value) * 1000,
    });
    state.cuts = data.cuts;
    // 컷 목록이 바뀌면 방금까지 보던 검수 결과는 더 이상 이 목록을 가리키지 않는다.
    // 남겨두면 화면에 보이는 것과 실제로 적용될 것이 어긋난다. 접고 다시 뽑게 한다.
    invalidateReview();
    renderCutCount();
    drawCurve();
  } catch (err) { showError(err.message); }
}

// 검수 결과를 버린다. 컷 목록이 달라졌거나 다른 조각을 고른 경우.
function invalidateReview() {
  state.keep = new Set();
  state.thumbs = new Map();
  $("cut-grid").innerHTML = "";
  $("step-review").hidden = true;
  $("step-apply").hidden = true;
}

function renderCutCount() {
  const pieces = state.cuts.length + 1;
  $("cut-count").textContent = `컷 ${state.cuts.length}개 → 조각 ${pieces}개`;
  $("btn-thumbs").disabled = state.cuts.length === 0;
}

function drawCurve() {
  const canvas = $("curve");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = 110;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  if (!state.curve.length) return;

  const threshold = Number($("threshold").value);
  // 임계값이 늘 보이도록 위쪽에 여유를 둔다. 곡선 최대값만 쓰면 임계값을 높였을 때
  // 가로선이 화면 밖으로 나가 버린다.
  const top = Math.max(...state.curve, threshold * 1.15) || 1;
  const step = width / state.curve.length;

  for (let i = 0; i < state.curve.length; i++) {
    const value = state.curve[i];
    const barHeight = (value / top) * (height - 12);
    ctx.fillStyle = value >= threshold ? "#5b9dff" : "#3c4250";
    ctx.fillRect(i * step, height - barHeight, Math.max(step - 0.4, 0.7), barHeight);
  }

  const line = height - (threshold / top) * (height - 12);
  ctx.strokeStyle = "#ffb84d";
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(0, line);
  ctx.lineTo(width, line);
  ctx.stroke();
  ctx.setLineDash([]);
}

// ------------------------------------------------------------------ 4단계

async function loadThumbs() {
  clearError();
  $("btn-thumbs").disabled = true;
  const times = state.cuts.map((c) => c.time_us);
  try {
    const { job } = await api("/api/thumbs", { scan_id: state.scanId, times_us: times });
    const result = await waitFor(job, (j) =>
      showProgress("thumb-progress", j.progress, `썸네일 뽑는 중 ${j.message}`)
    );
    $("thumb-progress").hidden = true;
    state.thumbs = new Map(result.thumbs.map((t) => [t.time_us, t]));
    state.keep = new Set(times);
    renderReview();
  } catch (err) {
    $("thumb-progress").hidden = true;
    showError(err.message);
  } finally {
    $("btn-thumbs").disabled = false;
  }
}

function renderReview() {
  const grid = $("cut-grid");
  grid.innerHTML = "";

  for (const cut of state.cuts) {
    const pair = state.thumbs.get(cut.time_us) || {};
    const card = document.createElement("div");
    card.className = "cut on";
    card.innerHTML =
      `<div class="pics">` +
      `<img alt="직전" src="${pair.before || ""}">` +
      `<span class="arrow">›</span>` +
      `<img alt="직후" src="${pair.after || ""}">` +
      `</div>` +
      `<div class="meta"><span class="t">${fmtTime(cut.time_us)}</span>` +
      `<span>점수 ${cut.score}</span></div>`;
    card.onclick = () => {
      if (state.keep.has(cut.time_us)) state.keep.delete(cut.time_us);
      else state.keep.add(cut.time_us);
      card.classList.toggle("on");
      card.classList.toggle("off");
      renderReviewCount();
    };
    grid.append(card);
  }

  $("step-review").hidden = false;
  $("step-apply").hidden = false;
  renderReviewCount();
  checkCapCut();
  $("step-review").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderReviewCount() {
  $("review-count").textContent = `${state.keep.size} / ${state.cuts.length} 개 선택됨`;
  // CapCut 이 켜져 있어 막아둔 상태라면 그대로 둔다.
  const blocked = !$("capcut-warn").hidden;
  $("btn-apply").disabled = blocked || state.keep.size === 0;
}

function setAll(on) {
  state.keep = on ? new Set(state.cuts.map((c) => c.time_us)) : new Set();
  for (let i = 0; i < $("cut-grid").children.length; i++) {
    const card = $("cut-grid").children[i];
    card.classList.toggle("on", on);
    card.classList.toggle("off", !on);
  }
  renderReviewCount();
}

// ------------------------------------------------------------------ 5단계

// CapCut 이 켜진 채로 적용하면 저장은 되는데 CapCut 이 곧바로 되돌린다. 오류가 안 나므로
// 사용자는 '프로그램이 안 먹는다' 고만 느낀다. 그래서 누르기 전에 미리 보여주고 막는다.
async function checkCapCut() {
  try {
    const { running } = await api("/api/capcut");
    $("capcut-warn").hidden = !running;
    $("capcut-ok").hidden = running;
    $("btn-apply").disabled = running || state.keep.size === 0;
    return running;
  } catch {
    return false;  // 확인 못 했다고 못 쓰게 막지는 않는다
  }
}

async function applyCuts() {
  clearError();
  $("btn-apply").disabled = true;
  try {
    const cuts = state.cuts.map((c) => c.time_us).filter((t) => state.keep.has(t));
    const result = await api("/api/apply", {
      folder: state.folder,
      segment_id: state.clip.segment_id,
      cuts_us: cuts,
      backup: $("backup").checked,
    });
    let text = `조각 ${result.pieces}개로 나눴습니다. (컷 ${result.applied_cuts}개 적용)`;
    if (result.backup) text += `\n백업: ${result.backup}`;
    if (result.skipped.length) text += `\n건너뜀(키프레임 있음): ${result.skipped.join(", ")}`;
    text += "\n\nCapCut 에서 프로젝트를 열어 확인하세요.";
    $("apply-result").hidden = false;
    $("apply-result").textContent = text;
    await openDraftKeepingResult();
  } catch (err) {
    showError(err.message);
  } finally {
    await checkCapCut();
  }
}

// 적용 뒤 백업 목록만 새로 읽는다. 화면 전체를 다시 그리면 방금 나온 결과가 사라진다.
async function openDraftKeepingResult() {
  try {
    const draft = await api(`/api/draft?folder=${encodeURIComponent(state.folder)}`);
    const backups = draft.backups || [];
    $("restore-wrap").hidden = !backups.length;
    $("backup-list").innerHTML = backups.map((b) => `<option>${b}</option>`).join("");
  } catch { /* 목록 갱신 실패는 치명적이지 않다 */ }
}

async function restore() {
  clearError();
  try {
    const result = await api("/api/restore", {
      folder: state.folder,
      backup: $("backup-list").value,
    });
    $("apply-result").hidden = false;
    $("apply-result").textContent =
      `${result.restored} 로 되돌렸습니다. 지금 영상 조각은 ${result.clips}개입니다.`;
  } catch (err) { showError(err.message); }
}

// ------------------------------------------------------------------ 연결

$("draft-list").onchange = (e) => openDraft(e.target.value);
$("btn-reload").onclick = loadDrafts;
$("btn-pick").onclick = async () => {
  try {
    const { folder } = await api("/api/pick");
    if (folder) {
      await loadDrafts();
      $("draft-list").value = folder;
      await openDraft(folder);
    }
  } catch (err) { showError(err.message); }
};

$("btn-scan").onclick = startScan;
$("threshold").oninput = scheduleRecut;
$("minscene").oninput = scheduleRecut;
$("adaptive").onchange = recut;
$("btn-thumbs").onclick = loadThumbs;
$("btn-all").onclick = () => setAll(true);
$("btn-none").onclick = () => setAll(false);
$("btn-apply").onclick = applyCuts;
$("btn-restore").onclick = restore;
$("btn-install-ffmpeg").onclick = installFfmpeg;
$("btn-recheck").onclick = checkCapCut;
window.addEventListener("resize", drawCurve);

checkFfmpeg();
loadDrafts();
