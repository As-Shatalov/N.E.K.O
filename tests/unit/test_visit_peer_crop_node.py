"""Parent bridge behaviour: visitor layer follows hb corrections, guest capture and starvation."""

from __future__ import annotations

from tests.visit_js_harness import BRIDGE_FILES, run_visit_js

_BRIDGE_JS = r"""
function makeBridge(opts) {
  opts = opts || {};
  const ctx = makeContext({ pathname: '/' });
  ctx.targetFrameRate = opts.targetFrameRate === undefined ? 60 : opts.targetFrameRate;
  if (opts.compat) ctx.__NEKO_DESKTOP_RUNTIME__ = { compatibilityMode: true };
  const view = ctx.document.createElement('canvas');
  view.width = 3840; view.height = 2160; view._rect = { left: 0, top: 0, width: 1920, height: 1080 };
  const handlers = {};
  const stage = { name: 'stage' };
  const renderer = {
    view, lastObjectRendered: stage, renderTexture: { current: null },
    on(e, f) { handlers[e] = f; }, off(e) { delete handlers[e]; },
  };
  const fpsCalls = [];
  // 三种渲染器都监听这个事件（live2d-core / vrm-manager / mmd-core）；这里只记下派发的帧率
  ctx.addEventListener('neko-frame-rate-changed', (e) => fpsCalls.push(e.detail.fps));
  ctx.live2dManager = {
    pixi_app: { renderer, stage },
    getModelScreenBounds: () => ({ left: 800, top: 100, right: 1200, bottom: 1000, width: 400, height: 900 }),
    getHeadDetectionGeometryInfo: () => ({ headRect: { left: 950, top: 150, width: 100, height: 120 } }),
  };
  ctx.avatarPortrait = { normalizeModelType: () => 'live2d', capture: () => Promise.resolve('img') };
  load(ctx, FILES);
  const bridge = ctx.NekoVisitParentBridge.createParentBridge({ window: ctx, document: ctx.document, getToken: () => Promise.resolve('tok'), assetVersion: '9' });
  const child = { posted: [], onFrames: [], token: null, suspended: [] };
  child.postMessage = (m) => child.posted.push(JSON.parse(JSON.stringify(m)));
  child.__nekoVisitFrameSink = {
    onFrame: (src, rect, ts) => { child.onFrames.push({ src: src === view, rect, ts }); return true; },
    suspend: (on) => child.suspended.push(on),
    setAuthToken: (t) => { child.token = t; },
  };
  return { ctx, bridge, child, renderer, handlers, stage, fpsCalls };
}
function change(h, action, extra) {
  h.bridge.onStateChange(Object.assign({ type: 'visit_state_change', action, side: h.side, visit_id: VISIT_ID }, extra || {}));
}
async function open(h, side, publishing) {
  h.side = side;
  change(h, 'pending');
  const frame = h.ctx.document.getElementById('visit-frame');
  frame.contentWindow = h.child;
  h.bridge._onFrameMessage({ source: h.child, origin: h.ctx.location.origin, data: { t: 'ready', v: 1, side, visit_id: VISIT_ID } });
  await settle();
  if (side === 'guest' && publishing !== false) {
    h.bridge._onFrameMessage({ source: h.child, origin: h.ctx.location.origin, data: { t: 'publish', on: true } });
  }
  h.frame = frame;
  return frame;
}
"""


