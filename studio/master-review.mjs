export function initMasterReview({ el, node, send }) {
  const panel = el("master-review"), canvas = el("mr-picture"), pen = canvas.getContext("2d");
  const position = el("mr-frame"), placement = el("mr-placement"), note = el("mr-note");
  const drafts = new Map();
  let spec, binding, selected, picture, region, anchor, cursor, generation = 0;
  let busy = false;
  const status = text => { el("mr-status").textContent = text; };
  const draftKey = () => JSON.stringify([binding, selected]);

  function stash() {
    if (picture && (note.value || region || anchor)) drafts.set(draftKey(), {text:note.value, region, anchor, cursor});
    else if (picture) drafts.delete(draftKey());
  }
  function controls() {
    el("mr-save").disabled = busy || !picture || !!anchor || !note.value.trim() || position.valueAsNumber !== selected;
    el("mr-clear").hidden = !region && !anchor;
  }
  function clearFrame() {
    picture?.close(); picture = null;
    selected = region = anchor = null; cursor = [.5,.5];
    canvas.hidden = true; note.value = ""; note.disabled = true; controls();
  }
  function reset() {
    stash(); generation++; spec = binding = null; busy = false; clearFrame();
    panel.hidden = true; panel.open = false; el("mr-history").replaceChildren();
  }
  function attach(value, filename) {
    reset(); spec = value; position.value = 0; placement.value = "original";
    placement.options[1].disabled = value.delivery.aspect !== "9:16";
    el("mr-title").textContent = value.delivery.name + " · 剪版 v" + value.delivery.revision + " · " + filename;
    status(""); panel.hidden = false;
  }
  function call(action, extra = {}, signal) {
    return send("/localhub/v2/deliveries/master-review/" + action + "?" + new URLSearchParams({project:spec.project}),
      "POST", {delivery:spec.delivery, master:spec.master, placement:placement.value, binding, ...extra}, {}, signal)
      .then(result => result.review);
  }
  async function read(_args, signal) {
    if (!spec) throw new Error("请先选择交付并核对母版");
    const seq = generation, review = await call("read", {}, signal);
    if (seq !== generation) throw new Error("审片对象已切换");
    return {...review, displayed: picture && panel.open && binding === review.binding ? {frame:selected, region} : null};
  }
  function history(notes) {
    const list = el("mr-history"); list.replaceChildren();
    if (!notes.length) list.textContent = "这份母版在当前版式下暂无意见。";
    for (const item of notes) {
      const button = node("button", "第 " + item.frame + " 帧 · " + item.note);
      button.type = "button"; button.onclick = () => { position.value = item.frame; locate(item); };
      list.append(button);
    }
  }
  function box(coords, color, dash = []) {
    const [l,t,r,b] = coords, w = canvas.width, h = canvas.height;
    pen.setLineDash(dash);
    for (const [stroke,width] of [["#171717",5],[color,3]]) {
      pen.strokeStyle = stroke; pen.lineWidth = width; pen.strokeRect(l*w,t*h,(r-l)*w,(b-t)*h);
    }
    pen.setLineDash([]);
  }
  function draw() {
    if (!picture) return;
    pen.drawImage(picture,0,0);
    if (placement.value === "youtube-vertical-2026-11-20") box([48/1080,288/1920,888/1080,1248/1920],"#72e2d0",[12,6]);
    if (region || anchor) box(region || [...anchor,...cursor],"#ffce70");
    if (canvas === document.activeElement) {
      const x = cursor[0]*canvas.width, y = cursor[1]*canvas.height;
      pen.fillStyle = "#ffd47a"; pen.fillRect(x-8,y-2,17,4); pen.fillRect(x-2,y-8,4,17);
    }
  }
  async function locate(saved) {
    if (!spec || !position.reportValidity()) return;
    const frame = position.valueAsNumber, seq = ++generation;
    stash(); clearFrame(); binding = null; el("mr-history").replaceChildren();
    busy = true; status("正在从母版读取第 " + frame + " 帧…");
    try {
      const review = await call("read");
      if (seq !== generation) return;
      binding = review.binding; history(review.notes); position.max = review.frames-1;
      const still = await call("frame", {frame});
      if (seq !== generation) return;
      const bytes = Uint8Array.from(atob(still.png), c => c.charCodeAt(0));
      const bitmap = await createImageBitmap(new Blob([bytes], {type:"image/png"}));
      if (seq !== generation) { bitmap.close(); return; }
      selected = frame; picture = bitmap;
      const draft = drafts.get(draftKey());
      region = saved ? saved.region || null : draft?.region || null;
      anchor = saved ? null : draft?.anchor || null; cursor = draft?.cursor || [.5,.5];
      note.value = saved ? saved.note : draft?.text || ""; note.disabled = false;
      canvas.width = bitmap.width; canvas.height = bitmap.height; canvas.hidden = false; draw();
      status("成片第 " + selected + " 帧 · " + (saved ? "已定位到保存的意见。" : "可以在画面上框选两个对角点。"));
    } catch (error) { if (seq === generation) status(error.message); }
    finally { if (seq === generation) { busy = false; controls(); } }
  }
  function choose() {
    if (!picture) return;
    if (!anchor) { anchor = [...cursor]; region = null; status("请选择另一角。"); }
    else if (anchor[0] !== cursor[0] && anchor[1] !== cursor[1]) {
      region = [Math.min(anchor[0],cursor[0]), Math.min(anchor[1],cursor[1]),
        Math.max(anchor[0],cursor[0]), Math.max(anchor[1],cursor[1])];
      anchor = null; status("已框选第 " + selected + " 帧的区域。");
    }
    draw(); controls();
  }
  const clamp = v => Math.round(Math.min(1,Math.max(0,v))*1e6)/1e6;
  canvas.onclick = event => {
    const b = canvas.getBoundingClientRect();
    cursor = [clamp((event.clientX-b.left)/b.width),clamp((event.clientY-b.top)/b.height)]; choose();
  };
  canvas.onkeydown = event => {
    const dir = {ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];
    if (dir) {
      event.preventDefault(); const step = event.shiftKey ? .05 : .005;
      cursor = cursor.map((v,i) => clamp(v+dir[i]*step)); draw();
    } else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
  };
  canvas.onfocus = canvas.onblur = draw;
  el("mr-clear").onclick = () => { anchor = region = null; draw(); controls(); };
  el("mr-locate").onclick = () => locate();
  placement.onchange = () => locate();
  position.oninput = note.oninput = controls;
  panel.ontoggle = () => { if (panel.open && spec && !picture && !busy) locate(); };
  el("mr-save").onclick = async () => {
    if (el("mr-save").disabled) return;
    const seq = generation;
    const annotation = {frame:selected, note:note.value.trim(), ...(region ? {region} : {})};
    busy = true; controls();
    try {
      const result = await call("save",{annotation});
      if (seq !== generation) return;
      history(result.notes); status("第 " + selected + " 帧的意见已保存到项目。");
    } catch (error) { if (seq === generation) status(error.message); }
    finally { if (seq === generation) { busy = false; controls(); } }
  };
  return {attach, reset, read};
}
