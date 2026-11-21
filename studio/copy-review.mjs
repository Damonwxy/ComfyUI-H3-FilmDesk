export function initCopyReview({ el, node, request, send, project }) {
  const panel = el("copy-panel");
  const choice = el("copy-choice");
  const edit = el("copy-edit");
  const review = el("copy-review");
  const history = el("copy-history");
  const status = el("copy-status");
  const labels = { pending: "待审核", approved: "已通过", returned: "已退回" };
  const url = () => "/localhub/v2/ad-copy?" + new URLSearchParams({ project: project() });
  let entries = [];
  let active;
  let generation = 0;
  let busy = false;

  function card(row) {
    const block = node("article");
    block.append(node("strong", row.name + " v" + row.revision + " · " + labels[row.decision]),
      node("p", row.wording), node("p", "适用范围　" + row.scope), node("p", "资料依据　" + row.evidence));
    if (row.reviewer) block.append(node("p", "审核记录　" + row.reviewer + " / " + row.reason));
    return block;
  }
  function dirty() {
    return ["name", "wording", "scope", "evidence"].some(k => edit.elements[k].value !== (active?.[k] || ""));
  }
  function controls() {
    choice.disabled = busy;
    el("copy-refresh").disabled = busy;
    for (const field of [...edit.elements, ...review.elements]) field.disabled = busy;
    review.hidden = !active || active.decision !== "pending" || dirty();
  }
  function show() {
    const rows = entries.filter(r => r.name === choice.value);
    active = rows[0];
    const record = el("copy-record");
    record.replaceChildren();
    if (active) record.append(card(active));
    review.reset();
    for (const k of ["name", "wording", "scope", "evidence"]) edit.elements[k].value = active?.[k] || "";
    edit.elements.name.readOnly = !!active;
    history.hidden = rows.length < 2;
    history.open = false;
    history.querySelector("div").replaceChildren(...rows.slice(1).map(card));
    controls();
  }
  function unsaved() {
    return dirty() || review.elements.reviewer.value || review.elements.reason.value;
  }
  async function load(name, payload, method) {
    const token = ++generation;
    const path = url();
    busy = true;
    controls();
    try {
      const saved = payload ? await send(path, method, payload) : null;
      if (token !== generation) return;
      const result = await request(path);
      if (token !== generation) return;
      entries = result.entries;
      const names = [...new Set(entries.map(r => r.name))];
      choice.replaceChildren(new Option("新建文案", ""), ...names.map(n => new Option(n, n)));
      choice.value = names.includes(name) ? name : "";
      show();
      status.textContent = saved ? "已记录 " + name + " v" + saved.revision : "";
    } catch (error) {
      if (token === generation) status.textContent = error.message;
    } finally {
      if (token === generation) { busy = false; controls(); }
    }
  }
  let initialized = false;
  panel.addEventListener("toggle", () => {
    if (panel.open && !initialized) { initialized = true; load(""); }
  });
  choice.addEventListener("change", () => {
    if (unsaved() && !confirm("放弃尚未保存的文案或审核意见？")) { choice.value = active?.name || ""; return; }
    status.textContent = "";
    show();
  });
  edit.addEventListener("input", controls);
  el("copy-refresh").addEventListener("click", () => {
    if (!unsaved() || confirm("重读将替换当前输入，继续吗？")) load(choice.value);
  });
  edit.addEventListener("submit", event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(edit));
    load(data.name.trim(), { ...data, expected_revision: active?.revision || 0 }, "POST");
  });
  review.addEventListener("submit", event => {
    event.preventDefault();
    if (!active || dirty() || !event.submitter) return;
    const data = Object.fromEntries(new FormData(review));
    load(active.name, { ...data, name: active.name, expected_revision: active.revision, decision: event.submitter.value }, "PUT");
  });
  function reset() {
    generation++;
    initialized = busy = false;
    panel.open = false;
    entries = [];
    choice.replaceChildren(new Option("新建文案", ""));
    status.textContent = "";
    show();
  }
  reset();
  return { reset, unsaved };
}