def test_hb_corrected_peer_hidden_and_crop_reach_the_visitor_layer():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge();
        const frame = await open(h, 'host');
        change(h, 'started', { peer_crop: 'upper' });
        await advance(600);
        const upper = frame.style.width.replace('px', '') / frame.style.height.replace('px', '');
        // 只有 hb 纠正推导出的两条，没有先前的 state
        change(h, 'peer_hidden');
        const dimmed = frame.style.opacity;
        const badge = h.ctx.document.getElementById('visit-peer-away-badge');
        change(h, 'peer_crop', { peer_crop: 'full' });
        await advance(5000);
        const full = frame.style.width.replace('px', '') / frame.style.height.replace('px', '');
        change(h, 'peer_visible');
        return { upper, full, opacity: dimmed === '0.6' ? 'dim' : dimmed, badge: badge && badge.textContent,
                 badgeGone: !h.ctx.document.getElementById('visit-peer-away-badge'), afterVisible: frame.style.opacity,
                 style: frame.style.cssText, cls: frame.className, src: frame.src, token: h.child.token,
                 places: h.child.posted.filter((m) => m.t === 'place').length };
        """,
        files=BRIDGE_FILES,
    )
    assert abs(out["upper"] - 320 / 448) < 0.01
    assert abs(out["full"] - 256 / 560) < 0.01
    assert out["opacity"] == "dim"
    assert out["badge"] == "Stepped away"
    assert out["badgeGone"] and out["afterVisible"] == ""
    assert "pointer-events:none" in out["style"] and "z-index:4" in out["style"]
    assert out["cls"] == "transparent-overlay"
    assert out["src"] == "/visit/transport?v=9&side=host&visit_id=AbCdEfGhIjKlMnOpQrStUv"
    assert out["token"] == "tok"
    assert out["places"] >= 2


def test_guest_capture_guards_rate_and_starvation():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge({ targetFrameRate: 20 });
        await open(h, 'guest');
        change(h, 'started');
        change(h, 'departed');
        await advance(700);
        const post = h.handlers.postrender;
        // 守卫：临时舞台 / RenderTexture 渲染不抓
        h.renderer.lastObjectRendered = { name: 'temp' };
        for (let k = 0; k < 10; k++) { clock.now += 1000 / 60; post(); }
        h.renderer.lastObjectRendered = h.stage; h.renderer.renderTexture.current = {};
        for (let k = 0; k < 10; k++) { clock.now += 1000 / 60; post(); }
        h.renderer.renderTexture.current = null;
        const guarded = h.child.onFrames.length;
        // 60 Hz 渲染 2 s → 约 60 帧
        for (let k = 0; k < 120; k++) { clock.now += 1000 / 60; post(); }
        const captured = h.child.onFrames.length;
        const rect = h.child.onFrames[captured - 1].rect;
        await advance(1500);    // 没有渲染：帧饥饿
        const hiddenOn = h.child.posted.filter((m) => m.t === 'hidden');
        clock.now += 16; post(); clock.now += 17; post();
        const hiddenAll = h.child.posted.filter((m) => m.t === 'hidden').map((m) => m.on);
        const away = h.ctx.document.getElementById('visit-away-badge');
        const fpsDuring = h.fpsCalls.slice();
        const holdDuring = h.ctx.nekoTargetFrameRateHold && Object.assign({}, h.ctx.nekoTargetFrameRateHold);
        const fpsValueDuring = h.ctx.targetFrameRate;
        const crop = h.child.posted.filter((m) => m.t === 'crop').pop();
        change(h, 'ended');
        return { holdDuring, fpsValueDuring, holdAfter: h.ctx.nekoTargetFrameRateHold, fpsValueAfter: h.ctx.targetFrameRate, guarded, captured, rect, hiddenOn: hiddenOn.map((m) => [m.on, m.cause]), hiddenAll, fpsDuring, fpsAfter: h.fpsCalls,
                 capturing: h.ctx.live2dManager._visitCaptureActive, away: away && away.textContent, crop,
                 frameGone: !h.ctx.document.getElementById('visit-frame'), badgeGone: !h.ctx.document.getElementById('visit-away-badge'),
                 unhooked: !h.handlers.postrender };
        """,
        files=BRIDGE_FILES,
    )
    assert out["guarded"] == 0
    assert 58 <= out["captured"] <= 62
    assert out["rect"]["w"] > 0 and out["rect"]["h"] > 0 and out["rect"]["x"] >= 0
    assert out["hiddenOn"] == [[True, "frame_starvation"]]
    assert out["hiddenAll"] == [True, False]
    assert out["fpsDuring"] == [30]
    assert out["fpsAfter"] == [30, 20]
    assert out["holdDuring"] == {"userFps": 20, "heldFps": 30} and out["fpsValueDuring"] == 30
    assert out["holdAfter"] is None and out["fpsValueAfter"] == 20
    assert out["capturing"] is False
    assert out["away"] == "Out visiting"
    assert out["crop"]["fps"] == 30 and out["crop"]["mode"] == "upper"
    assert out["frameGone"] and out["badgeGone"] and out["unhooked"]


def test_compat_mode_captures_at_fifteen():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge({ compat: true, targetFrameRate: 10 });
        await open(h, 'guest');
        change(h, 'started');
        await advance(700);
        for (let k = 0; k < 120; k++) { clock.now += 1000 / 60; h.handlers.postrender(); }
        const crop = h.child.posted.filter((m) => m.t === 'crop').pop();
        return { captured: h.child.onFrames.length, fps: crop.fps, fpsCalls: h.fpsCalls };
        """,
        files=BRIDGE_FILES,
    )
    assert 28 <= out["captured"] <= 32
    assert out["fps"] == 15
    assert out["fpsCalls"] == [15]


def test_portrait_export_suspends_capture():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge();
        await open(h, 'guest');
        change(h, 'started');
        await h.ctx.avatarPortrait.capture({});
        return h.child.suspended;
        """,
        files=BRIDGE_FILES,
    )
    assert out == [True, False]


