const genres = [["ad", "广告"], ["film", "微电影"], ["series", "短剧"]];
const uses = [["reference", "多模态参考"], ["first", "锁首帧"], ["last", "锁尾帧"], ["review", "审片参考"]];

function rateNumber(value) {
  const [numerator, denominator = 1] = String(value).split("/");
  return Number(numerator) / Number(denominator);
}

function blankShot(sceneId, title, frames, action = "") {
  return { id: crypto.randomUUID(), scene_id: sceneId, title, frames, action,
    prompt: "", dialogue: "", entry: "", exit: "", references: [], picture: null, sound: null };
}

export function initProjectPlan({ el, node, request, send, project, jobs, references, refreshReferences, draftCut, prepare, canGenerate, exportCut }) {
  const panel = el("project-plan");
  const form = el("plan-editor");
  const history = el("plan-version");
  const navigation = el("plan-navigation");
  const work = el("plan-work");
  let saved, draft, sceneId, shotId, episodeId, busy = false, dirty = false, requestId = 0;
  const endpoint = "/localhub/v2/plan";
  const editable = () => !!saved && saved.revision === saved.latest_revision && !busy;
  const episodes = () => draft.episodes;
  const scenes = () => draft.scenes.filter(scene => scene.episode_id === episodeId);
  const shots = () => draft.shots.filter(shot => shot.scene_id === sceneId);
  const pickedCount = (items) => items.filter(shot => shot.picture).length;

  function feedback(text, error = false) {
    const status = el("plan-feedback");
    status.textContent = text;
    status.dataset.error = String(error);
  }

  function toolbar() {
    form.hidden = !draft;
    form.inert = busy;
    el("save-plan").disabled = !editable() || !dirty;
    el("reload-plan").disabled = busy;
    history.disabled = busy || !saved?.latest_revision;
    if (!draft) return;
    const label = genres.find(([key]) => key === draft.genre)?.[1] || "";
    const name = saved.revision ? "v" + saved.revision : "未保存";
    el("plan-summary").textContent = [label, name, draft.shots.length + " 镜"].join(" · ");
    const entry = el("plan-entry");
    if (entry) entry.textContent = draft.shots.length ? "继续编辑分镜" : "从创作简报开始";
    const starter = el("ad-starter");
    if (starter) {
      starter.hidden = !editable() || draft.genre !== "ad" ||
        !!(draft.episodes.length || draft.scenes.length || draft.shots.length);
      starter.disabled = starter.hidden;
    }
    for (const control of form.querySelectorAll("[data-saved-plan]")) {
      control.disabled = busy || dirty || !saved.revision || control.dataset.savedPlan === "unavailable";
    }
  }

  function changed() {
    dirty = true;
    feedback("编排有未保存的修改");
    toolbar();
  }

  function button(text, action, disabled = false) {
    const control = node("button", text);
    control.type = "button";
    control.disabled = disabled || busy;
    control.addEventListener("click", action);
    return control;
  }

  function label(text, control) {
    const wrapper = node("label", text);
    wrapper.append(control);
    return wrapper;
  }

  function field(labelText, row, key, { rows, number, max, required = false, onChange } = {}) {
    const input = node(rows ? "textarea" : "input");
    if (rows) input.rows = rows;
    else input.type = number ? "number" : "text";
    if (number) {
      input.min = key === "start_frame" ? 0 : 1;
      input.step = 1;
      if (max) input.max = max;
    } else if (max) input.maxLength = max;
    input.value = row[key] ?? "";
    input.required = required;
    input.readOnly = !editable();
    input.addEventListener("input", () => {
      row[key] = number ? (input.value === "" ? null : input.valueAsNumber) : input.value;
      changed();
    });
    if (onChange) input.addEventListener("change", onChange);
    return label(labelText, input);
  }

  function select(labelText, options, value, choose, disabled = false) {
    const control = node("select");
    control.disabled = disabled || busy;
    for (const [key, text] of options) control.add(new Option(text, key));
    control.value = value ?? "";
    control.addEventListener("change", () => choose(control.value));
    return label(labelText, control);
  }

  function buildMetadata() {
    const target = el("plan-metadata");
    const title = field("作品名称", draft, "title", { required: true, max: 120 });
    const genre = select("题材", genres, draft.genre, value => { draft.genre = value; changed(); renderWorkspace(); }, !editable());
    const rate = field("编排帧率", draft, "rate", { required: true, max: 24, onChange: updateShotLabels });
    const header = node("div", null, "plan-fields");
    header.append(title, genre, rate);
    const brief = field("创作简报", draft, "brief", { rows: 3, max: 15000 });
    const script = field("剧本原文", draft, "script", { rows: 5, max: 500000 });
    script.lastChild.id = "plan-script";
    target.replaceChildren(header, brief, buildAdStarter(), script);
    refreshStarterReferences();
    toolbar();
  }

  function refreshStarterReferences() {
    const picker = el("ad-starter-reference");
    if (!picker) return;
    const selected = picker.value;
    picker.replaceChildren(new Option("稍后选择", ""), ...references().map(ref =>
      new Option(ref.asset_id + " v" + ref.revision, JSON.stringify([ref.asset_id, ref.revision]))));
    if ([...picker.options].some(option => option.value === selected)) picker.value = selected;
    picker.parentElement.hidden = picker.options.length === 1;
  }

  function buildAdStarter() {
    const box = node("fieldset", null, "plan-brief");
    box.id = "ad-starter";
    const seconds = node("input");
    seconds.type = "number";
    seconds.min = "0";
    seconds.step = "any";
    seconds.value = "12";
    const goal = node("input");
    goal.type = "text";
    goal.placeholder = "例如，壶身上的容量刻度";
    goal.maxLength = 500;
    const picker = node("select");
    picker.id = "ad-starter-reference";
    const controls = node("div", null, "plan-fields");
    const image = node("input");
    image.type = "file";
    image.accept = ".png,.jpg,.jpeg,.webp";
    controls.append(label("从电脑选择商品图", image));
    controls.append(label("广告总时长（秒）", seconds), label("最后要看清什么", goal));
    controls.append(label("共用商品参考图", picker));
    const create = button("起两镜草稿", async () => {
      if (!editable() || draft.genre !== "ad" || draft.episodes.length || draft.scenes.length || draft.shots.length) return;
      if (!seconds.value || !seconds.reportValidity()) { seconds.focus(); return; }
      const fps = rateNumber(draft.rate);
      if (!Number.isFinite(fps) || fps < 1 || fps > 240) {
        feedback("请先填写有效的编排帧率。", true);
        return;
      }
      const frames = Math.round(seconds.valueAsNumber * fps);
      const opening = Math.max(1, Math.round(frames / 3));
      if (frames < 2 || Math.max(opening, frames - opening) > 100000) {
        feedback("每个镜头需要 1 至 100000 帧，请调整总时长。", true);
        seconds.focus();
        return;
      }
      const file = image.files[0];
      let reference = picker.value ? JSON.parse(picker.value) : null;
      if (file) {
        const context = project(), plan = draft;
        const params = new URLSearchParams({ project: context, filename: file.name,
          asset_id: file.name.replace(/\.[^.]*$/, "").trim().slice(0, 120) || "商品图" });
        box.disabled = true;
        create.disabled = true;
        feedback("正在登记商品图…");
        try {
          const reply = await request("/localhub/v2/references/upload?" + params, { method: "POST", body: file });
          await refreshReferences(context);
          if (context !== project() || plan !== draft || !box.isConnected || !editable() || box.hidden) return;
          reference = [reply.reference.asset_id, reply.reference.revision];
        } catch (error) {
          if (context === project() && plan === draft) feedback(error.message, true);
          return;
        } finally {
          if (box.isConnected) { box.disabled = box.hidden; create.disabled = false; }
        }
      }
      const scene = { id: crypto.randomUUID(), title: "商品展示", episode_id: null, summary: "", script_line: null };
      const detail = goal.value.trim();
      const pair = [blankShot(scene.id, "开头 · 商品入画", opening, "让商品进入画面，展示一次使用动作。"),
        blankShot(scene.id, "收尾 · 商品细节", frames - opening,
          detail ? "最后停在" + detail + "，保持清晰可读。" : "靠近商品，停在需要观众看清的细节。")];
      if (reference) {
        const [asset_id, revision] = reference;
        for (const shot of pair) shot.references.push({ asset_id, revision, role: "商品外观", use: "reference" });
      }
      draft.scenes.push(scene);
      draft.shots.push(...pair);
      sceneId = scene.id;
      shotId = pair[0].id;
      changed();
      renderWorkspace();
      el("plan-work").querySelector(".plan-shot-list button")?.focus();
      feedback("两镜草稿已就绪，可分别改动作和时长，再保存编排。");
    });
    image.addEventListener("change", () => { picker.disabled = !!image.files.length; });
    box.append(node("legend", "两镜广告草稿"), controls, create);
    return box;
  }

  function navButton(title, selected, click) {
    const control = button(title, click);
    control.setAttribute("aria-pressed", String(selected));
    return control;
  }

  function smallActions(text, records, scope, row, remove) {
    const index = scope.indexOf(row);
    const move = (direction) => {
      const a = records.indexOf(row);
      const b = records.indexOf(scope[index + direction]);
      [records[a], records[b]] = [records[b], records[a]];
      changed();
      renderWorkspace();
    };
    const bar = node("div", null, "inline wrap plan-row-actions");
    bar.append(button(text + "前移", () => move(-1), !editable() || index < 1),
      button(text + "后移", () => move(1), !editable() || index === scope.length - 1),
      button("移除" + text, remove, !editable()));
    return bar;
  }

  function renderNavigation() {
    const list = node("div", null, "plan-scene-list");
    for (const scene of scenes()) {
      const picked = draft.shots.filter(shot => shot.scene_id === scene.id);
      const control = navButton(scene.title || "未命名场次", scene.id === sceneId, () => {
        sceneId = scene.id;
        shotId = null;
        renderWorkspace();
      });
      control.append(node("span", pickedCount(picked) + "/" + picked.length + " 已选", "quiet"));
      list.append(control);
    }
    if (!scenes().length) list.append(node("p", "在这里建立场次，再添加镜头。", "quiet"));
    const title = node("h3", "场次");
    navigation.replaceChildren();
    if (episodes().length) {
      const options = [["", "未分集"], ...episodes().map(row => [row.id, row.title])];
      navigation.append(select("分集", options, episodeId, value => {
        episodeId = value || null;
        sceneId = shotId = null;
        renderWorkspace();
      }));
    }
    if (draft.genre === "series" || episodes().length) navigation.append(button("添加分集", () => {
      const row = { id: crypto.randomUUID(), title: "第 " + (episodes().length + 1) + " 集", summary: "", cut_revision: null };
      episodes().push(row);
      episodeId = row.id;
      sceneId = shotId = null;
      changed();
      renderWorkspace();
    }, !editable()));
    navigation.append(title, list, button("添加场次", () => {
      const row = { id: crypto.randomUUID(), title: "场次 " + (scenes().length + 1), episode_id: episodeId, summary: "", script_line: null };
      draft.scenes.push(row);
      sceneId = row.id;
      shotId = null;
      changed();
      renderWorkspace();
    }, !editable()));
  }

  function showScript(row) {
    const input = el("plan-script");
    if (!row.script_line || !input) return;
    const lines = input.value.split("\n");
    const first = lines.slice(0, row.script_line - 1).reduce((size, line) => size + line.length + 1, 0);
    el("plan-brief").open = true;
    input.focus();
    input.setSelectionRange(first, first + (lines[row.script_line - 1]?.length || 0));
  }

  function scriptAnchor(row, caption) {
    const box = node("div", null, "inline wrap plan-anchor");
    const go = button("定位剧本行", () => showScript(row), !row.script_line);
    box.append(field(caption, row, "script_line", { number: true, onChange: () => { go.disabled = !row.script_line; } }), go);
    return box;
  }

  function sourcePicker(shot, name, audio) {
    const box = node("div", null, "plan-pick");
    const candidates = [];
    for (const job of jobs()) {
      const video = job.kind === "video";
      const withAudio = ["audio", "music"].includes(job.kind) || (video && (job.info.has_audio ?? (job.source === "generated")));
      if (job.state !== "completed" || !(audio ? withAudio : video)) continue;
      for (const out of job.outputs) {
        if (out.available) candidates.push({ job, out, value: JSON.stringify([job.id, out.index]) });
      }
    }
    candidates.sort((a, b) => Number(b.job.info.planned_shot?.shot_id === shot.id) - Number(a.job.info.planned_shot?.shot_id === shot.id));
    const selection = shot[name];
    const key = selection ? JSON.stringify([selection.job_id, selection.file_index]) : "";
    const options = [["", audio ? "未选声音" : "未选画面"], ...candidates.map(({ job, out, value }) =>
      [value, job.shot + " · " + out.name])];
    if (selection && !options.some(([value]) => value === key)) options.push([key, "已选素材 · 暂不在候选列表"]);
    const chooser = select(audio ? "声音候选" : "画面候选", options, key, value => {
      const match = candidates.find(item => item.value === value);
      if (!value) shot[name] = null;
      if (match) {
        const info = match.job.info;
        const rate = String(info.fps || draft.rate || "24");
        const duration = Math.max(1, Math.round(shot.frames * rateNumber(rate) / rateNumber(draft.rate)) || 1);
        const start = info.preflight?.take?.start_frame ?? 0;
        const available = (audio ? info.audio_frames || info.frames : info.frames) || start + duration;
        const planned = info.preflight?.take?.end_frame ?? start + duration;
        shot[name] = { job_id: match.job.id, file_index: match.out.index, start_frame: start,
          duration_frames: Math.max(1, Math.min(available, planned) - start), rate };
      }
      changed();
      renderWorkspace();
    }, !editable());
    box.append(chooser);
    if (selection) {
      const fields = node("div", null, "plan-clip-fields");
      const prefix = audio ? "声音" : "画面";
      fields.append(field(prefix + "源入点", selection, "start_frame", { number: true, required: true }),
        field(prefix + "取用帧数", selection, "duration_frames", { number: true, required: true }),
        node("p", selection.rate + " fps", "quiet"));
      box.append(fields);
    }
    return box;
  }

  function referenceFields(shot) {
    const box = node("div", null, "plan-references");
    box.append(node("h4", "参考版本"));
    const indexed = references();
    for (const ref of shot.references) {
      const row = node("div", null, "plan-reference");
      const options = [["", "请选择参考图及版号"], ...indexed.map(item => [
        JSON.stringify([item.asset_id, item.revision]), item.asset_id + " v" + item.revision,
      ])];
      const key = ref.asset_id ? JSON.stringify([ref.asset_id, ref.revision]) : "";
      if (key && !options.some(([value]) => value === key)) options.push([key, ref.asset_id + " v" + ref.revision]);
      row.append(select("参考图版本", options, key, value => {
        [ref.asset_id, ref.revision] = value ? JSON.parse(value) : ["", 0];
        changed();
      }, !editable()), select("用法", uses, ref.use, value => { ref.use = value; changed(); }, !editable()),
      field("用途说明", ref, "role", { max: 120 }), button("移除参考", () => {
        shot.references.splice(shot.references.indexOf(ref), 1);
        changed();
        renderWorkspace();
      }, !editable()));
      box.append(row);
    }
    if (indexed.length || shot.references.length) box.append(button("添加参考图", () => {
      shot.references.push({ asset_id: "", revision: 0, role: "", use: "reference" });
      changed();
      renderWorkspace();
    }, !editable()));
    else box.append(node("p", "可先登记角色、商品或场景参考图。", "quiet"));
    return box;
  }

  function renderShot(shot) {
    const detail = node("div", null, "plan-shot-editor");
    const heading = node("div", null, "plan-fields");
    heading.append(field("镜头名称", shot, "title", { required: true, max: 120, onChange: updateShotLabels }),
      field("计划帧数", shot, "frames", { number: true, max: 100000, required: true, onChange: updateShotLabels }),
      scriptAnchor(shot, "对白或动作所在行"));
    const continuity = node("div", null, "plan-picks");
    continuity.append(field("入镜状态", shot, "entry", { rows: 2, max: 12000 }),
      field("出镜状态", shot, "exit", { rows: 2, max: 12000 }));
    const writing = node("details", null, "plan-brief");
    writing.append(node("summary", "对白、提示词与衔接状态"),
      field("对白", shot, "dialogue", { rows: 2, max: 12000 }),
      field("H3 提示词", shot, "prompt", { rows: 3, max: 12000 }), continuity);
    const selected = node("div", null, "plan-picks");
    selected.append(sourcePicker(shot, "picture", false), sourcePicker(shot, "sound", true));
    const actions = smallActions("镜头", draft.shots, shots(), shot, () => {
      draft.shots.splice(draft.shots.indexOf(shot), 1);
      shotId = null;
      changed();
      renderWorkspace();
    });
    if (canGenerate()) {
      const toPreflight = button("送到 H3 预检", () => prepare(saved.revision, draft.rate, shot),
        dirty || !saved.revision || !(shot.prompt || shot.action));
      toPreflight.dataset.savedPlan = shot.prompt || shot.action ? "ready" : "unavailable";
      actions.prepend(toPreflight);
    }
    detail.append(heading, field("动作与构图", shot, "action", { rows: 2, max: 12000 }),
      writing, referenceFields(shot), selected, actions);
    work.append(detail);
  }

  function updateShotLabels() {
    const byId = new Map(shots().map(shot => [shot.id, shot]));
    for (const card of work.querySelectorAll("[data-shot-tab]")) {
      const shot = byId.get(card.dataset.shotTab);
      if (!shot) continue;
      card.textContent = shot.title || "未命名镜头";
      const duration = shot.frames / rateNumber(draft.rate);
      if (duration > 0 && Number.isFinite(duration)) card.append(node("span", " · " + Number(duration.toFixed(2)) + " 秒", "quiet"));
    }
  }

  function renderWorkspace() {
    if (!draft) return;
    if (episodeId && !episodes().some(row => row.id === episodeId)) episodeId = null;
    sceneId = (scenes().find(row => row.id === sceneId) || scenes()[0])?.id;
    shotId = (shots().find(row => row.id === shotId) || shots()[0])?.id;
    renderNavigation();
    work.replaceChildren();
    const episode = episodes().find(row => row.id === episodeId);
    if (episode) {
      const group = node("details", null, "plan-brief");
      const fields = node("div", null, "plan-picks");
      fields.append(field("分集名称", episode, "title", { required: true, max: 120, onChange: renderNavigation }),
        field("已交接剪辑版号", episode, "cut_revision", { number: true }));
      const exportButton = button("导出该集已登记 OTIO", async () => {
        try { await exportCut(episode.cut_revision, episode.title); } catch (error) { feedback(error.message, true); }
      }, !episode.cut_revision || dirty);
      exportButton.dataset.savedPlan = episode.cut_revision ? "ready" : "unavailable";
      const episodeActions = smallActions("分集", episodes(), episodes(), episode, () => {
        if (scenes().length) {
          feedback("先将场次移出这个分集，再移除分集。", true);
          return;
        }
        episodes().splice(episodes().indexOf(episode), 1);
        episodeId = null;
        changed();
        renderWorkspace();
      });
      episodeActions.prepend(exportButton);
      group.append(node("summary", episode.title), fields, field("分集梗概", episode, "summary", { rows: 2, max: 4000 }), episodeActions);
      work.append(group);
    }
    const scene = scenes().find(row => row.id === sceneId);
    if (!scene) {
      work.append(node("p", "为这段故事或广告添加第一个场次。", "quiet"));
      return;
    }
    const header = node("div", null, "plan-section-head");
    const sceneFields = node("div", null, "plan-fields");
    sceneFields.append(field("场次名称", scene, "title", { required: true, max: 120, onChange: renderNavigation }));
    if (episodes().length) sceneFields.append(select("所属分集", [["", "未分集"], ...episodes().map(row => [row.id, row.title])],
      scene.episode_id, value => { scene.episode_id = episodeId = value || null; changed(); renderWorkspace(); }, !editable()));
    sceneFields.append(scriptAnchor(scene, "场次起始行"));
    header.append(sceneFields, field("场次说明", scene, "summary", { rows: 2, max: 4000 }),
      smallActions("场次", draft.scenes, scenes(), scene, () => {
        if (shots().length && !confirm("从当前草稿移除这个场次及其镜头？")) return;
        draft.scenes.splice(draft.scenes.indexOf(scene), 1);
        draft.shots = draft.shots.filter(shot => shot.scene_id !== sceneId);
        sceneId = shotId = null;
        changed();
        renderWorkspace();
      }));
    const shoot = node("div", null, "inline wrap plan-shot-list");
    for (const shot of shots()) {
      const card = navButton(shot.title || "未命名镜头", shot.id === shotId, () => {
        shotId = shot.id;
        renderWorkspace();
      });
      card.dataset.shotTab = shot.id;
      shoot.append(card);
    }
    shoot.append(button("添加镜头", () => {
      const shot = blankShot(sceneId, "镜头 " + (shots().length + 1), 72);
      draft.shots.push(shot);
      shotId = shot.id;
      changed();
      renderWorkspace();
    }, !editable()));
    work.append(header, shoot);
    updateShotLabels();
    const shot = shots().find(row => row.id === shotId);
    if (shot) renderShot(shot);
    const fromPlan = button(episodeId ? "起该集剪辑草稿" : "起剪辑草稿", async () => {
      if (dirty) return;
      const qs = new URLSearchParams({ project: project(), revision: saved.revision });
      if (episodeId) qs.set("episode", episodeId);
      try { await draftCut(qs); } catch (error) { feedback(error.message, true); }
    }, dirty || !saved.revision);
    fromPlan.dataset.savedPlan = "ready";
    work.append(fromPlan);
  }

  function display(value) {
    saved = value;
    draft = structuredClone(value.plan);
    dirty = false;
    if (!sceneId) episodeId = draft.episodes[0]?.id || null;
    history.replaceChildren();
    if (!value.latest_revision) history.add(new Option("尚未保存", ""));
    for (let rev = value.latest_revision; rev >= 1; rev--) {
      history.add(new Option("v" + rev + (rev === value.latest_revision ? "（最新）" : ""), rev));
    }
    history.value = value.revision || "";
    el("plan-brief").open = !draft.scenes.length || el("plan-brief").open;
    toolbar();
    buildMetadata();
    renderWorkspace();
  }

  async function load(revision, payload) {
    const context = project();
    const id = ++requestId;
    busy = true;
    toolbar();
    feedback(payload ? "正在保存编排…" : "正在读取编排…");
    const qs = new URLSearchParams({ project: context });
    if (revision) qs.set("revision", revision);
    try {
      const result = payload ? await send(endpoint + "?" + qs, "PUT", payload) : await request(endpoint + "?" + qs);
      if (id !== requestId || context !== project()) return;
      busy = false;
      display(result);
      feedback(payload ? "已保存编排 v" + result.revision
        : result.revision !== result.latest_revision ? "正在查看历史编排" : "");
    } catch (error) {
      if (id !== requestId || context !== project()) return;
      feedback(error.message, true);
      history.value = saved?.revision || "";
    } finally {
      if (id === requestId) {
        busy = false;
        toolbar();
      }
    }
  }

  panel.addEventListener("toggle", () => {
    if (!panel.open) return;
    if (!saved && !busy) load();
    else if (!busy) renderWorkspace();
  });
  el("reload-plan").addEventListener("click", () => {
    if (!dirty || confirm("放弃当前未保存的编排修改？")) load();
  });
  history.addEventListener("change", () => {
    if (dirty && !confirm("放弃当前未保存的编排修改？")) {
      history.value = saved.revision;
      return;
    }
    load(Number(history.value));
  });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (editable() && dirty) load(null, { expected_revision: saved.revision, plan: draft });
  });

  function reset() {
    requestId++;
    saved = draft = sceneId = episodeId = shotId = null;
    dirty = busy = false;
    panel.open = false;
    el("plan-summary").textContent = "";
    feedback("");
    history.replaceChildren();
    toolbar();
  }

  return { initialize: () => { if (!saved && !busy) return load(); }, reset, refreshStarterReferences,
    hasShots: () => !!draft?.shots.length, unsaved: () => dirty, showSaved: (value) => {
    if (!dirty && !busy && (!saved || saved.revision === saved.latest_revision)) display(value);
  } };
}
