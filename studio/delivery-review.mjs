export function initDeliveryReview({ el, node, request, send, jobs }) {
  const panel = el("delivery-review");
  const subtitles = el("delivery-subtitles");
  const cues = el("subtitle-cues");
  const version = el("subtitle-version");
  const srt = el("download-subtitles");
  const masters = el("delivery-master");
  const options = el("handoff-options");
  const videoFinishing = el("video-finishing");
  const includeSubtitles = el("pack-subtitles");
  const api = "/localhub/v2/deliveries/";
  let active, sheet, checked;
  let approved = [], dirty = false, loading = false, checkId = 0;

  function status(id, message, error = false) {
    const field = el(id);
    field.textContent = message;
    field.dataset.error = String(error);
  }

  function field(label, control) {
    const wrapper = node("label", label);
    wrapper.append(control);
    return wrapper;
  }

  function key(name, revision) {
    return JSON.stringify([name, revision]);
  }

  function query(project, delivery, revision) {
    const value = new URLSearchParams({ project, ...delivery });
    if (revision) value.set("subtitle_revision", revision);
    return value;
  }

  function cueRow(cue = {}) {
    const line = node("div", null, "subtitle-row");
    const copy = node("select");
    copy.name = "copy";
    copy.required = true;
    copy.append(new Option("选择已通过文案", ""));
    for (const entry of approved) copy.append(new Option(entry.name + " v" + entry.revision, key(entry.name, entry.revision)));
    copy.value = cue.name ? key(cue.name, cue.revision) : "";
    const text = node("p", "", "quiet subtitle-text");
    const update = () => {
      const entry = approved.find(row => key(row.name, row.revision) === copy.value);
      text.textContent = entry ? entry.wording + "\n适用范围　" + entry.scope : "";
    };
    copy.addEventListener("change", update);
    update();
    const start = node("input");
    const end = node("input");
    for (const [input, name, value, min] of [[start, "start", cue.start_frame, 0], [end, "end", cue.end_frame, 1]]) {
      Object.assign(input, { type: "number", name, value: value ?? "", required: true, min, max: sheet.frames, step: 1 });
    }
    const remove = node("button", "移除");
    remove.type = "button";
    remove.addEventListener("click", () => {
      line.remove();
      el("add-subtitle").focus();
      changeDraft();
    });
    line.append(field("文案版本", copy), field("起始帧", start), field("结束帧", end), remove, text);
    return line;
  }

  function controls() {
    const latest = sheet && sheet.revision === sheet.latest_revision;
    version.disabled = loading || !sheet?.latest_revision;
    el("refresh-subtitles").disabled = loading;
    const editable = !loading && latest;
    el("add-subtitle").disabled = !editable || !approved.length;
    el("save-subtitles").disabled = !editable || !dirty;
    for (const input of cues.querySelectorAll("input,select,button")) input.disabled = !editable;
    srt.hidden = loading || dirty || !sheet?.cues.length;
    if (!srt.hidden) {
      srt.href = api + "subtitles.srt?" + query(active.project, active.delivery, sheet.revision);
      srt.download = active.filename + "-字幕v" + sheet.revision + ".srt";
    }
    includeSubtitles.disabled = srt.hidden;
    if (includeSubtitles.disabled) includeSubtitles.checked = false;
    el("pack-subtitle-label").textContent = srt.hidden ? "附上已保存字幕" : "附上字幕 v" + sheet.revision;
  }

  function changeDraft() {
    dirty = true;
    status("subtitle-status", "字幕有未保存的修改。");
    controls();
  }

  function display(value) {
    sheet = value;
    dirty = false;
    version.replaceChildren();
    if (!value.latest_revision) version.add(new Option("未保存", "0"));
    for (let id = value.latest_revision; id >= 1; id--) {
      version.add(new Option("v" + id + (id === value.latest_revision ? "（最新）" : ""), id));
    }
    version.value = String(value.revision);
    el("subtitle-timing").textContent = `${value.frames} 帧 · ${value.rate} fps · 从 0 计数，结束帧不含在字幕内。`;
    cues.replaceChildren(...value.cues.map(cueRow));
  }

  async function load(revision, input, refreshCopy = false) {
    const context = active;
    loading = true;
    controls();
    status("subtitle-status", input ? "正在保存字幕…" : "正在读取字幕…");
    try {
      if (refreshCopy) {
        const reply = await request("/localhub/v2/ad-copy?" + new URLSearchParams({ project: context.project }));
        if (active !== context) return;
        approved = reply.entries.filter(entry => entry.decision === "approved");
      }
      const url = api + "subtitles?" + query(context.project, context.delivery, revision);
      const result = input
        ? await send(url, "PUT", input)
        : await request(url);
      if (active !== context) return;
      display(result.sheet);
      status("subtitle-status", input ? "已保存字幕 v" + sheet.revision
        : !approved.length ? "先在广告文案审核中通过需要上屏的文字。" : "");
    } catch (error) {
      if (active === context) {
        version.value = String(sheet?.revision ?? 0);
        status("subtitle-status", error.message, true);
      }
    } finally {
      if (active === context) {
        loading = false;
        controls();
      }
    }
  }

  function syncMasters() {
    if (!active) return;
    const field = masters.elements.master;
    const previous = field.value;
    const candidates = jobs().filter(job => job.kind === "video" && job.state === "completed")
      .flatMap(job => job.outputs.filter(out => out.available && /\.(mov|mp4)$/i.test(out.name))
        .map(out => [job.shot + " · " + out.name, key(job.id, out.index)]));
    const signature = JSON.stringify(candidates);
    if (field.dataset.choices === signature) return;
    field.dataset.choices = signature;
    field.replaceChildren(new Option(candidates.length ? "选择已登记的成片" : "先登记成片文件", ""));
    for (const [label, value] of candidates) field.add(new Option(label, value));
    if (candidates.some(([, value]) => value === previous)) field.value = previous;
    else if (previous) invalidateMaster();
  }

  function invalidateMaster() {
    checked = null;
    checkId++;
    options.hidden = true;
    el("check-master").disabled = false;
    status("master-status", "");
    const dimensions = masters.elements.profile.value !== "cut";
    el("master-dimensions").hidden = !dimensions;
    for (const name of ["width", "height"]) {
      masters.elements[name].required = dimensions;
      masters.elements[name].disabled = !dimensions;
    }
  }

  function displaySources(sources) {
    el("video-source-review").hidden = !sources.length;
    videoFinishing.replaceChildren();
    for (const source of sources) {
      const row = node("div", null, "finishing-row");
      const details = node("div");
      const size = source.raster ? source.raster.width + " × " + source.raster.height : "读不到源片分辨率";
      details.append(node("strong", source.shot), node("p", size + " · " + source.filename, "quiet"));
      row.append(details);
      if (source.raster) {
        const method = node("select");
        method.dataset.source = key(source.job_id, source.file_index);
        method.append(new Option("未登记", ""), new Option("常规缩放", "resize"),
          new Option("Resolve Super Scale 2×", "resolve_super_scale_2x"));
        method.setAttribute("aria-label", source.shot + " " + source.filename + " 放大方式");
        row.append(method);
      }
      videoFinishing.append(row);
    }
  }

  function unsaved() { return dirty; }

  function associate(trigger, delivery) {
    const current = active && Object.keys(delivery).every(key => active.delivery[key] === delivery[key]);
    trigger.setAttribute("aria-expanded", current ? "true" : "false");
    if (current) active.trigger = trigger;
  }

  function reset() {
    const previous = active?.trigger;
    previous?.setAttribute("aria-expanded", "false");
    active = sheet = undefined;
    dirty = loading = false;
    approved = [];
    panel.hidden = true;
    return previous;
  }

  async function open(project, delivery, filename, trigger) {
    if (active?.trigger === trigger) {
      close();
      return;
    }
    if (unsaved() && !confirm("放弃当前未保存的字幕修改？")) return;
    reset();
    const context = { project, delivery, filename, trigger };
    active = context;
    trigger.setAttribute("aria-expanded", "true");
    panel.hidden = false;
    el("delivery-review-title").textContent = delivery.name;
    el("delivery-review-target").textContent = [delivery.market, delivery.language, delivery.aspect, "剪辑 v" + delivery.revision].join(" · ");
    el("delivery-review-title").focus();
    cues.replaceChildren();
    version.replaceChildren();
    el("subtitle-timing").textContent = "";
    masters.reset();
    invalidateMaster();
    syncMasters();
    controls();
    await load(null, null, true);
  }

  function close() {
    if (unsaved() && !confirm("放弃当前未保存的字幕修改？")) return;
    reset()?.focus();
  }

  el("close-delivery-review").addEventListener("click", close);
  el("refresh-subtitles").addEventListener("click", () => {
    if (!unsaved() || confirm("重读会丢弃未保存的字幕修改。继续吗？")) load(null, null, true);
  });
  el("add-subtitle").addEventListener("click", () => {
    const added = cueRow();
    cues.append(added);
    added.querySelector("select").focus();
    changeDraft();
  });
  cues.addEventListener("input", changeDraft);
  version.addEventListener("change", () => {
    if (unsaved() && !confirm("放弃当前未保存的字幕修改？")) {
      version.value = String(sheet.revision);
      return;
    }
    load(Number(version.value));
  });
  subtitles.addEventListener("submit", event => {
    event.preventDefault();
    if (!active || loading || !dirty || sheet.revision !== sheet.latest_revision) return;
    const rows = [...cues.children].map(row => {
      const [name, revision] = JSON.parse(row.querySelector("select").value);
      const [start, end] = row.querySelectorAll("input");
      return { name, revision, start_frame: start.valueAsNumber, end_frame: end.valueAsNumber };
    });
    load(null, { delivery: active.delivery, expected_revision: sheet.revision, cues: rows });
  });
  masters.addEventListener("input", event => {
    if (event.target.closest("#handoff-options")) return;
    invalidateMaster();
  });
  masters.addEventListener("submit", async event => {
    event.preventDefault();
    invalidateMaster();
    const context = active;
    const id = checkId;
    const form = masters.elements;
    const [job_id, file_index] = JSON.parse(form.master.value);
    const spec = { project: context.project, delivery: context.delivery, master: { job_id, file_index } };
    if (form.profile.value !== "cut") {
      spec.master_profile = { width: form.width.valueAsNumber, height: form.height.valueAsNumber };
      if (form.profile.value === "avc") Object.assign(spec.master_profile, {
        video_codec: "h264", pixel_format: "yuv420p", audio_codec: "aac", audio_sample_rate: 48000, audio_channels: 2,
      });
    }
    el("check-master").disabled = true;
    status("master-status", "正在核对母版…");
    try {
      const { master, video_sources } = await send(api + "master-check?" + new URLSearchParams({ project: context.project }), "POST", spec);
      if (active !== context || id !== checkId) return;
      checked = spec;
      status("master-status", `规格吻合 · ${master.width} × ${master.height} · ${master.fps} fps · ${master.frames} 帧`);
      displaySources(video_sources || []);
      options.hidden = false;
    } catch (error) {
      if (active === context && id === checkId) status("master-status", error.message, true);
    } finally {
      if (active === context && id === checkId) el("check-master").disabled = false;
    }
  });
  el("download-handoff").addEventListener("click", () => {
    if (!checked) return;
    const spec = { ...checked, credits: masters.elements.credits.value.split("\n").map(line => line.trim()).filter(Boolean) };
    const processing = Array.from(videoFinishing.querySelectorAll("select")).filter(select => select.value).map(select => {
      const [job_id, file_index] = JSON.parse(select.dataset.source);
      return { job_id, file_index, method: select.value };
    });
    if (processing.length) spec.video_finishing = processing;
    if (includeSubtitles.checked && !includeSubtitles.disabled) spec.subtitle_revision = sheet.revision;
    const url = URL.createObjectURL(new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" }));
    const link = node("a");
    link.href = url;
    link.download = active.filename + "-打包配置.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  return { open, reset, unsaved, syncMasters, associate };
}