def test_frame_messages_from_other_sources_are_ignored():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge();
        h.side = 'host';
        change(h, 'pending');
        const frame = h.ctx.document.getElementById('visit-frame');
        frame.contentWindow = h.child;
        h.bridge._onFrameMessage({ source: {}, origin: h.ctx.location.origin, data: { t: 'ready', v: 1, side: 'host', visit_id: VISIT_ID } });
        h.bridge._onFrameMessage({ source: h.child, origin: 'http://evil.test', data: { t: 'ready', v: 1, side: 'host', visit_id: VISIT_ID } });
        await settle();
        const before = h.child.token;
        h.bridge._onFrameMessage({ source: h.child, origin: h.ctx.location.origin, data: { t: 'closed', code: 4404 } });
        return { before, gone: !h.ctx.document.getElementById('visit-frame') };
        """,
        files=BRIDGE_FILES,
    )
    assert out == {"before": None, "gone": True}


def test_offscreen_half_of_the_model_is_not_framed():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge();
        h.ctx.live2dManager.getModelScreenBounds = () => ({ left: 1500, top: 600, right: 2300, bottom: 1900, width: 800, height: 1300 });
        h.ctx.live2dManager.getHeadDetectionGeometryInfo = () => null;
        await open(h, 'guest');
        change(h, 'started');
        await advance(700);
        return h.bridge._state.rect;
        """,
        files=BRIDGE_FILES,
    )
    # 只按视口内可见的 420×480 构框：框的底边不会落到屏幕下方几百像素
    assert out["y"] + out["height"] <= 1080 + 400
    assert abs(out["x"] + out["width"] / 2 - (1500 + 210)) < 1e-6


def test_user_frame_rate_change_during_a_visit_wins():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge({ targetFrameRate: 20 });
        await open(h, 'guest');
        change(h, 'started');
        await advance(700);
        // 用户在串门中把帧率改成 45（设置弹窗：先写 targetFrameRate，再派发事件）
        h.ctx.targetFrameRate = 45;
        h.ctx.dispatchEvent(new h.ctx.CustomEvent('neko-frame-rate-changed', { detail: { fps: 45 } }));
        const hold = h.ctx.nekoTargetFrameRateHold;
        change(h, 'ended');
        return { hold, after: h.ctx.targetFrameRate, calls: h.fpsCalls };
        """,
        files=BRIDGE_FILES,
    )
    assert out["hold"] is None
    assert out["after"] == 45
    assert out["calls"] == [30, 45]


def test_visitor_layer_fits_a_narrow_viewport():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge();
        h.ctx.innerWidth = 360; h.ctx.innerHeight = 640;
        h.ctx.live2dManager.getModelScreenBounds = () => ({ left: 40, top: 0, right: 320, bottom: 640, width: 280, height: 640 });
        const frame = await open(h, 'host');
        change(h, 'started', { peer_crop: 'upper' });
        await advance(600);
        return { left: parseFloat(frame.style.left), width: parseFloat(frame.style.width), height: parseFloat(frame.style.height) };
        """,
        files=BRIDGE_FILES,
    )
    assert out["left"] >= 0
    assert out["left"] + out["width"] <= 360 + 1
    assert abs(out["width"] / out["height"] - 320 / 448) < 0.01


