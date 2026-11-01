# overlay/overlay_page.py —— 悬浮窗页面（HTML 字符串） + 纯标准库 PNG 编码
# 铁律：零第三方依赖。PNG 用 zlib + struct + binascii 手工编码；不依赖 PIL/Pillow。

import struct
import zlib
import binascii
import html
import time


# ---------------------------------------------------------------------------
# 纯标准库 PNG 编码（RGB, 8bit, colortype=2）
# ---------------------------------------------------------------------------
def _crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


def _png_chunk(typ: bytes, data: bytes) -> bytes:
    chunk = typ + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", _crc32(chunk))


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """把 RGB 原始位图（长度 width*height*3）编码为合法 PNG 字节串。"""
    assert len(rgb) == width * height * 3, f"rgb 长度应为 {width*height*3}，实际 {len(rgb)}"
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # 每行前置 filter byte（0 = None）
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    idat = zlib.compress(bytes(raw), 9)
    return (sig + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b""))


def make_screen_png(scene: str | None = None, width: int = 320, height: int = 180) -> bytes:
    """合成模式占位"屏幕帧"：按场景着色的渐变 + 中心亮区（游戏视口）。
    真实模式若有 mss 帧，可把原始 RGB 直接喂 encode_png；此处合成兜底。"""
    if scene == "combat":
        base = (60, 20, 20)
    elif scene == "explore":
        base = (20, 40, 70)
    elif scene == "puzzle":
        base = (40, 40, 30)
    elif scene == "story":
        base = (50, 30, 60)
    else:
        base = (25, 25, 35)
    buf = bytearray()
    cw, ch = width // 2, height // 2
    half_w, half_h = width // 3, height // 3
    for y in range(height):
        for x in range(width):
            t = (x / width + y / height) / 2.0
            r = min(255, int(base[0] + t * 50))
            g = min(255, int(base[1] + t * 50))
            b = min(255, int(base[2] + t * 80))
            # 中心矩形（游戏视口）提亮
            if abs(x - cw) < half_w and abs(y - ch) < half_h:
                r = min(255, r + 45)
                g = min(255, g + 45)
                b = min(255, b + 45)
            buf += bytes((r, g, b))
    return encode_png(width, height, bytes(buf))


