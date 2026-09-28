/*
 * 浏览器端客户端：连 /ws，按事件重建界面。
 *
 * 服务端是唯一真相源——本文件不自己推导任何转录状态，只把收到的事件画出来。
 * 刷新页面靠 hello 事件里的完整快照重建，不会丢内容。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const shell = $("shell"), stage = $("stage"), list = $("list"), empty = $("empty");
  const interim = $("interim"), interimWrap = $("interimWrap");
  const timer = $("timer"), jumpBtn = $("jumpBtn");
  const stripView = $("stripView"), stripInner = $("stripInner");   // 外层滚动，内层负责居中
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // id → {el, tr}，只更新变化的那条，不整体重画
  const segs = new Map();
  let lastSession = null;
  let recording = false, startedAt = 0, tickId = null;
  let providers = [], config = {}, autoSave = false, saveInterval = 30;
  let ws = null, retryDelay = 500;

  const nearBottom = () => stage.scrollHeight - stage.scrollTop - stage.clientHeight < 60;
  const follow = (was) => { if (was) stage.scrollTop = stage.scrollHeight; };

  // 字幕条里存一份历史，鼠标滚轮往上翻；停在底部时才自动跟最新的那句，
  // 用户翻到一半不会被新字幕拽回去。只留最近 STRIP_KEEP 条，别让 DOM 无限涨。
  const STRIP_KEEP = 200;
  const stripNear = () => stripView.scrollHeight - stripView.scrollTop - stripView.clientHeight < 40;
  const stripFollow = (was) => { if (was) stripView.scrollTop = stripView.scrollHeight; };

  function toast(text) {
    const old = document.querySelector(".toast");
    if (old) old.remove();
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2200);
  }

  // ── 连接 ──────────────────────────────────────────────────────────────────
  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
      retryDelay = 500;
      interim.dataset.hint = "就绪";
    };
    ws.onmessage = (e) => {
      let event;
      try { event = JSON.parse(e.data); } catch (err) { return; }
      handle(event);
    };
    ws.onclose = () => {
      interim.dataset.hint = "与程序的连接已断开，正在重连…";
      setRecording(false);
      setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 8000);   // 退避，别把本机敲爆
    };
    ws.onerror = () => { try { ws.close(); } catch (err) {} };
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
    else toast("还没连上程序，请稍候");
  }

  // ── 事件分发 ──────────────────────────────────────────────────────────────
  function handle(ev) {
    switch (ev.type) {
      case "hello":        restore(ev.state); break;
      case "session":      addDivider(ev.id, ev.at); break;
      case "segment":      putSegment(ev); break;
      case "translation":  putTranslation(ev.id, ev.text, ev.done); break;
      case "error":        putError(ev.id, ev.message); break;
      case "interim":      setInterim(ev.text); break;
      case "level":        level.target = ev.v; break;
      case "status":       applyStatus(ev); break;
      case "translate":    setSwitch($("trBtn"), ev.on); break;
      case "auto_save":    autoSave = ev.on; saveInterval = ev.interval; setSwitch($("autoBtn"), ev.on); break;
      case "cleared":      wipe(); break;
      case "removed":      ev.ids.forEach(dropSegment); break;
      case "saved":        toast(`已记录 ${ev.count} 句到 RecordMemory.md`); break;
      case "save_error":   toast(ev.message); break;
      case "config":       config = ev; break;
      case "asr_error":    toast(ev.message); break;
      case "settings_saved": onSettingsSaved(ev); break;
      case "test_result":  onTestResult(ev); break;
    }
  }

  function restore(state) {
    wipe();
    providers = state.providers || [];
    config = state.config || {};
    autoSave = state.auto_save;
    saveInterval = state.save_interval;

    const sessions = new Map((state.sessions || []).map((s) => [s.id, s]));
    (state.segments || []).forEach((seg) => {
      if (seg.session_id !== lastSession && sessions.has(seg.session_id)) {
        addDivider(seg.session_id, sessions.get(seg.session_id).at);
      }
      putSegment(seg);
      if (seg.status === "done" || seg.status === "pending") {
        if (seg.translation) putTranslation(seg.id, seg.translation, seg.status === "done");
      } else if (seg.status === "error") {
        putError(seg.id, seg.error || "翻译失败");
      }
    });

    setInterim(state.interim || "");
    setSwitch($("trBtn"), state.translate);
    setSwitch($("autoBtn"), state.auto_save);
    $("srcSelect").value = state.source || "mic";
    applyStatus({ recording: state.recording, text: state.status });

    if (!state.asr_ready) {
      $("emptySub").textContent = state.asr_reason;
      toast(state.asr_reason);
    } else if (!config.ready) {
      $("emptySub").textContent = config.reason + "（可在设置里填写）";
    }
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────────
  function addDivider(id, at) {
    if (id === lastSession) return;
    lastSession = id;
    empty.hidden = true;
    const was = nearBottom();
    const div = document.createElement("div");
    div.className = "divider";
    const when = new Date((at || Date.now() / 1000) * 1000);
    div.innerHTML = `<span>${when.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</span>`;
    list.appendChild(div);
    follow(was);
  }

  function putSegment(data) {
    const existing = segs.get(data.id);
    if (existing) {                       // 重译：原地把译文换回等待态
      existing.el.querySelector(".tr")?.remove();
      existing.strip?.querySelector(".tr")?.remove();
      if (data.status === "pending") existing.el.appendChild(pendingLine());
      return;
    }

    const was = nearBottom();
    empty.hidden = true;

    const el = document.createElement("article");
    el.className = "seg";
    el.dataset.id = data.id;

    const src = document.createElement("p");
    src.className = "src";
    src.textContent = data.source;
    el.appendChild(src);

    const tools = document.createElement("div");
    tools.className = "tools";
    const copyBtn = document.createElement("button");
    copyBtn.textContent = "复制";
    copyBtn.onclick = () => copySeg(el);
    tools.appendChild(copyBtn);
    if (data.status !== "none" && data.status !== "skipped") {
      const again = document.createElement("button");
      again.textContent = "重译";
      again.onclick = () => send({ cmd: "retranslate", id: data.id });
      tools.appendChild(again);
    }
    el.appendChild(tools);

    if (data.status === "pending") {
      el.appendChild(pendingLine());
    } else if (data.status === "skipped") {
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = "中文原文 · 未翻译";
      el.appendChild(note);
    }

    list.appendChild(el);
    segs.set(data.id, { el, strip: stripAdd(data.source) });
    follow(was);
  }

  function stripAdd(source) {
    const was = stripNear();
    const item = document.createElement("div");
    item.className = "strip-item";
    const p = document.createElement("p");
    p.className = "src";
    p.textContent = source;
    item.appendChild(p);
    stripInner.appendChild(item);
    while (stripInner.children.length > STRIP_KEEP) stripInner.firstElementChild.remove();
    stripFollow(was);
    return item;
  }

  // 字幕条里的译文，跟着主界面那条一起变
  function stripTr(entry, text, cls) {
    if (!entry.strip) return;
    const was = stripNear();
    let tr = entry.strip.querySelector(".tr");
    if (!tr) { tr = document.createElement("p"); entry.strip.appendChild(tr); }
    tr.className = cls;
    tr.textContent = text;
    stripFollow(was);
  }

  function pendingLine() {
    const tr = document.createElement("p");
    tr.className = "tr pending";
    tr.innerHTML = "<i></i><i></i><i></i>";
    return tr;
  }

  function putTranslation(id, text, done) {
    const entry = segs.get(id);
    if (!entry) return;
    const was = nearBottom();
    let tr = entry.el.querySelector(".tr");
    if (!tr) { tr = document.createElement("p"); entry.el.appendChild(tr); }
    tr.className = done ? "tr" : "tr streaming";
    tr.textContent = text;
    follow(was);
    stripTr(entry, text, tr.className);
  }

  function putError(id, message) {
    const entry = segs.get(id);
    if (!entry) return;
    const was = nearBottom();
    let tr = entry.el.querySelector(".tr");
    if (!tr) { tr = document.createElement("p"); entry.el.appendChild(tr); }
    tr.className = "tr failed";
    tr.textContent = "";
    const btn = document.createElement("button");
    btn.className = "err-toggle";
    btn.textContent = "翻译失败";
    const detail = document.createElement("span");
    detail.className = "err-detail";
    detail.hidden = true;
    detail.textContent = message;
    btn.onclick = () => { detail.hidden = !detail.hidden; };
    tr.append(btn, detail);
    follow(was);
    stripTr(entry, "翻译失败", "tr failed");
  }

  function dropSegment(id) {
    const entry = segs.get(id);
    if (!entry) return;
    // 分句被移走后，它前面那条会话分隔线可能就空了
    const prev = entry.el.previousElementSibling;
    entry.el.remove();
    entry.strip?.remove();
    segs.delete(id);
    if (prev && prev.classList.contains("divider") &&
        (!prev.nextElementSibling || prev.nextElementSibling.classList.contains("divider"))) {
      prev.remove();
    }
    if (!list.children.length) { empty.hidden = false; lastSession = null; }
  }

  function wipe() {
    list.innerHTML = "";
    stripInner.innerHTML = "";
    segs.clear();
    lastSession = null;
    empty.hidden = false;
  }

  function setInterim(text) {
    interim.textContent = text;
    interim.classList.toggle("live", recording);
    // 超过可见行数时始终贴着最后一行，光标不会滚出视野
    interimWrap.scrollTop = interimWrap.scrollHeight;
    if (text) follow(true);
  }

  function copySeg(el) {
    const src = el.querySelector(".src").textContent;
    const tr = el.querySelector(".tr:not(.failed):not(.pending)");
    const text = tr ? src + "\n" + tr.textContent : src;
    const done = () => toast("已复制");
    try { navigator.clipboard.writeText(text).then(done, done); } catch (e) { done(); }
  }

  // ── 录音状态 ──────────────────────────────────────────────────────────────
  function applyStatus(ev) {
    if (ev.source) $("srcSelect").value = ev.source;
    setRecording(!!ev.recording);
    if (ev.text) interim.dataset.hint = ev.text;
  }

  function setRecording(on) {
    if (on === recording) return;
    recording = on;
    shell.classList.toggle("rec-on", on);
    $("recLabel").textContent = on ? "停止录音" : "开始录音";
    interim.classList.toggle("live", on);
    clearInterval(tickId); tickId = null;
    if (on) {
      startedAt = Date.now();
      tick();
      tickId = setInterval(tick, 500);
    } else {
      level.target = 0;
    }
  }

  function tick() {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    timer.textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  $("recBtn").onclick = () => {
    if (recording) send({ cmd: "stop" });
    else send({ cmd: "start", source: $("srcSelect").value });
  };

  // ── 电平线 ────────────────────────────────────────────────────────────────
  // 服务端每 200ms 才推一个值，这里向目标值缓动，线条才不会一跳一跳
  const level = { target: 0, current: 0 };
  const cv = $("wave"), ctx = cv.getContext("2d");
  const wave = new Array(56).fill(0);

  function sizeCanvas() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const w = cv.clientWidth || 104, h = cv.clientHeight || 24;
    cv.width = w * dpr; cv.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function drawWave() {
    const w = cv.clientWidth || 104, h = cv.clientHeight || 24, mid = h / 2;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    ctx.lineWidth = 1.5; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    for (let i = 0; i < wave.length; i++) {
      const x = (i / (wave.length - 1)) * w;
      const y = mid - wave[i] * (mid - 2);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
  }
  let acc = 0, last = 0;
  function loop(t) {
    acc += t - (last || t); last = t;
    if (acc > 34) {
      acc = 0;
      level.current += (level.target - level.current) * 0.25;
      const amp = recording ? level.current : 0;
      wave.push(Math.max(-1, Math.min(1, amp * (Math.random() * 0.8 + 0.2) * (Math.random() < 0.5 ? -1 : 1))));
      wave.shift();
      drawWave();
    }
    requestAnimationFrame(loop);
  }
  sizeCanvas(); drawWave();
  if (!reduced) requestAnimationFrame(loop);
  addEventListener("resize", () => { sizeCanvas(); drawWave(); });

  // ── 顶栏控件 ──────────────────────────────────────────────────────────────
  const isOn = (btn) => btn.getAttribute("aria-pressed") === "true";
  const setSwitch = (btn, on) => btn.setAttribute("aria-pressed", String(!!on));

  $("srcSelect").onchange = (e) => send({ cmd: "source", source: e.target.value });
  $("trBtn").onclick = () => send({ cmd: "translate", on: !isOn($("trBtn")) });
  $("autoBtn").onclick = () => send({ cmd: "auto_save", on: !isOn($("autoBtn")), interval: saveInterval });
  $("clearBtn").onclick = () => send({ cmd: "clear" });

  function setStrip(on) {
    shell.classList.toggle("strip", on);
    $("stripBtn").textContent = on ? "退出字幕条" : "字幕条";
    if (on) stripView.scrollTop = stripView.scrollHeight;   // 进来先看最新的
  }

  // 字幕条模式下，滚轮落在框外（上方留白、底栏）也当成翻字幕
  shell.addEventListener("wheel", (e) => {
    if (!shell.classList.contains("strip") || stripView.contains(e.target)) return;
    stripView.scrollTop += e.deltaMode === 1 ? e.deltaY * 18 : e.deltaY;
  }, { passive: true });
  $("stripBtn").onclick = () => setStrip(!shell.classList.contains("strip"));
  $("exitStripBtn").onclick = () => setStrip(false);

  $("themeBtn").onclick = () => {
    const root = document.documentElement;
    const now = root.getAttribute("data-theme")
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = now === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) {}
    drawWave();
  };
  try {
    const saved = localStorage.getItem("theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) {}

  stage.addEventListener("scroll", () => { jumpBtn.hidden = nearBottom(); });
  jumpBtn.onclick = () => stage.scrollTo({ top: stage.scrollHeight, behavior: reduced ? "auto" : "smooth" });

  addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (shell.classList.contains("strip")) { setStrip(false); return; }
      closeDrawer();
    }
    if ((e.ctrlKey || e.metaKey) && (e.key === "=" || e.key === "+" || e.key === "-")) {
      e.preventDefault();
      const cur = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--scale")) || 1;
      const next = Math.max(0.8, Math.min(1.6, cur + (e.key === "-" ? -0.1 : 0.1)));
      document.documentElement.style.setProperty("--scale", next.toFixed(2));
      try { localStorage.setItem("scale", next.toFixed(2)); } catch (err) {}
      toast("字号 " + Math.round(next * 100) + "%");
    }
  });
  try {
    const scale = localStorage.getItem("scale");
    if (scale) document.documentElement.style.setProperty("--scale", scale);
  } catch (e) {}

  // ── 设置抽屉 ──────────────────────────────────────────────────────────────
  function openDrawer() {
    if (document.querySelector(".drawer")) return;

    const scrim = document.createElement("div");
    scrim.className = "scrim";
    scrim.onclick = closeDrawer;

    const d = document.createElement("aside");
    d.className = "drawer";
    d.innerHTML = `
      <div class="d-head">翻译设置</div>
      <div class="d-body">
        <div class="field">
          <label for="f-provider">服务商</label>
          <select id="f-provider">
            <option value="-1">自定义</option>
            ${providers.map((p, i) => `<option value="${i}">${p.name}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label for="f-url">接口地址</label>
          <input class="code" id="f-url" spellcheck="false">
        </div>
        <div class="field">
          <label for="f-key">API Key</label>
          <div class="key-row">
            <input class="code" id="f-key" type="password" spellcheck="false">
            <button class="btn" id="f-eye" type="button">显示</button>
          </div>
        </div>
        <div class="field">
          <label for="f-model">模型</label>
          <input class="code" id="f-model" spellcheck="false">
        </div>
        <div class="field">
          <label for="f-lang">目标语言</label>
          <input id="f-lang">
        </div>
        <div class="field">
          <label for="f-extra">附加参数（JSON）</label>
          <input class="code" id="f-extra" spellcheck="false">
        </div>
        <div class="row"><span>附带上文句数</span><input class="num" id="f-ctx" type="number" min="0" max="20"></div>
        <div class="d-rule"></div>
        <div class="row"><span>自动记录间隔（秒）</span><input class="num" id="f-int" type="number" min="5"></div>
        <p class="d-note">自动记录的开关在顶部工具栏。开启后每隔一段时间把已确定的文字连同译文追加写入 RecordMemory.md，并清空界面。</p>
      </div>
      <div class="d-foot">
        <button class="btn" id="f-test">测试翻译</button>
        <span class="d-msg" id="f-msg"></span>
        <button class="btn solid" id="f-save">保存</button>
      </div>`;

    document.body.append(scrim, d);

    d.querySelector("#f-url").value = config.base_url || "";
    d.querySelector("#f-key").value = config.api_key || "";
    d.querySelector("#f-model").value = config.model || "";
    d.querySelector("#f-lang").value = config.target_lang || "简体中文";
    d.querySelector("#f-extra").value = config.extra_body || "{}";
    d.querySelector("#f-ctx").value = config.context_size ?? 3;
    d.querySelector("#f-int").value = saveInterval;

    const msg = d.querySelector("#f-msg");
    const pick = d.querySelector("#f-provider");
    const match = providers.findIndex((p) => p.base_url === config.base_url);
    pick.value = String(match);

    pick.onchange = () => {
      const p = providers[Number(pick.value)];
      if (!p) return;
      d.querySelector("#f-url").value = p.base_url;
      d.querySelector("#f-model").value = p.model;
      d.querySelector("#f-extra").value = p.extra_body;
      msg.className = "d-msg";
      msg.textContent = p.model ? "" : "该服务商需自行填写模型名";
    };
    d.querySelector("#f-eye").onclick = (e) => {
      const input = d.querySelector("#f-key");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      e.target.textContent = show ? "隐藏" : "显示";
    };
    d.querySelector("#f-int").onchange = (e) => {
      saveInterval = Math.max(5, parseInt(e.target.value, 10) || 30);
      e.target.value = saveInterval;
      if (autoSave) send({ cmd: "auto_save", on: true, interval: saveInterval });
    };
    d.querySelector("#f-test").onclick = () => {
      msg.className = "d-msg"; msg.textContent = "测试中…";
      send({ cmd: "test_settings", ...collect(d) });
    };
    d.querySelector("#f-save").onclick = () => {
      msg.className = "d-msg"; msg.textContent = "保存中…";
      send({ cmd: "save_settings", ...collect(d) });
    };
    pick.focus();
  }

  function collect(d) {
    return {
      base_url: d.querySelector("#f-url").value.trim(),
      api_key: d.querySelector("#f-key").value.trim(),
      model: d.querySelector("#f-model").value.trim(),
      target_lang: d.querySelector("#f-lang").value.trim(),
      extra_body: d.querySelector("#f-extra").value.trim(),
      context_size: parseInt(d.querySelector("#f-ctx").value, 10) || 0,
    };
  }

  function drawerMsg() { return document.querySelector("#f-msg"); }

  function onSettingsSaved(ev) {
    const msg = drawerMsg();
    if (ev.ok) {
      closeDrawer();
      toast("已保存 · 写入程序目录的 .env");
    } else if (msg) {
      msg.className = "d-msg bad";
      msg.textContent = ev.message;
    }
  }

  function onTestResult(ev) {
    const msg = drawerMsg();
    if (!msg) return;
    if (ev.ok) {
      msg.className = "d-msg ok";
      msg.textContent = `“${ev.text}” · ${ev.seconds}s`;
    } else {
      msg.className = "d-msg bad";
      msg.textContent = ev.message;
    }
  }

  function closeDrawer() {
    document.querySelector(".drawer")?.remove();
    document.querySelector(".scrim")?.remove();
  }
  $("gearBtn").onclick = openDrawer;

  // 内容高度会在渲染之后继续变（网页字体分批加载、窗口缩放、流式文字换行数变化），
  // 每变一次就把停在底部的视图重新推到底；用户往上翻过就不再跟，免得被拽回去。
  function stickToBottom(scroller, content, near) {
    if (!window.ResizeObserver) return;
    let stick = true;
    scroller.addEventListener("scroll", () => { stick = near(); });
    new ResizeObserver(() => { if (stick) scroller.scrollTop = scroller.scrollHeight; }).observe(content);
  }
  stickToBottom(stage, $("column"), nearBottom);
  stickToBottom(stripView, stripInner, stripNear);

  connect();
})();