def test_text_only_visit_does_not_capture_or_report_hidden():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge({ targetFrameRate: 20 });
        await open(h, 'guest', false);           // 传输页没在发布（视频不可用）
        change(h, 'started');
        await advance(3000);
        const hooked = !!h.handlers.postrender;
        return { hooked, fps: h.fpsCalls, hidden: h.child.posted.filter((m) => m.t === 'hidden').length,
                 capturing: h.ctx.live2dManager._visitCaptureActive };
        """,
        files=BRIDGE_FILES,
    )
    assert out == {"hooked": False, "fps": [], "hidden": 0, "capturing": False}


def test_minimized_model_stops_capture_and_reports_hidden():
    out = run_visit_js(
        _BRIDGE_JS + """
        const h = makeBridge();
        const box = h.ctx.document.createElement('div');
        box.id = 'live2d-container';
        h.ctx.document.body.appendChild(box);
        await open(h, 'guest');
        change(h, 'started');
        await advance(700);
        for (let k = 0; k < 30; k++) { clock.now += 1000 / 60; h.handlers.postrender(); }
        const visible = h.child.onFrames.length;
        box.classList.add('minimized');
        await advance(700);
        const mark = h.child.onFrames.length;
        for (let k = 0; k < 120; k++) { clock.now += 1000 / 60; h.handlers.postrender(); }
        await advance(1500);
        const hidden = h.child.posted.filter((m) => m.t === 'hidden').map((m) => m.on);
        return { visible: visible > 0, minimizedFrames: h.child.onFrames.length - mark, hidden };
        """,
        files=BRIDGE_FILES,
    )
    assert out == {"visible": True, "minimizedFrames": 0, "hidden": [True]}


def test_mirrored_css_rect_maps_from_the_right_edge():
    out = run_visit_js(
        """
        const ctx = makeContext({ pathname: '/' });
        load(ctx, FILES);
        const g = ctx.NekoVisitCropGeometry;
        const m = { originX: 100, originY: 0, pixelRatioX: 2, pixelRatioY: 2, srcW: 800, srcH: 800, cssWidth: 400 };
        const rect = { x: 120, y: 10, width: 100, height: 50 };
        return { plain: g.cssRectToPixelRect(rect, m), mirrored: g.cssRectToPixelRect(rect, Object.assign({ mirrorX: true }, m)) };
        """,
        files=BRIDGE_FILES,
    )
    assert out["plain"] == {"x": 40, "y": 20, "w": 200, "h": 100}
    # 屏幕上 120..220（容器 100..500）镜像后对应源图 (500-220)*2 .. (500-120)*2
    assert out["mirrored"] == {"x": 560, "y": 20, "w": 200, "h": 100, "flipX": True}


_PNG_BRIDGE_JS = r"""
function makePngBridge(opts) {
  const ctx = makeContext({ pathname: '/' });
  ctx.targetFrameRate = 60;
  const img = ctx.document.createElement('img');
  img.naturalWidth = 1000; img.naturalHeight = 2000;
  img._rect = Object.assign({ left: 800, top: 100, width: 400, height: 800 }, opts.rect || {});
  ctx.pngtuberManager = { image: img };
  ctx.getComputedStyle = (el) => ({ visibility: 'visible', display: 'block', opacity: '1',
    transform: el === img && opts.mirror ? 'matrix(-1, 0, 0, 1, 0, 0)' : 'none' });
  ctx.nekoFramePacing = { requestPacedFrame: (cb) => { const id = setTimeoutF(() => cb(clock.now), 16); return () => clearTimerF(id); } };
  ctx.avatarPortrait = { normalizeModelType: () => 'pngtuber', capture: () => Promise.resolve('img') };
  load(ctx, FILES);
  const bridge = ctx.NekoVisitParentBridge.createParentBridge({ window: ctx, document: ctx.document, getToken: () => Promise.resolve('tok'), assetVersion: '9' });
  const child = { posted: [], onFrames: [] };
  child.postMessage = (m) => child.posted.push(JSON.parse(JSON.stringify(m)));
  child.__nekoVisitFrameSink = { onFrame: (src, rect) => { child.onFrames.push(rect); return true; }, suspend() {}, setAuthToken() {} };
  return { ctx, bridge, child, img };
}
"""


def test_png_capture_follows_mirror_and_viewport():
    out = run_visit_js(
        _BRIDGE_JS + _PNG_BRIDGE_JS + """
        const runs = {};
        for (const [name, opts] of [['mirrored', { mirror: true }], ['offscreen', { rect: { left: 2500 } }], ['edge', { rect: { left: 1700 } }]]) {
          const h = makePngBridge(opts);
          await open(h, 'guest');
          change(h, 'started');
          await advance(1600);
          const last = h.child.onFrames[h.child.onFrames.length - 1] || null;
          runs[name] = { frames: h.child.onFrames.length, last, hidden: h.child.posted.filter((m) => m.t === 'hidden').map((m) => m.on) };
          change(h, 'ended');
        }
        return runs;
        """,
        files=BRIDGE_FILES,
    )
    assert out["mirrored"]["frames"] > 0 and out["mirrored"]["last"]["flipX"] is True
    assert out["offscreen"]["frames"] == 0 and out["offscreen"]["hidden"] == [True]
    # 一半挂在屏幕外（视口宽 1920）：取景框以可见的 1700..1920 为准，中心在 1810 附近（不裁剪会在 1900）
    edge = out["edge"]["last"]
    center_css = 1700 + (edge["x"] + edge["w"] / 2) / 2.5
    assert out["edge"]["frames"] > 0 and abs(center_css - 1810) < 15