# ---------------------------------------------------------------------------
# 悬浮窗 HTML 页面（深色主题、中文、聊天+监控）
# embed=1 时隐藏"在浏览器打开"提示，交给 Electron 壳承载
# ---------------------------------------------------------------------------
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>元灵 MetaSpirit</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: #0e1116; color: #e6e6e6;
    font-family: "PingFang SC","Microsoft YaHei",system-ui,sans-serif; font-size: 13px; }
  #app { display: flex; flex-direction: column; height: 100vh; }
  .dragbar { height: 8px; -webkit-app-region: drag; cursor: move; background: #1b2230; }
  header { padding: 6px 10px; display: flex; align-items: center; gap: 8px;
    border-bottom: 1px solid #1f2733; }
  header .logo { font-weight: 700; color: #6cc6ff; }
  header .badge { font-size: 11px; padding: 1px 6px; border-radius: 10px;
    background: #16351f; color: #6ee787; }
  header .badge.off { background: #3a2326; color: #ff7b72; }
  header .spacer { flex: 1; }
  header button { background: #1b2230; color: #e6e6e6; border: 1px solid #2a3340;
    border-radius: 6px; padding: 2px 8px; cursor: pointer; }
  .status { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 10px;
    padding: 8px 10px; border-bottom: 1px solid #1f2733; font-size: 12px; }
  .status .k { color: #8b98a9; }
  .status .v { color: #e6e6e6; }
  .hpbar { height: 6px; background: #2a3340; border-radius: 4px; overflow: hidden; }
  .hpbar > i { display: block; height: 100%; background: linear-gradient(90deg,#ff5b5b,#6ee787); }
  .screen { padding: 8px 10px; }
  .screen img { width: 100%; border-radius: 6px; border: 1px solid #2a3340;
    background: #000; display: block; }
  .log { flex: 1; overflow-y: auto; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
  .msg { max-width: 86%; padding: 6px 9px; border-radius: 10px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }
  .msg.user { align-self: flex-end; background: #1f3a5f; }
  .msg.agent { align-self: flex-start; background: #1b2230; border: 1px solid #2a3340; }
  .msg .who { font-size: 10px; color: #8b98a9; display: block; margin-bottom: 2px; }
  .inputbar { display: flex; gap: 6px; padding: 8px 10px; border-top: 1px solid #1f2733; }
  .inputbar input { flex: 1; background: #11161d; border: 1px solid #2a3340; color: #e6e6e6;
    border-radius: 6px; padding: 6px 8px; }
  .inputbar button { background: #2563eb; color: #fff; border: none; border-radius: 6px;
    padding: 6px 12px; cursor: pointer; }
  .events { font-size: 11px; color: #8b98a9; padding: 4px 10px; max-height: 64px; overflow-y: auto;
    border-top: 1px solid #1f2733; }
  #openhint { display: none; padding: 6px 10px; font-size: 11px; color: #8b98a9; }
</style>
</head>
<body>
<div id="app">
  <div class="dragbar"></div>
  <header>
    <span class="logo">元灵 MetaSpirit</span>
    <span id="badge" class="badge">运行中</span>
    <span class="spacer"></span>
    <button id="btnScreenshot">刷新画面</button>
    <button id="btnStop">停止</button>
  </header>
  <div id="openhint">提示：这是浏览器预览版；可用 <code>metaspirit_overlay show</code> 唤起 Electron 悬浮窗。</div>
  <div class="status">
    <span class="k">场景</span><span class="v" id="stScene">—</span>
    <span class="k">紧迫度</span><span class="v" id="stUrgency">—</span>
    <span class="k">HP</span><span class="v"><span class="hpbar" style="display:inline-block;width:80px;vertical-align:middle"><i id="stHp" style="width:100%"></i></span> <span id="stHpTxt"></span></span>
    <span class="k">当前目标</span><span class="v" id="stGoal">—</span>
    <span class="k">预算(USD)</span><span class="v" id="stBudget">—</span>
    <span class="k">技能数</span><span class="v" id="stSkills">—</span>
    <span class="k">最近动作</span><span class="v" id="stAction">—</span>
  </div>
  <div class="screen"><img id="screen" src="/api/screen?t=0" alt="screen"></div>
  <div class="log" id="log"></div>
  <div class="events" id="events"></div>
  <div class="inputbar">
    <input id="txt" placeholder="和元灵说话…（例如：去西边看看）" autocomplete="off">
    <button id="send">发送</button>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const embed = new URLSearchParams(location.search).get('embed') === '1';
if (!embed) $('openhint').style.display = 'block';

function addMsg(who, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + (who === '你' ? 'user' : 'agent');
  d.innerHTML = '<span class="who">' + who + '</span>' + String(text).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  $('log').appendChild(d);
  $('log').scrollTop = $('log').scrollHeight;
}

async function refreshState() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    $('badge').textContent = s.running ? '运行中' : '已停止';
    $('badge').className = 'badge' + (s.running ? '' : ' off');
    $('stScene').textContent = s.scene ?? '—';
    $('stUrgency').textContent = (s.urgency ?? 0).toFixed(2);
    const hp = (s.hp == null ? 1 : s.hp);
    $('stHp').style.width = (hp * 100).toFixed(0) + '%';
    $('stHpTxt').textContent = (hp * 100).toFixed(0) + '%';
    $('stGoal').textContent = s.current_goals || '—';
    const b = s.budget || {};
    $('stBudget').textContent = (b.total_usd ?? 0).toFixed(4);
    $('stSkills').textContent = s.skills_count ?? 0;
    $('stAction').textContent = s.latest_action || '—';
    $('screen').src = '/api/screen?t=' + Date.now();
  } catch (e) { /* 忽略瞬时抖动 */ }
}

async function refreshEvents() {
  try {
    const r = await fetch('/api/events');
    const j = await r.json();
    const items = (j.events || []).slice().reverse().map(e => {
      const t = new Date((e.t || 0) * 1000).toLocaleTimeString();
      return '[' + t + '] ' + (e.type || '') + ': ' + (e.text || '');
    });
    $('events').innerHTML = items.join('<br>');
  } catch (e) {}
}

async function sendChat() {
  const v = $('txt').value.trim();
  if (!v) return;
  $('txt').value = '';
  addMsg('你', v);
  try {
    const r = await fetch('/api/chat', {
      method: 'POST', headers: {'Content-Type':'application/json','X-MetaSpirit-Token':(window.MS_TOKEN||'')},
      body: JSON.stringify({ text: v })
    });
    const j = await r.json();
    addMsg('元灵', j.reply || '(无回复)');
  } catch (e) {
    addMsg('元灵', '（通信失败：' + e + '）');
  }
}

$('send').onclick = sendChat;
$('txt').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });
$('btnScreenshot').onclick = refreshState;
$('btnStop').onclick = async () => { try { await fetch('/api/control', {method:'POST',headers:{'Content-Type':'application/json','X-MetaSpirit-Token':(window.MS_TOKEN||'')},body:JSON.stringify({action:'stop'})}); } catch(e){} };

refreshState(); refreshEvents();
setInterval(refreshState, 2000);
setInterval(refreshEvents, 2000);
</script>
</body>
</html>
"""


def render_page(embed: bool = False, token: str = "") -> str:
    """返回悬浮窗 HTML 页面字符串。embed 由前端按 ?embed=1 自行判断是否隐藏浏览器提示。
    token：POST 鉴权令牌，注入页面 JS（同源页面持有；跨Origin 网页读不到）。"""
    page = PAGE_HTML
    if token:
        inject = "window.MS_TOKEN = " + repr(token) + ";\n"
        page = page.replace("async function sendChat() {", inject + "async function sendChat() {", 1)
    return page
