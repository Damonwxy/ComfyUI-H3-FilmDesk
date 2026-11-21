// Region coordinates always refer to the uncropped candidate, including after a resize.
export function frameRegionEditor({ player, frame, fps, context, node, button, status }) {
  const host = node("div", null, "region-editor");
  const canvas = node("canvas");
  canvas.tabIndex = 0;
  canvas.hidden = true;
  canvas.setAttribute("aria-label", "区域选框。点击两个对角点，或用方向键移动并按回车确定各点");
  const help = node("span", null, "quiet");
  help.setAttribute("role", "status");
  const still = document.createElement("canvas");
  let captured = null, pending = null, rect = null, anchor = null, cursor = [.5, .5], registration;
  function metadata() {
    return { project: context.project, job_id: context.job_id, file_index: context.file_index,
      stamp: context.stamp, frame: captured, region: rect.join(",") };
  }
  const round = value => Math.round(Math.max(0, Math.min(1, value)) * 1e6) / 1e6;
  function draw() {
    if (captured === null) return;
    const c = canvas.getContext("2d"), w = canvas.width, h = canvas.height;
    c.drawImage(still, 0, 0);
    const box = rect || (anchor && [...anchor, ...cursor]);
    if (box) {
      for (const [color, width] of [["black", 4], ["white", 2]]) {
        c.strokeStyle = color; c.lineWidth = width;
        c.strokeRect(box[0]*w, box[1]*h, (box[2]-box[0])*w, (box[3]-box[1])*h);
      }
    }
    if (document.activeElement === canvas) {
      c.fillStyle = "#ffbf51";
      c.fillRect(cursor[0]*w-5, cursor[1]*h-1, 11, 3);
      c.fillRect(cursor[0]*w-1, cursor[1]*h-5, 3, 11);
    }
    registration?.invalidate();
  }
  function clear() {
    pending = captured = rect = anchor = null;
    cursor = [.5, .5]; canvas.hidden = true; help.textContent = ""; reset.hidden = true;
    annotate.hidden = true;
    registration?.dispose(); registration = null;
  }
  function hitTest({ clientX, clientY }) {
    if (!rect || captured === null) return null;
    const b = canvas.getBoundingClientRect();
    const [l,t,r,d] = rect;
    const x = b.left + l*b.width, y = b.top + t*b.height;
    const width = (r-l)*b.width, height = (d-t)*b.height;
    if (clientX < x || clientY < y || clientX > x+width || clientY > y+height) return null;
    return { id: JSON.stringify([context.job_id, context.file_index, context.stamp, captured, ...rect]),
      name: context.shot + " · 第 " + captured + " 帧选区", role: "video-region",
      metadata: metadata(), rect: { x,y,width,height } };
  }
  function capture() {
    if (!pending || !host.isConnected || player.seeking || player.readyState < 2) return;
    if (Math.floor(player.currentTime*fps) !== pending.frame) return;
    captured = pending.frame; rect = pending.region; pending = null;
    still.width = canvas.width = player.videoWidth;
    still.height = canvas.height = player.videoHeight;
    still.getContext("2d").drawImage(player, 0, 0);
    canvas.hidden = false;
    help.textContent = rect ? "已定位到保存的区域。" : "请选择区域的两个对角点。";
    registration = document.oai?.annotation?.registerSurface?.({ element: canvas, hitTest });
    annotate.hidden = !(rect && typeof document.oai?.annotation?.request === "function");
    draw();
  }
  function open(region = null) {
    clear();
    if (!frame.reportValidity()) return;
    player.pause();
    pending = { frame: frame.valueAsNumber, region };
    reset.hidden = false;
    help.textContent = "正在定位源帧…";
    player.currentTime = (pending.frame + .5) / fps;
    capture();
  }
  function choose() {
    if (!anchor) {
      anchor = [...cursor]; rect = null; help.textContent = "请选择另一角。";
    } else {
      const [x,y] = cursor, [a,b] = anchor;
      if (a === x || b === y) return;
      rect = [Math.min(a,x), Math.min(b,y), Math.max(a,x), Math.max(b,y)];
      anchor = null; help.textContent = "已框选，可保存批注。";
    }
    annotate.hidden = !(rect && typeof document.oai?.annotation?.request === "function");
    draw();
  }
  canvas.addEventListener("click", event => {
    const b = canvas.getBoundingClientRect();
    cursor = [round((event.clientX-b.left)/b.width), round((event.clientY-b.top)/b.height)];
    choose();
  });
  canvas.addEventListener("keydown", event => {
    const offsets = { ArrowRight:[1,0], ArrowLeft:[-1,0], ArrowUp:[0,-1], ArrowDown:[0,1] };
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
    if (offsets[event.key]) {
      event.preventDefault();
      cursor = cursor.map((v,i) => round(v + offsets[event.key][i]*(event.shiftKey ? .05 : .005)));
      draw();
    }
  });
  canvas.addEventListener("focus", draw); canvas.addEventListener("blur", draw);
  const reset = button("取消区域", clear); reset.hidden = true;
  const annotate = button("用 Codex 批注此区域", () => {
    const result = document.oai?.annotation?.request?.(canvas, {
      metadata: metadata(), initialComment: context.shot + " · 请核对第 " + captured + " 帧的选区。",
    });
    help.textContent = result?.accepted ? "浏览器已接收批注请求，请在编辑器中确认。"
      : "当前浏览器未接收批注请求，仍可保存到项目。";
  });
  annotate.hidden = true;
  const controls = node("div", null, "inline wrap");
  controls.append(button("标记此帧区域", () => open()), reset, annotate);
  host.append(controls, help, canvas);
  player.addEventListener("seeked", capture); player.addEventListener("loadeddata", capture);
  return { host, clear, open,
    read() {
      if (pending || (captured !== null && (!rect || frame.valueAsNumber !== captured))) {
        status.textContent = "请完成此帧的选区，或取消区域再保存。"; return false;
      }
      return rect ? { region: [...rect] } : {};
    },
    dispose() { clear(); player.removeEventListener("seeked", capture); player.removeEventListener("loadeddata", capture); }
  };
}
