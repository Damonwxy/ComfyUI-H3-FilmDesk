import { registerSiteTools } from "./site-tools.mjs";
import { initCopyReview } from "./copy-review.mjs";
import { initDeliveryReview } from "./delivery-review.mjs";
import { initProjectPlan } from "./project-plan.mjs";
import { frameRegionEditor } from "./frame-region.mjs";
import { matchedRange } from "./compare-cut.mjs";

const api = "/localhub/v2";
const el = (id) => document.getElementById(id);
const statuses = {
  accepted: "已接收", submitting: "提交中", pending: "排队中", in_progress: "生成中",
  completed: "已完成", failed: "失败", cancelled: "已取消", needs_reconciliation: "待核对",
};
const terminal = new Set(["completed", "failed", "cancelled"]);
const audioFlags = {
  missing_audio: "无音轨", no_signal: "无可测音频信号", sample_at_full_scale: "样本触满幅，复听是否削波",
  near_ceiling: "真峰值接近或超过满幅", quiet: "整体响度偏低", extended_silence: "存在较长静音",
  av_timing: "音画起止时间有偏差", timing_unavailable: "无法核对音画起止时间", multiple_audio_streams: "有多条音轨，只检查了第一条",
};
const state = {
  project: new URLSearchParams(location.search).get("project") || "本地项目",
  jobs: [], references: [], selected: null, outputIndex: 0, revision: 0, edit: null, dirty: false, editVersion: 0, polling: null,
  generation: false,
};
el("project").value = state.project;
const copyReview = initCopyReview({ el, node, request, send, project: () => state.project });
const deliveryReview = initDeliveryReview({ el, node, request, send, jobs: () => state.jobs });
const projectPlan = initProjectPlan({ el, node, request, send, jobs: () => state.jobs, references: () => state.references,
  refreshReferences,
  project: () => state.project, prepare: preparePlannedGeneration, draftCut: loadPlanCut,
  canGenerate: () => state.generation,
  exportCut: (revision, title) => downloadCut(state.project, revision, (title + "-v" + revision).replace(/[<>:"/\\|?*]/g, "_")),
});

function query(project = state.project) {
  return new URLSearchParams({ project });
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const reply = await response.json();
  if (!response.ok) throw Object.assign(new Error(reply.error || "请求失败"), { status: response.status });
  return reply;
}

function send(path, method, value, headers = {}, signal) {
  return request(path, {
    method, headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(value), signal,
  });
}

function message(text, error = false) {
  const notice = el("notice");
  notice.hidden = !text;
  notice.textContent = text;
  notice.dataset.error = String(error);
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(label, action, disabled = false) {
  const element = node("button", label);
  element.type = "button";
  element.disabled = disabled;
  element.addEventListener("click", action);
  return element;
}

function currentJob() {
  return state.jobs.find((job) => job.id === state.selected);
}

function currentOutput(job) {
  return (job?.outputs || []).find((entry) => entry.available && entry.index === state.outputIndex);
}

let audioSelection;

function renderAudioReport(report) {
  const panel = el("audio-result");
  panel.replaceChildren();
  if (!report) return;
  const metrics = report.audio;
  panel.append(node("p", report.status === "pass" ? "音频指标通过" : "音频需复核"));
  if (metrics) {
    const loudness = metrics.integrated_lufs === null ? "响度不可测" : metrics.integrated_lufs + " LUFS";
    const peak = metrics.true_peak_dbtp === null ? "真峰值不可测" : metrics.true_peak_dbtp + " dBTP";
    panel.append(node("p", loudness + " · " + peak + " · 最长静音 " + metrics.longest_silence_s.toFixed(2) + " 秒", "quiet"));
  }
  if (report.timing) {
    const ms = (value) => Math.round(value * 1000) + " ms";
    panel.append(node("p", "音轨相对画面起止偏差 " + ms(report.timing.audio_start_delta_s) + " / "
      + ms(report.timing.audio_end_delta_s), "quiet"));
  }
  for (const flag of report.flags) panel.append(node("p", audioFlags[flag] || flag));
}

function selectAudioCandidate(url) {
  const check = el("check-audio");
  check.hidden = !url;
  if (audioSelection?.url === url) return;
  audioSelection = url ? { url, checking: false, requested: false } : null;
  const selection = audioSelection;
  check.disabled = false;
  renderAudioReport(null);
  if (!selection) return;
  request(url).then(({ report }) => {
    if (audioSelection === selection && !selection.requested) renderAudioReport(report);
  }).catch((error) => {
    if (audioSelection === selection && !selection.requested) el("audio-result").textContent = error.message;
  });
}

function renderJobs() {
  const shots = el("shots");
  shots.replaceChildren();
  if (state.jobs.length === 0) {
    shots.append(node("p", "这个项目还没有候选。", "quiet"));
    const brief = button(projectPlan.hasShots() ? "继续编辑分镜" : "从创作简报开始", () => {
      el("project-plan").open = true;
      el("plan-brief").open = !projectPlan.hasShots();
      const target = projectPlan.hasShots() ? el("plan-work").querySelector('[aria-pressed="true"]') :
        el("plan-metadata").querySelector("textarea");
      (target || el("project-plan").querySelector("summary")).focus();
    });
    brief.id = "plan-entry";
    const footage = button("使用已有实拍或成片", () => {
      const form = el("register-media");
      form.closest("details").open = true;
      form.elements.shot.focus();
    });
    shots.append(brief, footage);
  }
  const groups = new Map();
  for (const job of state.jobs) {
    if (!groups.has(job.shot)) groups.set(job.shot, []);
    groups.get(job.shot).push(job);
  }
  for (const [shot, jobs] of groups) {
    const group = node("section");
    group.append(node("h3", shot));
    for (const job of jobs) {
      const date = new Date(job.created * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
      const name = job.source === "imported" ? job.outputs[0]?.name : null;
      const label = name || [date, job.info?.width && job.info?.height ? job.info.width + "×" + job.info.height : null,
        job.info?.seed != null ? "种子 " + job.info.seed : null].filter(Boolean).join(" · ");
      const choice = button(label, () => showCandidate(job));
      const status = job.source === "imported" ? "已登记" : statuses[job.state] || job.state;
      choice.setAttribute("aria-pressed", String(state.selected === job.id));
      choice.setAttribute("aria-label", shot + " " + label + " " + status);
      choice.append(node("span", status));
      group.append(choice);
    }
    shots.append(group);
  }
  renderPreview();
}

function showCandidate(job) {
  state.selected = job.id;
  state.outputIndex = job.outputs.find((entry) => entry.available)?.index || 0;
  renderJobs();
}

let stopComparison = null;

function closeComparison() {
  stopComparison?.();
  stopComparison = null;
  el("compare-choice").value = "";
  el("compare-status").textContent = "";
  el("compare-play").hidden = true;
  el("compare-cut").hidden = true;
  el("compare-segment").replaceChildren();
  delete el("compare-segment").dataset.choices;
  el("compare-decision").textContent = "";
  delete el("compare-decision").dataset.context;
}

function comparedMedia() {
  const url = el("compare-choice").value;
  let peer;
  for (const job of state.jobs) {
    const output = job.outputs.find(item => item.available && item.url === url);
    if (output) { peer = { job, output }; break; }
  }
  return [{ job: currentJob(), output: currentOutput(currentJob()) }, peer];
}

function renderComparisonCut() {
  const panel = el("compare-cut");
  if (!el("compare-choice").value || !state.edit) { panel.hidden = true; return; }
  const sides = comparedMedia();
  panel.hidden = !sides.every(side => side?.output);
  if (panel.hidden) return;
  const choices = [];
  state.edit.tracks.forEach((track, t) => {
    if (track.kind !== "Video") return;
    track.clips.forEach((clip, c) => {
      const side = sides.findIndex(item => item.job.id === clip.job_id && item.output.index === (clip.file_index ?? 0));
      if (side >= 0) choices.push({ key: t + "/" + c, side, label: track.name + " · 第 " + (c + 1) +
        " 段 · " + (side === 0 ? "A" : "B") + " · 源入点 " + clip.start_frame + " 帧" });
    });
  });
  const select = el("compare-segment");
  const signature = JSON.stringify(choices);
  if (signature !== select.dataset.choices) {
    const previous = select.value;
    select.replaceChildren(...choices.map(row => new Option(row.label, row.key)));
    if (!choices.length) select.add(new Option("加入画面轨末尾", "append"));
    if (choices.length > 1) {
      select.add(new Option("请选择要替换的片段", ""), 0);
      select.value = choices.some(row => row.key === previous) ? previous : "";
    }
    select.dataset.choices = signature;
  }
  const used = choices.find(row => row.key === select.value)?.side;
  el("choose-a").disabled = !select.value || used === 0;
  el("choose-b").disabled = !select.value || used === 1;
  const cut = state.dirty ? "待保存草稿" : "剪辑 v" + state.revision;
  const feedback = el("compare-decision");
  const context = JSON.stringify([select.value, signature, state.editVersion, state.revision, state.dirty]);
  if (feedback.dataset.context !== context) {
    feedback.dataset.context = context;
    feedback.textContent = used === undefined ? "" : cut + " · 此段使用 " + (used === 0 ? "A" : "B");
  }
}

async function adoptSide(side) {
  const media = comparedMedia()[side];
  if (!media?.output || !state.edit) return;
  const select = el("compare-segment");
  if (!select.value) return;
  if (select.value === "append") {
    append("Video", media.job, media.output);
  } else {
    const choice = select.value;
    const [t, c] = choice.split("/").map(Number);
    const edit = state.edit, version = state.editVersion, compared = el("compare-choice").value;
    const primary = currentOutput(currentJob())?.url;
    const clip = edit.tracks[t].clips[c];
    const stillSelected = () => state.edit === edit && state.editVersion === version &&
      compared === el("compare-choice").value && choice === select.value && primary === currentOutput(currentJob())?.url;
    let range;
    try {
      const timing = await request(media.output.url + "/frame-notes?" + query());
      range = matchedRange(clip, edit.rate, timing);
    } catch (error) {
      if (stillSelected()) el("compare-decision").textContent = error.message;
      return;
    }
    if (!stillSelected()) return;
    Object.assign(clip, range, { job_id: media.job.id, file_index: media.output.index });
    markDirty();
    renderCut();
  }
  el("compare-decision").textContent = "剪辑草稿已采用 " + (side === 0 ? "A" : "B") + "，保存清单后生成新版本。";
}

el("compare-segment").addEventListener("change", renderComparisonCut);
el("choose-a").addEventListener("click", perform(() => adoptSide(0)));
el("choose-b").addEventListener("click", perform(() => adoptSide(1)));

function updateCompareChoices(job, output) {
  const select = el("compare-choice");
  const candidates = [];
  if (job?.kind === "video" && output) {
    const videos = state.jobs.filter((item) => item.kind === "video" && item.state === "completed");
    videos.sort((a, b) => Number(b.shot === job.shot) - Number(a.shot === job.shot));
    for (const item of videos) {
      for (const file of item.outputs) {
        if (file.available && file.url !== output.url) {
          let label = file.name;
          if (item.source !== "imported") {
            const dimensions = item.info.width && item.info.height ? item.info.width + "×" + item.info.height + " / " : "";
            label = dimensions + item.id.slice(0, 8);
            if (item.outputs.length > 1) label += " / 输出 " + (file.index + 1);
          }
          candidates.push([file.url, item.shot + " / " + label]);
        }
      }
    }
  }
  el("compare-bar").hidden = candidates.length === 0;
  const signature = JSON.stringify(candidates);
  if (signature === select.dataset.choices) return;
  const previous = select.value;
  select.dataset.choices = signature;
  select.replaceChildren(new Option("单候选预览", ""), ...candidates.map(([url, name]) => new Option(name, url)));
  if (candidates.some(([url]) => url === previous)) select.value = previous;
  else closeComparison();
}

el("compare-choice").addEventListener("change", () => {
  const select = el("compare-choice");
  const url = select.value;
  closeComparison();
  if (!url) return;
  select.value = url;
  renderComparisonCut();
  const monitor = el("monitor");
  const primary = monitor.querySelector("video");
  primary.pause();
  const peer = node("video");
  peer.muted = true;
  peer.playsInline = true;
  peer.preload = "auto";
  peer.setAttribute("aria-label", "对照候选 B");
  primary.setAttribute("aria-label", "审片候选 A");
  const a = node("span", "A · 审片与声音", "compare-label");
  const b = node("span", "B · 对照", "compare-label");
  monitor.classList.add("comparing");
  monitor.append(peer, a, b);
  const status = el("compare-status");
  const play = el("compare-play");
  play.hidden = false;
  play.disabled = true;
  play.textContent = "同步播放";
  status.textContent = "正在载入 B…";
  const events = new AbortController();
  const listen = (media, event, listener) => media.addEventListener(event, listener, { signal: events.signal });
  let tick = 0;
  function align() {
    if (!Number.isFinite(peer.duration)) return;
    const time = Math.min(primary.currentTime, peer.duration);
    if (Math.abs(peer.currentTime - time) > 0.001) peer.currentTime = time;
  }
  function pausePair() {
    cancelAnimationFrame(tick);
    peer.pause();
    align();
    if (!peer.error && peer.readyState >= 1) status.textContent = primary.currentTime >= peer.duration ? "B 已到片尾" : "";
    play.textContent = "同步播放";
  }
  function track() {
    if (primary.paused || events.signal.aborted) return;
    if (primary.currentTime >= peer.duration) {
      peer.pause();
      align();
      status.textContent = "B 已到片尾";
    } else if (!peer.seeking && Math.abs(primary.currentTime - peer.currentTime) > 0.04) {
      align();
    }
    tick = requestAnimationFrame(track);
  }
  function startPair() {
    cancelAnimationFrame(tick);
    if (primary.paused || primary.seeking || peer.readyState < 1) return;
    align();
    peer.playbackRate = primary.playbackRate;
    if (primary.currentTime < peer.duration) {
      status.textContent = "";
      peer.play().catch((error) => {
        if (!events.signal.aborted && error.name !== "AbortError") {
          primary.pause();
          status.textContent = "B 播放失败，请重新选择对照候选。";
        }
      });
    }
    play.textContent = "同步暂停";
    tick = requestAnimationFrame(track);
  }
  listen(peer, "loadedmetadata", () => {
    b.textContent = "B · 对照 " + peer.videoWidth + "×" + peer.videoHeight;
    play.disabled = false;
    status.textContent = "";
    align();
    if (!primary.paused) startPair();
  });
  listen(peer, "error", () => {
    primary.pause();
    play.disabled = true;
    status.textContent = "无法读取 B，请重新选择对照候选。";
  });
  listen(primary, "playing", startPair);
  for (const event of ["pause", "ended", "waiting", "seeking"]) listen(primary, event, pausePair);
  listen(primary, "seeked", () => primary.paused ? align() : startPair());
  listen(primary, "ratechange", () => { peer.playbackRate = primary.playbackRate; });
  listen(play, "click", async () => {
    if (!primary.paused) primary.pause();
    else {
      try { await primary.play(); }
      catch (error) { if (!events.signal.aborted) status.textContent = error.message; }
    }
  });
  stopComparison = () => {
    events.abort();
    cancelAnimationFrame(tick);
    primary.pause();
    peer.pause();
    peer.removeAttribute("src");
    peer.load();
    peer.remove();
    a.remove();
    b.remove();
    primary.removeAttribute("aria-label");
    monitor.classList.remove("comparing");
  };
  peer.src = url;
});

let regionEditor;

function reviewKey(project) { return "localhub.review.v1:" + project; }

function lastReview(project) {
  try {
    const saved = JSON.parse(localStorage.getItem(reviewKey(project)));
    if (typeof saved?.job_id === "string" && Number.isInteger(saved.file_index) && saved.file_index >= 0) return saved;
  } catch { /* Browsing still works when storage is unavailable. */ }
  return null;
}

function rememberReview(identity, location = {}) {
  if (identity.project !== state.project || identity.job_id !== state.selected || identity.file_index !== state.outputIndex) return;
  try {
    localStorage.setItem(reviewKey(identity.project), JSON.stringify({
      job_id: identity.job_id, file_index: identity.file_index, ...location,
    }));
  } catch { /* Saving a viewing preference is optional. */ }
}

function renderPreview() {
  const job = currentJob();
  const output = currentOutput(job);
  const monitor = el("monitor");
  const selection = output?.url || "empty";
  if (monitor.dataset.selection !== selection) {
    regionEditor?.dispose();
    regionEditor = null;
    closeComparison();
    monitor.replaceChildren();
    el("frame-review").replaceChildren();
    monitor.dataset.selection = selection;
    if (output) {
      let player;
      if (job.kind === "video") player = node("video");
      else if (job.kind === "image") player = node("img");
      else player = node("audio");
      if (player instanceof HTMLMediaElement) {
        player.controls = true;
        player.preload = "metadata";
      } else {
        player.alt = job.shot;
      }
      player.src = output.url;
      monitor.append(player);
      const identity = { project: state.project, job_id: job.id, file_index: output.index, shot: job.shot };
      const saved = lastReview(state.project);
      const previous = saved?.job_id === job.id && saved.file_index === output.index ? saved : null;
      if (!previous) rememberReview(identity);
      if (player instanceof HTMLVideoElement) {
        mountFrameReview(player, output.url + "/frame-notes?" + query(), identity, previous);
      }
    } else {
      monitor.append(node("p", "选择一个已完成的候选来预览"));
    }
  }
  const stateText = job?.source === "imported" ? "本机素材" : job ? (statuses[job.state] || job.state) : "";
  updateCompareChoices(job, output);
  renderComparisonCut();
  el("job-state").textContent = stateText + (job?.cancel_requested ? " · 已请求取消" : "");
  const planned = job?.info?.planned_shot;
  const detail = [job?.shot, planned ? "项目编排 v" + planned.revision : null, job?.info?.frames ? job.info.frames + " 帧" : null,
    job?.delivery === "missing" ? "文件缺失或已变动" : null, job?.error].filter(Boolean);
  el("job-details").textContent = detail.join(" · ");
  const outputs = el("outputs");
  outputs.replaceChildren();
  for (const item of job?.outputs || []) {
    const choice = button(item.name, () => {
      state.outputIndex = item.index;
      renderPreview();
    }, !item.available);
    choice.setAttribute("aria-pressed", String(item.index === state.outputIndex));
    outputs.append(choice);
  }
  el("append-picture").hidden = !output || job.kind !== "video";
  el("append-audio").hidden = !output || !["video", "audio", "music"].includes(job.kind) || job.info.has_audio === false;
  selectAudioCandidate(output && ["video", "audio", "music"].includes(job.kind) ? output.url + "/audio-qc" : null);
  el("cancel").hidden = !state.generation || !job || terminal.has(job.state) || job.cancel_requested;
}

function mountFrameReview(player, url, identity, previous) {
  const panel = node("details", null, "frame-notes");
  const summary = node("summary", "逐帧审片");
  const status = node("p", null, "quiet");
  status.setAttribute("role", "status");
  panel.append(summary, status);
  el("frame-review").append(panel);
  panel.open = Number.isInteger(previous?.frame);
  let loaded = false;
  panel.addEventListener("toggle", async () => {
    if (!panel.isConnected) return;
    if (!panel.open) {
      rememberReview(identity);
      previous = null;
      return;
    }
    if (loaded) return;
    loaded = true;
    status.textContent = "正在读取源帧和批注…";
    let data;
    try {
      data = await request(url);
    } catch (error) {
      status.textContent = error.message + "。收起后可重试。";
      loaded = false;
      return;
    }
    if (!panel.isConnected) return;
    const [n, d = 1] = String(data.fps).split("/").map(Number);
    const fps = n / d;
    const form = node("form");
    const label = node("label", "源帧（从 0 起）");
    const frame = node("input");
    frame.type = "number";
    frame.min = "0";
    frame.max = String(data.frames - 1);
    frame.required = true;
    const currentFrame = () => Math.min(data.frames - 1, Math.floor(player.currentTime * fps));
    frame.value = currentFrame();
    label.append(frame);
    const editor = frameRegionEditor({ player, frame, fps, context: { ...identity, stamp: data.stamp }, node, button, status });
    regionEditor = editor;
    function seek() {
      editor.clear();
      if (!frame.reportValidity()) return;
      player.pause();
      player.currentTime = (frame.valueAsNumber + 0.5) / fps;
      rememberReview(identity, { stamp: data.stamp, frame: frame.valueAsNumber });
    }
    frame.addEventListener("input", () => {
      if (frame.validity.valid) seek();
    });
    const position = button("取当前播放帧", () => {
      editor.clear();
      player.pause();
      frame.value = currentFrame();
      rememberReview(identity, { stamp: data.stamp, frame: frame.valueAsNumber });
    });
    const controls = node("div", null, "inline wrap");
    function step(offset) {
      const from = Number.isInteger(frame.valueAsNumber) ? frame.valueAsNumber : currentFrame();
      frame.value = Math.max(0, Math.min(data.frames - 1, from + offset));
      seek();
    }
    controls.append(label, button("上一帧", () => step(-1)), button("下一帧", () => step(1)),
      position, node("span", data.frames + " 帧 / " + data.fps + " fps", "quiet"));
    const noteLabel = node("label", "审片意见");
    const note = node("textarea");
    note.rows = 2;
    note.maxLength = 1000;
    note.required = true;
    noteLabel.append(note);
    const save = node("button", "保存帧批注");
    save.type = "submit";
    form.append(controls, editor.host, noteLabel, save);
    const list = node("ul");
    function renderNotes(notes) {
      list.replaceChildren();
      for (const entry of notes) {
        const row = node("li");
        row.append(button("第 " + entry.frame + " 帧" + (entry.region ? " · 区域" : ""), () => {
          frame.value = entry.frame;
          if (entry.region) editor.open(entry.region);
          else seek();
          rememberReview(identity, { stamp: data.stamp, frame: entry.frame, region: entry.region, note: entry.note });
        }), node("span", entry.note));
        list.append(row);
      }
    }
    panel.insertBefore(form, status);
    panel.append(list);
    renderNotes(data.notes);
    status.textContent = data.notes.length ? "" : "当前候选还没有帧批注。";
    if (previous?.stamp === data.stamp && Number.isInteger(previous.frame) &&
        previous.frame >= 0 && previous.frame < data.frames) {
      const entry = data.notes.find(item => item.frame === previous.frame && item.note === previous.note &&
        JSON.stringify(item.region) === JSON.stringify(previous.region));
      frame.value = previous.frame;
      if (entry?.region) editor.open(entry.region);
      else {
        seek();
        if (entry) rememberReview(identity, { stamp: data.stamp, frame: entry.frame, note: entry.note });
      }
      status.textContent = "继续上次审片 · 第 " + previous.frame + " 帧" + (entry ? " · " + entry.note : "");
    } else if (previous?.stamp) {
      rememberReview(identity);
      status.textContent = "候选版本已变化，请重新选择审片位置。";
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const area = editor.read();
      if (!area) return;
      const payload = { frame: frame.valueAsNumber, note: note.value, stamp: data.stamp, ...area };
      save.disabled = true;
      try {
        const reply = await send(url, "POST", payload);
        renderNotes(reply.notes);
        if (panel.isConnected && panel.open && frame.valueAsNumber === payload.frame) {
          rememberReview(identity, { stamp: data.stamp, frame: payload.frame, region: payload.region, note: payload.note.trim() });
        }
        if (note.value === payload.note && frame.valueAsNumber === payload.frame) note.value = "";
        status.textContent = "已保存第 " + payload.frame + " 帧的批注。";
      } catch (error) {
        status.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
  });
}

function renderCutControls() {
  renderComparisonCut();
  el("pin-delivery").disabled = !state.revision;
  el("register-delivery").elements.revision.max = state.revision;
  el("revision").textContent = "版本 " + state.revision + (state.dirty ? " · 有待保存修改" : "");
  el("save").disabled = !state.dirty;
  el("export").disabled = state.dirty || !state.revision || !state.edit?.tracks.some((track) => track.clips.length);
  el("export-revision-label").hidden = state.revision < 2;
  el("export-revision").max = state.revision;
}

function markDirty() {
  state.editVersion += 1;
  state.dirty = true;
  renderCutControls();
}

function numberInput(label, value, min, update) {
  const field = node("label", label);
  const input = node("input");
  input.type = "number";
  input.min = String(min);
  input.step = "1";
  input.value = value;
  input.addEventListener("input", () => {
    update(input.valueAsNumber);
    markDirty();
  });
  field.append(input);
  return field;
}

function renderCut() {
  const list = el("tracks");
  list.replaceChildren();
  const tracks = state.edit?.tracks || [];
  if (!tracks.some((track) => track.clips.length)) {
    list.append(node("p", "预览候选后，可将取用范围加入画面轨或声音轨。", "quiet"));
  }
  for (const track of tracks) {
    const section = node("section", null, "track");
    section.append(node("h3", track.name));
    track.clips.forEach((clip, index) => {
      const job = state.jobs.find((row) => row.id === clip.job_id);
      const row = node("div", null, "clip");
      const name = node("span");
      const title = node("span", clip.gap ? "空隙" : cutSourceLabel(job, clip.job_id, clip.file_index));
      if (!clip.gap) {
        title.dataset.cutJob = clip.job_id;
        title.dataset.fileIndex = clip.file_index ?? 0;
      }
      name.append(title, node("span", (clip.rate || state.edit.rate) + " fps", "quiet"));
      row.append(name);
      if (clip.gap) row.append(node("span"));
      else row.append(numberInput("源入点（帧）", clip.start_frame, 0, (v) => { clip.start_frame = v; }));
      row.append(numberInput("取用（帧）", clip.duration_frames, 1, (v) => { clip.duration_frames = v; }));
      const actions = node("div", null, "inline wrap");
      actions.append(button("上移", () => {
        [track.clips[index - 1], track.clips[index]] = [track.clips[index], track.clips[index - 1]];
        markDirty();
        renderCut();
      }, index === 0));
      actions.append(button("移除", () => {
        track.clips.splice(index, 1);
        markDirty();
        renderCut();
      }));
      row.append(actions);
      section.append(row);
    });
    list.append(section);
  }
  renderCutControls();
}

function cutSourceLabel(job, id, index = 0) {
  const output = job?.outputs.find(item => item.index === index);
  const source = job?.source === "imported" && output?.name
    ? output.name : "候选 " + id.slice(0, 8) + " · 输出 " + (index + 1);
  return job ? job.shot + " · " + source : source;
}

async function refreshJobs() {
  const project = state.project;
  if (state.polling === project) return;
  state.polling = project;
  try {
    const reply = await request(api + "/jobs?" + query());
    const active = state.generation ? reply.jobs.filter((job) => !terminal.has(job.state)).slice(0, 8) : [];
    const selected = currentJob();
    const older = selected && !reply.jobs.some((job) => job.id === selected.id) ? selected : null;
    const pending = older ? [...active, older] : active;
    const statuses = await Promise.allSettled(pending.map((job) =>
      request(api + "/jobs/" + encodeURIComponent(job.id) + "?" + query(project))));
    const updated = new Map(statuses.filter((r) => r.status === "fulfilled").map((r) => [r.value.job.id, r.value.job]));
    if (project !== state.project) return;
    state.jobs = reply.jobs.map((job) => updated.get(job.id) || job);
    if (older && state.selected === older.id && updated.has(older.id)) state.jobs.push(updated.get(older.id));
    renderJobs();
    deliveryReview.syncMasters();
    const jobsById = new Map(state.jobs.map(job => [job.id, job]));
    for (const title of el("tracks").querySelectorAll("[data-cut-job]")) {
      const id = title.dataset.cutJob;
      title.textContent = cutSourceLabel(jobsById.get(id), id, Number(title.dataset.fileIndex));
    }
  } finally {
    if (state.polling === project) state.polling = null;
  }
}

async function refreshReferences(project = state.project) {
  const reply = await request(api + "/references?" + query(project));
  if (project !== state.project) return;
  state.references = reply.references;
  projectPlan.refreshStarterReferences();
  const choices = el("registered-reference");
  const selected = choices.value;
  choices.replaceChildren(new Option("请选择参考图及版本", ""), ...reply.references.map((ref) =>
    new Option(ref.asset_id + " v" + ref.revision, JSON.stringify([ref.asset_id, ref.revision]))));
  if ([...choices.options].some((option) => option.value === selected)) choices.value = selected;
  updateReferenceRole();
}

async function refreshLegacyCanvas(project) {
  const { archives } = await request(api + "/legacy-canvas?" + query(project));
  if (project !== state.project) return;
  const output = el("legacy-archives");
  const labels = { linked: "已接入剪辑", missing: "待重连", unlinked: "待登记", archived_file_missing: "档案副本失联" };
  let linked = 0;
  let pending = 0;
  output.replaceChildren();
  for (const archive of archives) {
    const section = node("section", null, "legacy-archive");
    for (const group of archive.groups) {
      section.append(node("p", (group.name || group.id) + " · " + group.asset_ids.join("、"), "quiet"));
    }
    const list = node("ul");
    for (const entry of archive.assets) {
      const { original, state: assetState, job_id } = entry;
      const filename = String(original.path || "").split(/[\\/]/).at(-1);
      const item = node("li", [original.id, filename, labels[assetState]].filter(Boolean).join(" · "));
      if (original.path) item.title = original.path;
      if (assetState === "linked") {
        linked++;
        const open = button("打开片段", perform(async () => {
          const { job } = await request(api + "/jobs/" + encodeURIComponent(job_id) + "?" + query(project));
          if (project !== state.project) return;
          const index = state.jobs.findIndex((candidate) => candidate.id === job.id);
          if (index === -1) state.jobs.push(job);
          else state.jobs[index] = job;
          showCandidate(job);
          el("preview-title").focus();
        }));
        open.setAttribute("aria-label", "打开 " + original.id + " 的片段");
        item.append(open);
      } else {
        pending++;
      }
      list.append(item);
    }
    section.append(list);
    output.append(section);
  }
  el("legacy-status").textContent = linked + " 个可用 · " + pending + " 个待处理";
  el("legacy-canvas").hidden = !archives.length;
}

function updateReferenceRole() {
  const registered = el("registered-reference");
  const role = el("reference-role");
  registered.disabled = !!plannedGeneration;
  role.disabled = !!plannedGeneration || !registered.value;
  const firstFrame = plannedGeneration?.locked || (!role.disabled && role.value === "first");
  el("free-references").disabled = firstFrame;
  el("video-audio").disabled = firstFrame;
}

async function loadProject() {
  closeReturnReview();
  const project = state.project;
  const reply = await request(api + "/timeline?" + query());
  if (state.project !== project) return;
  state.revision = reply.revision;
  state.edit = reply.timeline;
  state.dirty = false;
  state.jobs = [];
  state.selected = null;
  el("legacy-canvas").hidden = true;
  renderJobs();
  renderCut();
  await Promise.all([refreshJobs(), refreshReferences(project), refreshDeliveries(project), refreshLegacyCanvas(project)]);
  if (project === state.project && !state.selected) {
    const saved = lastReview(project);
    if (saved) {
      let job = state.jobs.find(item => item.id === saved.job_id);
      if (!job) {
        try { job = (await request(api + "/jobs/" + encodeURIComponent(saved.job_id) + "?" + query(project))).job; }
        catch { /* Deleted or unavailable candidates do not block opening the project. */ }
      }
      if (project === state.project && !state.selected && job?.outputs.some(item => item.index === saved.file_index && item.available)) {
        if (!state.jobs.some(item => item.id === job.id)) state.jobs.push(job);
        state.selected = job.id;
        state.outputIndex = saved.file_index;
        renderJobs();
      }
    }
  }
  if (project === state.project) await projectPlan.initialize();
}

async function refreshBoard() {
  const project = state.project;
  await refreshJobs();
  if (project !== state.project || state.dirty || !state.edit) return;
  const version = state.editVersion;
  const reply = await request(api + "/timeline?" + query(project));
  if (project === state.project && !state.dirty && version === state.editVersion && reply.revision > state.revision) {
    state.revision = reply.revision;
    state.edit = reply.timeline;
    renderCut();
  }
}

function perform(action) {
  return async (event) => {
    try {
      await action(event);
    } catch (error) {
      message(error.message, true);
    }
  };
}

el("project-form").addEventListener("submit", perform(async (event) => {
  event.preventDefault();
  const next = el("project").value.trim();
  if (!next) return;
  if (state.dirty && !confirm("当前剪辑清单尚未保存。要离开这个项目吗？")) return;
  if (copyReview.unsaved() && !confirm("切换项目将放弃未保存的文案或审核意见。继续吗？")) return;
  if (deliveryReview.unsaved() && !confirm("切换项目将放弃未保存的字幕。继续吗？")) return;
  if (projectPlan.unsaved() && !confirm("切换项目将放弃未保存的项目编排。继续吗？")) return;
  state.project = next;
  copyReview.reset();
  deliveryReview.reset();
  projectPlan.reset();
  state.edit = null;
  state.revision = 0;
  state.dirty = false;
  state.jobs = [];
  state.references = [];
  state.selected = null;
  renderCut();
  renderJobs();
  el("register-delivery").reset();
  el("new-delivery").open = false;
  el("delivery-status").textContent = "";
  el("delivery-list").replaceChildren();
  el("registered-reference").value = "";
  el("reference-role").value = "reference";
  updateReferenceRole();
  el("export-revision").value = "";
  if (plannedGeneration) el("generate").reset();
  clearPlannedGeneration();
  const url = new URL(location.href);
  url.searchParams.set("project", next);
  history.replaceState(null, "", url);
  message("");
  await loadProject();
}));
el("refresh").addEventListener("click", perform(() => Promise.all([refreshBoard(), refreshReferences(), refreshDeliveries()])));

function append(kind, job = currentJob(), output = currentOutput(job)) {
  if (!output || !state.edit) return;
  let track = state.edit.tracks.find((row) => row.kind === kind);
  if (!track) {
    track = { kind, name: kind === "Video" ? "画面" : "声音", clips: [] };
    state.edit.tracks.push(track);
  }
  const take = kind === "Video" ? job.info.preflight?.take : null;
  const clip = {
    job_id: job.id, file_index: output.index, start_frame: take?.start_frame ?? 0,
    duration_frames: take ? take.end_frame - take.start_frame
      : (kind === "Audio" ? job.info.audio_frames ?? job.info.frames : job.info.frames) || 24,
    rate: String(job.info.fps || state.edit.rate),
  };
  track.clips.push(clip);
  markDirty();
  renderCut();
}
el("append-picture").addEventListener("click", () => append("Video"));
el("append-audio").addEventListener("click", () => append("Audio"));
el("check-audio").addEventListener("click", async () => {
  const selection = audioSelection;
  if (!selection || selection.checking) return;
  selection.checking = selection.requested = true;
  el("check-audio").disabled = true;
  el("audio-result").textContent = "正在测量音频…";
  try {
    const { report } = await request(selection.url, { method: "POST" });
    if (audioSelection === selection) renderAudioReport(report);
  } catch (error) {
    if (audioSelection === selection) el("audio-result").textContent = error.message;
  } finally {
    selection.checking = false;
    if (audioSelection === selection) el("check-audio").disabled = false;
  }
});
el("cancel").addEventListener("click", perform(async () => {
  const job = currentJob();
  const reply = await send(api + "/jobs/" + job.id + "/cancel", "POST", {});
  message(reply.job.cancel_requested ? "已向 ComfyUI 请求取消。" : statuses[reply.job.state]);
  await refreshJobs();
}));

el("save").addEventListener("click", perform(async () => {
  const project = state.project;
  const version = state.editVersion;
  const reply = await send(api + "/timeline?" + query(), "PUT", {
    expected_revision: state.revision, timeline: state.edit,
  });
  if (state.project !== project) return;
  state.revision = reply.revision;
  if (version === state.editVersion) {
    state.edit = reply.timeline;
    state.dirty = false;
  }
  renderCut();
  message("剪辑清单已保存。");
}));

el("export").addEventListener("click", perform(async () => {
  const field = el("export-revision");
  if (state.revision > 1 && field.value && !field.reportValidity()) return;
  const selected = state.revision > 1 && field.value ? Number(field.value) : state.revision;
  await downloadCut(state.project, selected, "localhub-v" + selected);
}));

async function downloadCut(project, selected, filename) {
  const qs = query(project);
  qs.set("revision", selected);
  qs.set("target", "resolve");
  const response = await fetch(api + "/timeline/otio?" + qs);
  if (!response.ok) throw new Error((await response.json()).error);
  const url = URL.createObjectURL(await response.blob());
  const download = node("a");
  download.href = url;
  download.download = filename + ".otio";
  download.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function loadPlanCut(params) {
  if (!state.edit) throw new Error("请先等项目的剪辑清单读完");
  if (state.dirty && !confirm("用编排生成的草稿替换未保存的剪辑？")) return;
  const version = state.editVersion;
  const result = await request(api + "/plan/cut?" + params);
  if (result.project !== state.project || version !== state.editVersion) return;
  state.edit = result.timeline;
  markDirty();
  renderCut();
  el("cut-title").scrollIntoView({ block: "start" });
  message("已按编排 v" + result.plan_revision + " 起剪辑草稿。" +
    (result.unselected.length ? " " + result.unselected.length + " 个未选镜头保留空隙。" : "") + "确认后保存清单。");
}

let deliveryRead = 0;
async function refreshDeliveries(project = state.project) {
  const attempt = ++deliveryRead;
  const { deliveries } = await request(api + "/deliveries?" + query(project));
  if (attempt !== deliveryRead || project !== state.project) return;
  const list = el("delivery-list");
  list.replaceChildren();
  if (!deliveries.length) list.append(node("p", "还没有登记交付规格。", "quiet"));
  for (const item of deliveries) {
    const row = node("div", null, "delivery-row");
    const title = node("div");
    title.append(node("strong", item.name), node("span", [item.market, item.language].join(" / "), "quiet"));
    const version = "v" + item.revision;
    const filename = [item.name, item.market, item.language, item.aspect.replace(":", "x")]
      .map((part) => part.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").slice(0, 30)).join("-");
    const download = button("导出 OTIO", perform(() => downloadCut(project, item.revision, filename + "-" + version)));
    download.setAttribute("aria-label", [item.name, item.market, item.language, item.aspect, version, "导出 OTIO"].join(" "));
    const review = button("核对交付", () => deliveryReview.open(project, item, filename + "-" + version, review));
    review.setAttribute("aria-label", [item.name, item.market, item.language, item.aspect, version, "核对交付"].join(" "));
    review.setAttribute("aria-controls", "delivery-review");
    deliveryReview.associate(review, item);
    const actions = node("div", null, "inline wrap delivery-actions");
    actions.append(download, review);
    const [width, height] = item.aspect.split(":").map(Number);
    const orientation = width === height ? "方形" : width > height ? "横屏" : "竖屏";
    row.append(title, node("span", orientation + " " + item.aspect), node("span", version, "edition"), actions);
    list.append(row);
  }
}

el("new-delivery").addEventListener("toggle", () => {
  const field = el("register-delivery").elements.revision;
  if (el("new-delivery").open && !field.value && state.revision) field.value = state.revision;
});
el("register-delivery").addEventListener("submit", async (event) => {
  event.preventDefault();
  const project = state.project;
  const fields = Object.fromEntries(new FormData(event.currentTarget));
  fields.revision = Number(fields.revision);
  const status = el("delivery-status");
  try {
    await send(api + "/deliveries?" + query(project), "POST", fields);
    if (project !== state.project) return;
    await refreshDeliveries(project);
    if (project !== state.project) return;
    status.dataset.error = "false";
    status.textContent = fields.name + " 已登记到 v" + fields.revision;
  } catch (error) {
    if (project !== state.project) return;
    status.dataset.error = "true";
    status.textContent = error.message;
  }
});

let returnFile = null;
let checkedReturn = null;
let returnRequest = 0;
const sourceEvidence = new Map();

function resetReturnResult() {
  returnRequest++;
  checkedReturn = null;
  el("return-selected").hidden = true;
  el("return-apply").hidden = true;
  el("return-apply").textContent = "将整份回传载入草稿";
  el("return-changes").replaceChildren();
  el("return-status").textContent = "";
}

function closeReturnReview() {
  resetReturnResult();
  returnFile = null;
  el("return-review").hidden = true;
}

el("otio").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  resetReturnResult();
  returnFile = file;
  sourceEvidence.clear();
  el("return-manifest").value = "";
  el("return-file").textContent = file.name + " · 当前剪版 v" + state.revision;
  const base = el("return-base");
  base.min = state.revision ? 1 : 0;
  base.max = state.revision;
  base.value = state.revision <= 1 ? state.revision : "";
  base.disabled = state.revision <= 1;
  el("return-review").hidden = false;
  (base.disabled ? el("return-check") : base).focus();
});

el("return-close").addEventListener("click", () => {
  closeReturnReview();
  el("otio").focus();
});
function resetReturnSources() {
  sourceEvidence.clear();
  resetReturnResult();
}
el("return-base").addEventListener("input", resetReturnSources);
el("return-manifest").addEventListener("change", resetReturnSources);

function showReturnSources(sources, changes) {
  for (const [url, result] of Object.entries(sources)) {
    const section = node("section", null, "track");
    section.append(node("h4", "回传素材 · " + url.split(/[\\/]/).pop()));
    const location = node("details");
    location.append(node("summary", "查看回传文件路径"), node("p", url, "quiet"));
    section.append(location);
    const label = node("label", "选择这条引用对应的回传媒体 ");
    const file = node("input");
    file.type = "file";
    file.accept = ".mp4,.mov,.wav";
    label.append(file);
    section.append(label);
    const serial = returnRequest;
    file.addEventListener("change", async () => {
      const received = file.files[0];
      if (!received || serial !== returnRequest) return;
      resetReturnResult();
      const active = returnRequest;
      el("return-status").textContent = "正在核对 " + received.name + " 的文件内容…";
      try {
        const proof = await request(api + "/timeline/source-fingerprint", {method: "POST", body: received});
        if (active !== returnRequest) return;
        sourceEvidence.set(url, {bytes: proof.bytes, sha256: proof.sha256, name: received.name});
        el("return-check").click();
      } catch (error) {
        if (active === returnRequest) el("return-status").textContent = error.message;
      }
    });
    const proof = sourceEvidence.get(url);
    if (result.verified_content) {
      const count = result.choices.length;
      section.append(node("p", proof.name + (count ? " · 内容相同的候选 " + count + " 个" : " · 项目中没有内容相同的在线候选")));
      if (result.needs_choice) {
        if (count === 1) section.append(node("p", "当前无法核实原交付记录，可选择内容一致的另一份记录。"));
        const pickLabel = node("label", "沿用哪份候选记录 ");
        const pick = node("select");
        pick.append(new Option("请选择候选", ""));
        for (const [i, c] of result.choices.entries()) {
          const ambiguous = result.choices.some(other => other !== c && other.shot === c.shot && other.file === c.file);
          const option = new Option(c.shot + " · " + c.file + (ambiguous ? " · " + c.job_id + "/" + c.file_index : "") +
            (c.delivered ? " · 交付清单内" : ""), String(i));
          option.selected = c.job_id === result.selected?.job_id && c.file_index === result.selected?.file_index;
          pick.append(option);
        }
        pickLabel.append(pick);
        section.append(pickLabel);
        pick.addEventListener("change", () => {
          const c = result.choices[pick.value];
          sourceEvidence.set(url, {...proof, choice: c ? [c.job_id, c.file_index] : undefined});
          el("return-check").click();
        });
      }
      if (result.selected) {
        const match = result.choices.find(c => c.job_id === result.selected.job_id && c.file_index === result.selected.file_index);
        section.append(node("p", "已对应「" + match.shot + "」" + (match.delivered ? " · 已核对交付清单" : " · 已核对项目素材")));
      }
    }
    changes.append(section);
  }
}

function returnClipLabel(clip) {
  if (clip.gap) return "空隙 " + clip.duration_frames + " 帧";
  const job = state.jobs.find(item => item.id === clip.job_id);
  const name = clip.unresolved !== undefined
    ? clip.name + "（" + clip.reason + " · " + clip.unresolved + "）" : cutSourceLabel(job, clip.job_id, clip.file_index);
  return name + " · 源入点 " + clip.start_frame + "，取 " + clip.duration_frames + " 帧 / " + clip.rate + " fps";
}

function showReturnChanges(reply) {
  const changes = el("return-changes");
  showReturnSources(reply.source_review, changes);
  const picks = [];
  function offer(target, pick, label, after) {
    if (!pick) return;
    if (pick.blocked) {
      target.append(node("p", pick.blocked, "quiet"));
      return;
    }
    if (pick.conflict) {
      const local = pick.local.map(returnClipLabel).join("；") || "此处为空";
      target.append(node("p", "本机草稿也改了此处 · " + local));
    }
    const input = node("input");
    input.type = "checkbox";
    const control = node("label", null, "inline");
    control.append(input, document.createTextNode(pick.conflict ? label + "（覆盖本机此处）" : label));
    target.append(control);
    picks.push({ input, pick, after });
    input.addEventListener("change", () => {
      el("return-selected").disabled = !picks.some(option => option.input.checked);
    });
  }
  if (reply.partial_error) changes.append(node("p", reply.partial_error));
  if (reply.base_rate !== reply.rate) changes.append(node("p", "剪辑帧率 " + reply.base_rate + " → " + reply.rate));
  for (const track of reply.changes) {
    const section = node("section", null, "track");
    const lane = (track.kind === "Video" ? "画面轨 " : "声音轨 ") + track.index;
    const names = track.before_name === track.after_name ? track.after_name :
      (track.before_name || "新增") + " → " + (track.after_name || "移除");
    section.append(node("h4", lane + " · " + names));
    for (const range of track.ranges) {
      for (const [label, clips, start] of [["交出", range.before, range.before_at], ["回传", range.after, range.after_at]]) {
        clips.forEach((clip, i) => section.append(node("p", label + "第 " + (start + i + 1) + " 段 · " + returnClipLabel(clip))));
      }
      offer(section, range.pick, "采用" + lane + " · 第 " + (range.before_at + 1) + " 段起的改动", range.after);
    }
    offer(section, track.pick, track.before_name ? "采用移除轨道「" + track.before_name + "」" :
      "在现有" + (track.kind === "Video" ? "画面轨上方" : "声音轨之后") + "加入「" + track.after_name + "」");
    changes.append(section);
  }
  el("return-selected").hidden = !picks.length;
  el("return-selected").disabled = true;
  return picks;
}

el("return-check").addEventListener("click", async () => {
  resetReturnResult();
  if (!returnFile) return;
  const base = el("return-base");
  if (!Number.isInteger(base.valueAsNumber) || !base.validity.valid) {
    el("return-status").textContent = "请填写上次实际交出的剪版号。";
    base.focus();
    return;
  }
  const serial = returnRequest;
  const project = state.project;
  const baseRevision = base.valueAsNumber;
  const edit = state.edit, version = state.editVersion, revision = state.revision;
  const draft = state.dirty ? structuredClone(edit) : undefined;
  el("return-status").textContent = "正在核对素材与切点…";
  try {
    const manifestFile = el("return-manifest").files[0];
    const manifest = manifestFile ? JSON.parse(await manifestFile.text()) : undefined;
    const reply = await send(api + "/timeline/otio?" + query(project), "POST",
      { preview: true, base_revision: baseRevision, draft, otio: await returnFile.text(),
        manifest, source_evidence: Object.fromEntries(sourceEvidence) });
    if (serial !== returnRequest || project !== state.project) return;
    reply.options = showReturnChanges(reply);
    reply.draftSnapshot = { edit, version, revision };
    const same = !reply.changes.length && reply.base_rate === reply.rate;
    el("return-status").textContent = (same ? "与交出的 v" + baseRevision + " 一致。" :
      "相对交出的 v" + baseRevision + "，有 " + reply.changes.length + " 条轨道有改动。") +
      " 当前已保存 v" + reply.revision + "。" +
      (reply.current_differs ? " 制作台在交出后也有改动。载入整份回传会换掉当前剪辑内容。" : "") +
      (reply.timeline ? "" : " 请先核对未匹配素材，登记或重连后再次对照。");
    checkedReturn = reply;
    el("return-apply").hidden = !reply.timeline;
    if (!reply.timeline) el("return-changes").append(button("登记本机素材", () => {
      const form = el("register-media");
      form.closest("details").open = true;
      form.elements.shot.focus();
    }));
  } catch (error) {
    if (serial === returnRequest && project === state.project) {
      for (const proof of sourceEvidence.values()) delete proof.choice;
      el("return-status").textContent = error.message;
    }
  }
});

el("return-selected").addEventListener("click", () => {
  const reply = checkedReturn;
  if (!reply) return;
  const snapshot = reply.draftSnapshot;
  if (reply.revision !== state.revision || snapshot.revision !== state.revision ||
      snapshot.version !== state.editVersion || snapshot.edit !== state.edit) {
    el("return-status").textContent = "剪版或草稿在核对后已有变化，请重新对照再选择。";
    return;
  }
  const selected = reply.options.filter(option => option.input.checked);
  if (!selected.length) return;
  const draft = structuredClone(state.edit);
  // Existing-track indices and range offsets refer to the same draft snapshot.
  // Apply from the end so earlier changes cannot shift the next target.
  for (const { pick, after } of [...selected].reverse().filter(o => o.pick.op !== "add")
      .sort((a, b) => b.pick.track_index - a.pick.track_index || b.pick.at - a.pick.at)) {
    if (pick.op === "remove") draft.tracks.splice(pick.track_index, 1);
    else {
      const track = draft.tracks[pick.track_index];
      track.clips = track.clips.slice(0, pick.at).concat(after, track.clips.slice(pick.at + pick.count));
    }
  }
  for (const { pick } of selected.filter(o => o.pick.op === "add")) {
    const last = draft.tracks.findLastIndex(track => track.kind === pick.track.kind);
    const at = last >= 0 ? last + 1 : pick.track.kind === "Video" ? 0 : draft.tracks.length;
    draft.tracks.splice(at, 0, pick.track);
  }
  state.edit = draft;
  closeReturnReview();
  markDirty();
  renderCut();
  message("已将 " + selected.length + " 处回传改动加入草稿。保存清单后生成新版本。");
  el("save").focus();
});

el("return-apply").addEventListener("click", () => {
  const reply = checkedReturn;
  if (!reply?.timeline) return;
  if (reply.revision !== state.revision) {
    el("return-status").textContent = "当前剪版已变化，请重新读取并对照。";
    return;
  }
  if (state.dirty && reply.confirmVersion !== state.editVersion) {
    reply.confirmVersion = state.editVersion;
    el("return-apply").textContent = "确认替换未保存草稿";
    el("return-status").textContent = "当前草稿有未保存修改。可关闭核对保留草稿，或确认使用整份回传。";
    return;
  }
  state.edit = reply.timeline;
  closeReturnReview();
  markDirty();
  renderCut();
  message("回传已载入草稿。复核后可保存为新版本。");
  el("save").focus();
});

el("register-media").addEventListener("submit", perform(async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const fields = new FormData(form);
  const project = state.project;
  const reply = await send(api + "/media", "POST", { project, shot: fields.get("shot"), file: fields.get("file") });
  if (project !== state.project) return;
  state.selected = reply.job.id;
  state.outputIndex = 0;
  form.reset();
  message("素材已登记，可以加入剪辑。");
  await refreshJobs();
}));

el("register-reference").addEventListener("submit", perform(async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const project = state.project;
  const formData = new FormData(form);
  const { reference } = await send(api + "/references", "POST", {
    project, asset_id: formData.get("asset_id"), file: formData.get("file"),
  });
  if (state.project !== project) return;
  await refreshReferences(project);
  if (state.project !== project) return;
  el("registered-reference").value = JSON.stringify([reference.asset_id, reference.revision]);
  updateReferenceRole();
  resetPreflight();
  message("参考图已登记 · " + reference.asset_id + " v" + reference.revision);
}));

el("aspect").addEventListener("change", (event) => {
  if (event.target.value) {
    const [width, height] = event.target.value.split("x");
    const fields = el("generate").elements;
    fields.namedItem("width").value = width;
    fields.namedItem("height").value = height;
  }
  resetPreflight();
});
el("registered-reference").addEventListener("change", updateReferenceRole);
el("reference-role").addEventListener("change", updateReferenceRole);

let plannedGeneration = null;

function preparePlannedGeneration(revision, rate, shot) {
  const form = el("generate");
  if (form.elements.prompt.value && !confirm("用所选编排镜头替换当前生成草稿？")) return;
  const [numerator, denominator = 1] = String(rate).split("/");
  const fields = form.elements;
  for (const key of ["ref_images", "ref_videos", "ref_audios", "control_video", "take_start", "take_end"]) fields[key].value = "";
  fields.control_strength.value = fields.control_strength.defaultValue;
  el("registered-reference").value = "";
  el("reference-role").value = "reference";
  fields.shot.value = shot.title;
  fields.prompt.value = shot.prompt || shot.action;
  fields.duration_s.value = Number((shot.frames * Number(denominator) / Number(numerator)).toFixed(5));
  plannedGeneration = { revision, shot_id: shot.id, locked: shot.references.some(ref => ref.use === "first") };
  for (const key of ["shot", "prompt", "duration_s"]) fields[key].readOnly = true;
  el("planned-generation").hidden = false;
  el("planned-generation-label").textContent = shot.title + " · 编排 v" + revision;
  updateReferenceRole();
  resetPreflight();
  form.closest("details").open = true;
  el("preflight").focus();
}

function clearPlannedGeneration() {
  plannedGeneration = null;
  el("planned-generation").hidden = true;
  for (const key of ["shot", "prompt", "duration_s"]) el("generate").elements[key].readOnly = false;
  updateReferenceRole();
  resetPreflight();
}

el("clear-planned-generation").addEventListener("click", clearPlannedGeneration);

function generationDraft(form) {
  const fields = new FormData(form);
  const lines = (name) => (fields.get(name) || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  const params = {
    prompt: fields.get("prompt"), width: Number(fields.get("width")), height: Number(fields.get("height")),
    duration_s: Number(fields.get("duration_s")), seed: null,
    ref_images: lines("ref_images"), ref_audios: lines("ref_audios"),
    ref_videos: lines("ref_videos").map((path) => ({ path, include_audio: fields.has("video_audio") })),
  };
  if (fields.get("registered_reference")) {
    const [asset_id, revision] = JSON.parse(fields.get("registered_reference"));
    const reference = { asset_id, revision };
    if (fields.get("reference_role") === "first") {
      params.mode = "i2v";
      params.first_frame = reference;
    } else {
      params.ref_images.unshift(reference);
    }
  }
  const controlVideo = fields.get("control_video").trim();
  if (controlVideo) {
    params.control_video = controlVideo;
    params.control_strength = Number(fields.get("control_strength") || 1);
  }
  params.mode ??= params.ref_images.length || params.ref_audios.length || params.ref_videos.length ? "r2v" : "t2v";
  const body = { project: state.project, shot: fields.get("shot"), kind: "video", params };
  if (plannedGeneration) body.plan_shot = { revision: plannedGeneration.revision, shot_id: plannedGeneration.shot_id };
  if (fields.get("take_start") !== "" || fields.get("take_end") !== "") {
    body.take = { start_frame: Number(fields.get("take_start")) };
    if (fields.get("take_end") !== "") body.take.end_frame = Number(fields.get("take_end"));
  }
  return body;
}

let approvedDraft;

function resetPreflight() {
  approvedDraft = null;
  el("preflight-result").replaceChildren();
  el("confirm-generate").disabled = true;
}

async function displayReferenceEnd(figure, png) {
  try {
    const bytes = Uint8Array.from(atob(png), char => char.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    try {
      const canvas = node("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", figure.querySelector("figcaption").textContent);
      canvas.getContext("2d").drawImage(bitmap, 0, 0);
      figure.prepend(canvas);
    } finally { bitmap.close(); }
  } catch {
    figure.append(node("p", "末帧预览解码失败。"));
  }
}

function renderPlan(info, previews = []) {
  const { take, warnings } = info.preflight;
  const panel = el("preflight-result");
  panel.replaceChildren(node("p",
    info.mode.toUpperCase() + " · " + info.width + " × " + info.height + " · " + info.fps + " fps · "
    + info.frames + " 帧（" + info.duration_s + " 秒） · 取用 [" + take.start_frame + ", " + take.end_frame + ")"));
  panel.append(node("p", [info.models.unet, info.models.lora].filter(Boolean).join(" · "), "quiet"));
  if (info.planned_shot) panel.append(node("p", "项目编排 v" + info.planned_shot.revision, "quiet"));
  if (info.references.length) {
    const refs = node("ul");
    for (const ref of info.references) {
      const label = ref.version ? ref.version.asset_id + " v" + ref.version.revision + " · " + ref.source : ref.source;
      refs.append(node("li", ref.tag + " · " + label + (ref.role === "soundtrack" ? "（视频原声）" : "")));
    }
    panel.append(refs);
  }
  for (const warning of warnings) panel.append(node("p", warning));
  function showEnd(span, summary) {
    const figure = node("figure");
    figure.append(node("figcaption", summary));
    panel.append(figure);
    const preview = previews.find(item => item.tag === span.tag);
    if (preview) displayReferenceEnd(figure, preview.png);
  }
  for (const span of info.preflight.video_ranges || []) {
    const dropped = span.prepared_frames - span.end_frame;
    showEnd(span, span.tag + " · 转为 24 fps 并限长后共 " + span.prepared_frames + " 帧，画面采用第 0–"
      + span.last_frame + " 帧" + (dropped ? "，末尾 " + dropped + " 帧未采用。" : "。"));
  }
  const control = info.preflight.control_range;
  if (control) {
    let summary = "控制片共 " + control.source_frames + " 帧，按原帧顺序采用第 0–" + control.last_frame + " 帧。";
    if (control.repeated_frames) summary += "末帧再保持 " + control.repeated_frames + " 帧。";
    if (control.dropped_frames) summary += "末尾 " + control.dropped_frames + " 帧超出生成时长。";
    showEnd(control, summary + "图为最后采用的原片画面，模型会按镜头比例居中裁切。");
  }
}

el("generate").addEventListener("input", (event) => {
  if (["width", "height"].includes(event.target.name)) el("aspect").value = "";
  resetPreflight();
});
el("generate").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = generationDraft(form);
  const draft = JSON.stringify(body);
  const pending = JSON.parse(sessionStorage.getItem("localhub.pending") || "null");
  if (pending?.draft === draft && pending.body) body.params.seed = pending.body.params.seed;
  const preview = el("preflight");
  resetPreflight();
  preview.disabled = true;
  el("preflight-result").textContent = "正在检查镜头和参考素材…";
  try {
    const { info, reference_previews } = await send(api + "/video/preflight", "POST",
      { ...body, preview_reference_ends: true });
    if (draft !== JSON.stringify(generationDraft(form))) return;
    body.params.seed = info.seed;
    body.take = info.preflight.take;
    body.plan_sha256 = info.preflight.plan_sha256;
    approvedDraft = { body, draft };
    renderPlan(info, reference_previews);
    el("confirm-generate").disabled = false;
  } catch (error) {
    if (draft === JSON.stringify(generationDraft(form))) el("preflight-result").textContent = error.message;
  } finally {
    preview.disabled = false;
  }
});

el("confirm-generate").addEventListener("click", async () => {
  const form = el("generate");
  if (!approvedDraft || approvedDraft.draft !== JSON.stringify(generationDraft(form))) {
    resetPreflight();
    return;
  }
  const { body, draft } = approvedDraft;
  const pending = JSON.parse(sessionStorage.getItem("localhub.pending") || "null");
  // The same checked draft can be retried after a lost reply or a page reload.
  const key = pending?.draft === draft && pending.body?.plan_sha256 === body.plan_sha256
    ? pending.key : crypto.randomUUID();
  sessionStorage.setItem("localhub.pending", JSON.stringify({ draft, key, body }));
  const submit = el("confirm-generate");
  const preview = el("preflight");
  submit.disabled = preview.disabled = true;
  try {
    const reply = await send(api + "/jobs", "POST", body, { "Idempotency-Key": key });
    sessionStorage.removeItem("localhub.pending");
    if (state.project !== body.project) return;
    state.selected = reply.job.id;
    state.outputIndex = 0;
    if (draft === JSON.stringify(generationDraft(form))) {
      form.reset();
      clearPlannedGeneration();
    }
    resetPreflight();
    message("已接收生成任务。");
    await refreshJobs();
  } catch (error) {
    if (draft !== JSON.stringify(generationDraft(form))) return;
    if (error.status === 409) {
      sessionStorage.removeItem("localhub.pending");
      resetPreflight();
    }
    el("preflight-result").textContent = error.message;
  } finally {
    preview.disabled = false;
    submit.disabled = !approvedDraft;
  }
});

async function readSiteProject(signal) {
  const project = state.project;
  const qs = query(project);
  const [jobs, edit] = await Promise.all([
    request(api + "/jobs?" + qs, { signal }),
    request(api + "/timeline?" + qs, { signal }),
  ]);
  if (project !== state.project) throw new Error("当前页面已切换项目");
  return {
    project, revision: edit.revision, timeline: edit.timeline,
    jobs: jobs.jobs.map(({ id, shot, kind, source, state, delivery, outputs, info }) =>
      ({ id, shot, kind, source, state, delivery, outputs, frames: info.frames, fps: info.fps })),
  };
}

async function saveSiteCut({ project, expected_revision, timeline }, signal) {
  if (project !== state.project) throw new Error("当前页面已切换项目");
  if (!state.edit) throw new Error("请等项目加载完成");
  if (state.dirty) throw new Error("页面有待保存的剪辑修改");
  const reply = await send(api + "/timeline?" + query(project), "PUT", { expected_revision, timeline }, {}, signal);
  if (project === state.project && !state.dirty && reply.revision >= state.revision) {
    state.revision = reply.revision;
    state.edit = reply.timeline;
    renderCut();
    message("已通过站点工具保存剪辑清单。");
  }
  return { project, revision: reply.revision };
}

async function readSitePlan({ revision }, signal) {
  const project = state.project;
  const params = query(project);
  if (revision !== undefined) params.set("revision", revision);
  const result = await request(api + "/plan?" + params, { signal });
  if (project !== state.project) throw new Error("当前页面已切换项目");
  return result;
}

async function saveSitePlan({ project, expected_revision, plan }, signal) {
  if (project !== state.project) throw new Error("当前页面已切换项目");
  if (projectPlan.unsaved()) throw new Error("页面有待保存的编排修改");
  const result = await send(api + "/plan?" + query(project), "PUT", { expected_revision, plan }, {}, signal);
  if (project === state.project) projectPlan.showSaved(result);
  return result;
}

async function preflightSiteShot({ revision, shot_id, params = {} }, signal) {
  const project = state.project;
  const result = await send(api + "/video/preflight", "POST", { project, plan_shot: { revision, shot_id }, params }, {}, signal);
  if (project !== state.project) throw new Error("当前页面已切换项目");
  return result;
}

async function submitSiteShot({ submission }, signal) {
  const project = state.project;
  if (submission?.body?.project !== project || !submission.body.plan_shot || typeof submission.key !== "string") {
    throw new Error("请使用当前项目的镜头预检回执");
  }
  const reply = await send(api + "/jobs", "POST", submission.body, { "Idempotency-Key": submission.key }, signal);
  if (project === state.project) {
    state.selected = reply.job.id;
    state.outputIndex = 0;
    refreshJobs().catch(error => message(error.message, true));
  }
  return reply;
}

async function readSiteJob({ job_id }, signal) {
  const project = state.project;
  const result = await request(api + "/jobs/" + encodeURIComponent(job_id) + "?" + query(project), { signal });
  if (project !== state.project) throw new Error("当前页面已切换项目");
  return result;
}

async function startStudio() {
  const { generation } = await request(api + "/studio/status");
  state.generation = generation;
  el("review-mode").hidden = generation;
  el("generation-panel").hidden = !generation;
  const callbacks = { read: readSiteProject, save: saveSiteCut, readPlan: readSitePlan, savePlan: saveSitePlan,
    readJob: readSiteJob, readMasterReview: deliveryReview.readMasterReview,
    async readFrameNotes({ job_id, file_index }, signal) {
      const project = state.project;
      const result = await request(api + "/jobs/" + encodeURIComponent(job_id) + "/media/" +
        encodeURIComponent(file_index) + "/frame-notes?" + query(project), { signal });
      if (project !== state.project) throw new Error("当前页面已切换项目");
      return { project, job_id, file_index, ...result };
    } };
  if (generation) Object.assign(callbacks, { preflightShot: preflightSiteShot, submitShot: submitSiteShot });
  const siteStatus = el("site-tools");
  registerSiteTools(document, navigator, callbacks).then((registered) => {
    if (!registered) return;
    siteStatus.textContent = "本页已注册站点工具";
    el("connections").hidden = false;
  }).catch((error) => {
    siteStatus.textContent = "站点工具未能完整注册";
    el("connections").hidden = false;
    console.warn("Could not register Local Hub site tools", error);
  });
  setInterval(() => {
    if (!document.hidden) refreshBoard().catch(() => {});
  }, 5000);
  await loadProject();
}

perform(startStudio)();
