"""Static gates and fake-SDK behaviour of the visit transport page (design §5 PR-10)."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from tests.visit_js_harness import PROJECT_ROOT, TRANSPORT_DIR, run_visit_js

TEMPLATE = PROJECT_ROOT / "templates" / "visit_transport.html"
LIBS = PROJECT_ROOT / "static" / "libs"
NOTICES = LIBS / "THIRD_PARTY_NOTICES.md"
SDK_FILES = {"trtc": LIBS / "trtc.js", "livekit": LIBS / "livekit-client.umd.js"}


def _src(name: str) -> str:
    return (TRANSPORT_DIR / name).read_text(encoding="utf-8")


def _all_transport_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(TRANSPORT_DIR.glob("*.js"))}


def _code_only(src: str) -> str:
    """Drop // line comments and /* */ blocks (the gates look at code, not prose)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}),\s])//[^\n]*", "", src)


def _function_body(src: str, header_regex: str) -> str:
    m = re.search(header_regex, src)
    assert m, header_regex
    depth = 0
    start = src.index("{", m.end() - 1)
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces")


# ── 静态门 ────────────────────────────────────────────────────────────


def test_template_only_references_static_and_no_vendor_sdk():
    html = TEMPLATE.read_text(encoding="utf-8")
    refs = re.findall(r"""(?:src|href)=["']([^"']+)["']""", html)
    assert refs, "template must load the transport scripts"
    assert all(r.startswith("/static/visit/transport/") and "?v={{ static_asset_version }}" in r for r in refs)
    assert "trtc.js" not in html and "livekit-client" not in html
    assert "<video" in html and "muted" in html and "playsinline" in html
    video_rule = re.search(r"#visit-video\s*\{([^}]*)\}", html).group(1).replace(" ", "")
    assert "display:none" not in video_rule and "width:2px" in video_rule and "opacity:0.01" in video_rule
    assert re.search(r"html,\s*body\s*\{[^}]*background:\s*transparent", html)


def test_only_backend_ws_opens_a_websocket_and_only_to_location_host():
    sources = _all_transport_sources()
    owners = [n for n, s in sources.items() if "new WebSocket(" in _code_only(s)]
    assert owners == ["backend-ws.js"]
    ws = _code_only(sources["backend-ws.js"])
    assert ws.count("new WebSocket(") == 1
    assert "loc.host" in ws and "'/api/visit/transport/ws?visit_id='" in ws
    assert "fetch(" not in ws
    for name, s in sources.items():
        assert "page_config" not in s, name
    onopen = _function_body(ws, r"ws\.onopen = function \(\)")
    assert "type: 'auth', csrf_token: self.token" in onopen
    assert onopen.index("type: 'auth'") < onopen.index("self.hooks.onOpen()")


def test_backend_close_stops_media_and_leaves_before_any_reconnect():
    ws = _code_only(_src("backend-ws.js"))
    onclose = _function_body(ws, r"ws\.onclose = function \(event\)")
    stop = onclose.index("stopAllMedia(")
    leave = onclose.index("leaveRoom(")
    assert stop < onclose.index("setTimeout(") and leave < onclose.index("setTimeout(")
    # 4409 只停媒体、不离房、不重连
    assert "code !== CLOSE_SUPERSEDED ? self.hooks.leaveRoom('backend_socket_closed') : null" in onclose
    # 重连等上一次离房结束
    assert "Promise.all([delayDone, leftOrGaveUp])" in onclose


def test_frame_sink_object_is_built_once_with_three_entry_points():
    sources = _all_transport_sources()
    assigners = {n: s.count("__nekoVisitFrameSink =") for n, s in sources.items() if "__nekoVisitFrameSink =" in s}
    assert assigners == {"frame-sink.js": 1}
    body = _function_body(sources["frame-sink.js"], r"root\.__nekoVisitFrameSink = ")
    for key in ("onFrame:", "suspend:", "setAuthToken:"):
        assert key in body


def test_vendor_parameters_are_pinned():
    lk = _code_only(_src("livekit-transport.js"))
    for literal in ("scalabilityMode: 'L1T1'", "simulcast: false", "autoSubscribe: false", "'maintain-framerate'",
                    "adaptiveStream: false", "dynacast: false"):
        assert literal in lk, literal
    assert "setMicrophoneEnabled" not in lk and "createLocalAudioTrack" not in lk
    trtc = _code_only(_src("trtc-transport.js"))
    for literal in ("autoReceiveVideo: false", "autoReceiveAudio: false", "view: null", "'SmallStreamAutoSwitcher'"):
        assert literal in trtc, literal
    for name, s in _all_transport_sources().items():
        assert "startLocalAudio" not in s, name
        assert "visibilitychange" not in s, name


def test_sizes_come_only_from_the_framing_tables():
    pack = _code_only(_src("pack.js"))
    assert "alpha: false" in pack and "requestFrame()" in pack and "captureStream(0)" in pack
    assert "contentHint = 'motion'" in pack
    for name in ("trtc-transport.js", "unpack.js", "livekit-transport.js", "loader.js"):
        code = _code_only(_src(name))
        assert not re.search(r"\b(896|448|1120|560|320|256)\b", code.replace("560) * 1000", "")), name
    # pack.js：尺寸数字只出现在 CROP_GEOMETRY 与阶梯表的定义里
    tables = re.search(r"var CROP_GEOMETRY = \{.*?\};\s*.*?var CONGESTION_LADDER = \{.*?\};", pack, re.S)
    assert tables
    rest = pack.replace(tables.group(0), "")
    assert not re.search(r"\b(896|448|1120|560|320|256|352|272|208|160|192)\b", rest)


def test_preflight_does_not_touch_vendor_globals():
    loader = _code_only(_src("loader.js"))
    preflight = _function_body(loader, r"function runPreflight\(win\)")
    assert "TRTC" not in preflight and "LivekitClient" not in preflight
    assert "isSecureContext" in preflight and "RTCPeerConnection" in preflight
    assert "captureStream" not in preflight and "webgl" not in preflight.lower()


def test_vendor_sdks_are_registered_pinned_and_hashed():
    notices = NOTICES.read_text(encoding="utf-8")
    assert "## trtc-sdk-v5" in notices and "## livekit-client" in notices
    assert "`5.20.1`" in notices and "`2.22.3`" in notices
    loader = _src("loader.js")
    for transport, path in SDK_FILES.items():
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() in notices, transport
        sri = "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()
        assert f"{transport}: '{sri}'" in loader, transport
    assert (LIBS / "licenses" / "TRTC.LICENSE").is_file()
    assert "Apache License" in (LIBS / "licenses" / "LIVEKIT.LICENSE").read_text(encoding="utf-8")
    nuitka = (PROJECT_ROOT / "scripts" / "check_nuitka_dist.py").read_text(encoding="utf-8")
    for line in ('("static/libs", "trtc.js")', '("static/libs", "livekit-client.umd.js")',
                 '("static/libs/licenses", "TRTC.LICENSE")', '("static/libs/licenses", "LIVEKIT.LICENSE")'):
        assert line in nuitka, line


# ── 假 SDK 行为 ───────────────────────────────────────────────────────


def test_fragment_pacing_stays_inside_the_vendor_window():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        const start = clock.now;
        for (let k = 0; k < 3; k++) h.c.core.send(2, { t: 'text', v: 1, seq: k + 1, ln: 'g:' + k, txt: '喵'.repeat(2600) });
        for (let k = 0; k < 12; k++) h.c.core.send(2, { t: 'line_delta', v: 1, ln: 'g:9', i: k, txt: 'abc'.repeat(50) });
        await advance(8000);
        const sends = h.log.filter((e) => e[0] === 'send');
        let worstBytes = 0, worstPieces = 0;
        for (const s of sends) {
          const win = sends.filter((x) => x[1] >= s[1] && x[1] < s[1] + 1000);
          worstBytes = Math.max(worstBytes, win.reduce((a, x) => a + x[3], 0));
          worstPieces = Math.max(worstPieces, win.length);
        }
        const firstSecond = sends.filter((x) => x[1] < start + 1000).reduce((a, x) => a + x[3], 0);
        return { total: sends.length, worstBytes, worstPieces, firstSecond, lastAt: sends[sends.length - 1][1] - start };
        """
    )
    assert out["worstBytes"] <= 6144
    assert out["worstPieces"] <= 20
    assert out["firstSecond"] <= 6144
    assert out["lastAt"] < 6000
    assert out["total"] >= 3 * 7 + 12


def test_backend_socket_drop_stops_media_leaves_and_reconnects_with_same_vid():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const firstTrack = h.c.packer.track;
        h.log.length = 0;
        h.ws.serverClose(1006);
        const syncKinds = kinds(h.log);
        const socketsBefore = h.sockets.length;
        await advance(1100);
        const ws2 = h.sockets[h.sockets.length - 1];
        ws2.serverOpen();
        const firstFrames = ws2.sent.map((m) => m.type + (m.stage ? ':' + m.stage : ''));
        ws2.serverSend(credsFor('guest'));
        await settle();
        ws2.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const enter = h.log.filter((e) => e[0] === 'enterRoom');
        const publish = h.log.filter((e) => e[0] === 'startLocalVideo');
        return {
          syncKinds, reconnected: h.sockets.length === socketsBefore + 1, firstFrames,
          enterUser: enter.map((e) => e[1]), trackReused: publish.length === 1 && publish[0][1] === firstTrack.id,
          trackLive: firstTrack.readyState, states: ws2.sent.filter((m) => m.type === 'state').map((m) => m.state),
        };
        """
    )
    # 同步执行：先停发布，再离房；都早于 1 s 后的重连
    assert out["syncKinds"][:2] == ["stopLocalVideo", "exitRoom"]
    assert out["reconnected"]
    assert out["firstFrames"][:2] == ["auth", "caps:preflight"]
    assert out["enterUser"] == ["g_self"]
    assert out["trackReused"] and out["trackLive"] == "live"
    assert "joined" in out["states"]


def test_ended_track_is_rebuilt_before_republishing():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const old = h.c.packer.track;
        h.ws.serverSend({ type: 'media', publish: false });
        await settle();
        old.stop();
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const pub = h.log.filter((e) => e[0] === 'startLocalVideo').map((e) => e[1]);
        return { pub, oldId: old.id, streams: h.c.packer.pack.streams };
        """
    )
    assert out["pub"][0] == out["oldId"]
    assert out["pub"][1] != out["oldId"]
    assert out["streams"] == 2


def test_superseded_4409_is_terminal_and_does_not_leave():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.log.length = 0;
        const n = h.sockets.length;
        h.ws.serverClose(4409);
        await advance(30000);
        // 新 iframe 同 vid 入房把这里顶下线：不触发任何上报
        h.trtc.emit('KICKED_OUT', { reason: 'kick' });
        return { kinds: kinds(h.log), newSockets: h.sockets.length - n, closed: h.posted.filter((m) => m.t === 'closed') };
        """
    )
    assert "stopLocalVideo" in out["kinds"]
    assert "exitRoom" not in out["kinds"]
    assert out["newSockets"] == 0
    assert out["closed"] == [{"t": "closed", "code": 4409}]


def test_unknown_visit_4404_leaves_once_and_stops_for_good():
    out = run_visit_js(
        """
        const h = await joinedController('host');
        h.log.length = 0;
        const n = h.sockets.length;
        h.ws.serverClose(4404);
        await advance(30000);
        return { exits: h.log.filter((e) => e[0] === 'exitRoom').length, newSockets: h.sockets.length - n,
                 closed: h.posted.filter((m) => m.t === 'closed').length };
        """
    )
    assert out == {"exits": 1, "newSockets": 0, "closed": 1}


def test_reconnect_gives_up_after_twenty_seconds():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.ws.serverClose(1006);
        for (let k = 0; k < 40; k++) {
          await advance(1000);
          const last = h.sockets[h.sockets.length - 1];
          if (last.readyState !== 3) last.serverClose(1006);
        }
        return { sockets: h.sockets.length, closed: h.posted.filter((m) => m.t === 'closed').length,
                 exits: h.log.filter((e) => e[0] === 'exitRoom').length };
        """
    )
    assert out["closed"] == 1
    assert out["sockets"] <= 23
    assert out["exits"] >= 1


def test_text_only_snapshots_never_publish():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.ws.serverSend({ type: 'media', publish: false, crop: 'upper', ladder: 0 });
        await settle();
        h.ws.serverClose(1006);
        await advance(1100);
        const ws2 = h.sockets[h.sockets.length - 1];
        ws2.serverOpen();
        ws2.serverSend(credsFor('guest'));
        await settle();
        ws2.serverSend({ type: 'media', publish: false, crop: 'upper', ladder: 0 });
        await settle();
        const host = await joinedController('host');
        host.ws.serverSend({ type: 'media', subscribe: false, peer_vid: 'g_peer' });
        host.trtc.emit('REMOTE_VIDEO_AVAILABLE', { userId: 'g_peer', streamType: 'main' });
        await settle();
        return { guestPublish: h.log.filter((e) => e[0] === 'startLocalVideo').length,
                 hostSubscribe: host.log.filter((e) => e[0] === 'startRemoteVideo').length };
        """
    )
    assert out == {"guestPublish": 0, "hostSubscribe": 0}


def test_refresh_credentials_are_accepted_without_rejoining_and_used_by_a_manual_rejoin():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.ws.serverSend(credsFor('guest', { refresh: true, vendor: { trtc: { sdk_app_id: 1, user_id: 'g_self', user_sig: 'sig2', private_map_key: 'pmk2', str_room_id: VISIT_ID, expire: 600 } } }));
        h.ws.serverSend(credsFor('guest', { refresh: true, vendor: { trtc: { sdk_app_id: 1, user_id: 'g_self', user_sig: 'sig3', private_map_key: 'pmk3', str_room_id: VISIT_ID, expire: 600 } } }));
        await settle();
        const entersAfterRefresh = h.log.filter((e) => e[0] === 'enterRoom').length;
        // SDK 自动重连因旧凭证鉴权失败：手动 exitRoom + enterRoom，用最新凭证
        h.trtc.emit('CONNECTION_STATE_CHANGED', { prevState: 'RECONNECTING', state: 'DISCONNECTED' });
        await settle();
        const enters = h.log.filter((e) => e[0] === 'enterRoom');
        const states = h.ws.sent.filter((m) => m.type === 'state').map((m) => m.state);
        return { entersAfterRefresh, lastSig: enters[enters.length - 1][2], enterCount: enters.length, states };
        """
    )
    assert out["entersAfterRefresh"] == 1
    assert out["enterCount"] == 2
    assert out["lastSig"] == "sig3"
    # 手动重新入房的完整顺序：先报重连中，再 joining，最后 joined
    tail = out["states"][out["states"].index("reconnecting"):]
    assert tail == ["reconnecting", "joining", "joined"]


def test_peer_presence_reports_only_explicit_leaves():
    out = run_visit_js(
        """
        const h = await joinedController('host');
        h.trtc.emit('REMOTE_USER_ENTER', { userId: 'g_peer' });
        h.trtc.emit('REMOTE_USER_EXIT', { userId: 'g_peer', reason: 1 });
        h.trtc.emit('REMOTE_USER_ENTER', { userId: 'g_peer' });
        h.trtc.emit('REMOTE_USER_EXIT', { userId: 'g_peer', reason: 0 });
        const st = h.ws.sent.filter((m) => m.type === 'state');
        return st.map((m) => [m.peer_present, m.vendor_reason || null]);
        """
    )
    assert out[-2:] == [[True, None], [False, "0"]]
    assert [False, "1"] not in out


def test_parent_hidden_goes_up_as_state_hidden():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.c.onParentHidden(true);
        await advance(1500);
        h.c.onParentHidden(false);
        await advance(1500);
        return h.ws.sent.filter((m) => m.type === 'state').map((m) => m.hidden);
        """
    )
    assert out[-2:] == [True, False]


def test_preflight_rejects_a_wrapped_websocket():
    out = run_visit_js(
        """
        const ctx = makeContext();
        load(ctx, FILES);
        const bad = ctx.NekoVisitTransport.loader.runPreflight(ctx);
        const insecure = ctx.NekoVisitTransport.loader.runPreflight(Object.assign({}, ctx, { isSecureContext: false }));
        const noRtc = ctx.NekoVisitTransport.loader.runPreflight(Object.assign({}, ctx, { WebSocket: globalThis.WebSocket || function WebSocket() {}, RTCPeerConnection: undefined }));
        return { bad: bad.reason, insecure: insecure.reason, noRtc: noRtc.reason, clean: ctx.NekoVisitTransport.loader.queryIsClean('?v=1&side=host&visit_id=x'), dirty: ctx.NekoVisitTransport.loader.queryIsClean('?v=1&trtcDebug=1') };
        """
    )
    assert out["bad"] == "foreign_websocket"
    assert out["insecure"] == "insecure_context"
    assert out["noRtc"] in ("no_webrtc", "foreign_websocket")
    assert out["clean"] is True and out["dirty"] is False


def test_first_frame_carries_a_96px_png_data_url():
    out = run_visit_js(
        """
        const h = await joinedController('host');
        const video = h.c.unpacker.video;
        h.ws.serverSend({ type: 'media', subscribe: true, peer_vid: 'g_peer' });
        h.trtc.emit('REMOTE_VIDEO_AVAILABLE', { userId: 'g_peer', streamType: 'main' });
        await settle();
        Object.assign(video, { readyState: 4, videoWidth: 256, videoHeight: 1120 });
        h.c.unpacker.draw();
        await advance(50);
        const ff = h.posted.filter((m) => m.t === 'first_frame');
        return { n: ff.length, url: ff[0] && ff[0].dataURL };
        """
    )
    assert out["n"] == 1
    url = out["url"]
    assert url.startswith("data:image/png;base64,")
    png = base64.b64decode(url.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(png[16:20], "big") == 96 and int.from_bytes(png[20:24], "big") == 96


def test_stop_unpublishes_leaves_reports_left_and_closes():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.log.length = 0;
        h.ws.serverSend({ type: 'stop', reason: 'route_end' });
        await settle();
        await advance(5000);
        return { kinds: kinds(h.log), lastState: h.ws.sent.filter((m) => m.type === 'state').pop().state,
                 closedWith: h.ws.closedWith, sockets: h.sockets.length, trackEnded: h.c.packer.track.readyState };
        """
    )
    assert out["kinds"][:2] == ["stopLocalVideo", "exitRoom"]
    assert out["lastState"] == "left"
    assert out["closedWith"] == 1000
    assert out["sockets"] == 1
    assert out["trackEnded"] == "ended"


def test_unknown_or_foreign_template_free_files():
    # 目录里只有登记过的 8 个脚本（新增脚本要同步模板与本测试）
    names = sorted(p.name for p in TRANSPORT_DIR.glob("*.js"))
    assert names == sorted([
        "transport.js", "pack.js", "unpack.js", "trtc-transport.js", "livekit-transport.js",
        "frame-sink.js", "backend-ws.js", "loader.js",
    ])
    html = TEMPLATE.read_text(encoding="utf-8")
    assert [Path(r).name.split("?")[0] for r in re.findall(r'src="([^"]+)"', html)] == [
        "transport.js", "pack.js", "unpack.js", "trtc-transport.js", "livekit-transport.js",
        "frame-sink.js", "backend-ws.js", "loader.js",
    ]


_FAKE_LIVEKIT = """
function makeFakeLK(log) {
  const RoomEvent = {};
  ['ParticipantConnected', 'ParticipantDisconnected', 'TrackPublished', 'TrackUnpublished', 'TrackSubscribed',
   'TrackUnsubscribed', 'DataReceived', 'Reconnecting', 'Reconnected', 'Disconnected', 'ConnectionQualityChanged']
    .forEach((n) => { RoomEvent[n] = n; });
  const sender = {
    params: { encodings: [{ maxBitrate: 560000, maxFramerate: 30 }] },
    getParameters() { return JSON.parse(JSON.stringify(this.params)); },
    setParameters(p) { this.params = p; log.push(['setParameters', p.encodings[0].maxBitrate, p.encodings[0].maxFramerate]); return Promise.resolve(); },
  };
  class Room {
    constructor(opts) { this.opts = opts; this.handlers = {}; this.remoteParticipants = new Map();
      this.localParticipant = {
        publishTrack: (t, o) => { log.push(['publishTrack', o.videoCodec]); return Promise.resolve({ track: { sender } }); },
        unpublishTrack: () => Promise.resolve(), publishData: () => Promise.resolve() };
      Room.last = this; }
    on(e, f) { this.handlers[e] = f; }
    emit(e, ...a) { this.handlers[e] && this.handlers[e](...a); }
    connect(url, token, o) { log.push(['connect', url, o.autoSubscribe]); return Promise.resolve(); }
    disconnect() { return Promise.resolve(); }
    removeAllListeners() {}
  }
  return { Room, RoomEvent, Track: { Source: { Camera: 'camera' } }, DisconnectReason: { 1: 'CLIENT_INITIATED' }, sender, isBrowserSupported: () => true };
}
"""


def test_failed_trtc_publish_is_retried_by_the_next_snapshot():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let fail = true;
        const orig = h.trtc.startLocalVideo;
        h.trtc.startLocalVideo = (c) => { if (fail) { fail = false; h.log.push(['startLocalVideo-fail']); return Promise.reject(new Error('transient')); } return orig(c); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        return kinds(h.log).filter((k) => k.startsWith('startLocalVideo') || k === 'updateLocalVideo');
        """
    )
    assert out == ["startLocalVideo-fail", "startLocalVideo"]


def test_livekit_ladder_codec_stats_and_loopback():
    out = run_visit_js(
        _FAKE_LIVEKIT + """
        const ctx = makeContext();
        load(ctx, FILES);
        const LKV = ctx.NekoVisitTransport.vendors.livekit;
        const log = [];
        const LK = makeFakeLK(log);
        const events = [];
        const handlers = { onPiece() {}, onPeer() {}, onState(s) { events.push(s); }, onRemoteTrack() {}, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const creds = { vendor: { livekit: { url: 'wss://lk.example.com', token: 't' } }, allowed_hosts: ['lk.example.com'], publish: { codec: 'vp8', bitrate_kbps: 560, fps: 30 } };
        const a = LKV.createAdapter({ sdk: LK, handlers });
        await a.join(creds);
        await a.publish({ id: 'pack' });
        const ok = await a.updateProfile({ width: 208, height: 896, frameRate: 15, bitrate: 400 });
        // 接收统计
        let bytes = 0, recv = 0, lost = 0;
        const receiver = { getStats: () => Promise.resolve(new Map([
          ['in', { type: 'inbound-rtp', kind: 'video', bytesReceived: bytes, packetsReceived: recv, packetsLost: lost }],
          ['cp', { type: 'candidate-pair', nominated: true, currentRoundTripTime: 0.12 }]]) ) };
        await a.setSubscribe(true);
        LK.Room.last.emit('TrackSubscribed', { kind: 'video', mediaStreamTrack: {}, receiver }, {}, { identity: 'g_peer' });
        const first = await a.receiverStats(0);
        bytes = 350000; recv = 950; lost = 50;
        const second = await a.receiverStats(5000);
        // 编码能力门
        const caps = (enc, dec) => { ctx.RTCRtpSender = { getCapabilities: () => ({ codecs: enc.map((n) => ({ mimeType: 'video/' + n })) }) };
          ctx.RTCRtpReceiver = { getCapabilities: () => ({ codecs: dec.map((n) => ({ mimeType: 'video/' + n })) }) }; };
        caps(['VP8'], ['VP8']);
        const vp8Only = await LKV.capabilities(LK, { publish: { codec: 'vp9' } }, 'guest');
        const hostVp8Only = await LKV.capabilities(LK, { publish: { codec: 'vp9' } }, 'host');
        caps([], ['VP8', 'VP9']);
        const noEnc = await LKV.capabilities(LK, { publish: { codec: 'vp9' } }, 'guest');
        const hostBoth = await LKV.capabilities(LK, {}, 'host');
        return {
          ok, log, first, second, vp8Only, hostVp8Only: hostVp8Only.decode, noEnc: [noEnc.encode, noEnc.codec], hostBoth: hostBoth.decode,
          hosts: [
            LKV.hostAllowed('ws://127.0.0.1:7880', ['127.0.0.1']),
            LKV.hostAllowed('ws://[::1]:7880', ['::1']),
            LKV.hostAllowed('ws://localhost:7880', ['localhost']),
            LKV.hostAllowed('ws://lk.example.com', ['lk.example.com']),
            LKV.hostAllowed('wss://lk.example.com/x', ['lk.example.com']),
            LKV.hostAllowed('wss://evil.example.com', ['lk.example.com']),
          ],
        };
        """
    )
    assert out["ok"] is True
    assert ["setParameters", 400000, 15] in out["log"]
    assert ["connect", "wss://lk.example.com", False] in out["log"]
    assert out["first"] == {"rtt_ms": 120}
    assert out["second"]["rx_kbps"] == 560 and abs(out["second"]["loss_pct"] - 5) < 1e-9 and out["second"]["rtt_ms"] == 120
    assert out["vp8Only"]["encode"] is True and out["vp8Only"]["codec"] == "vp8"
    assert out["hostVp8Only"] is False
    assert out["noEnc"] == [False, None]
    assert out["hostBoth"] is True
    assert out["hosts"] == [True, True, True, False, True, False]


def test_codec_fallback_is_kept_across_credential_refresh():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.c.codecOverride = 'vp8';
        h.c.applyCodecOverride(h.c.creds);
        h.ws.serverSend(credsFor('guest', { refresh: true, publish: { codec: 'vp9', bitrate_kbps: 560, fps: 30 } }));
        await settle();
        return h.c.creds.publish.codec;
        """
    )
    assert out == "vp8"


def test_stalled_stream_reports_zero_rx_fps_in_fallback():
    out = run_visit_js(
        """
        const ctx = makeContext();
        load(ctx, FILES);
        const doc = ctx.document;
        const video = doc.createElement('video');
        let decoded = 300;
        video.getVideoPlaybackQuality = () => ({ totalVideoFrames: decoded, droppedVideoFrames: 0 });
        const u = new ctx.NekoVisitTransport.unpack.Unpacker({ video, canvas: doc.createElement('canvas') });
        u.draws = 0;
        u.stats(0);
        u.draws = 150;             // 回落定时器 5 s 里重画了 150 次，但没有新帧
        const stalled = u.stats(5000).rx_fps;
        decoded = 450;
        const flowing = u.stats(10000).rx_fps;
        return { stalled, flowing };
        """
    )
    assert out == {"stalled": 0, "flowing": 30}


def test_reconnect_waits_for_a_slow_vendor_exit():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let releaseExit;
        h.trtc.exitRoom = () => { h.log.push(['exitRoom-slow']); return new Promise((r) => { releaseExit = r; }); };
        const n = h.sockets.length;
        h.ws.serverClose(1006);
        await advance(3000);
        const beforeExit = h.sockets.length - n;
        releaseExit();
        await settle();
        const afterExit = h.sockets.length - n;
        return { beforeExit, afterExit };
        """
    )
    assert out == {"beforeExit": 0, "afterExit": 1}


def test_publish_finishing_after_stop_is_withdrawn():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let finish;
        h.trtc.startLocalVideo = (c) => { h.log.push(['startLocalVideo-pending']); return new Promise((r) => { finish = r; }); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.ws.serverSend({ type: 'media', publish: false });
        await settle();
        finish();
        await settle();
        return kinds(h.log).filter((k) => k.indexOf('LocalVideo') !== -1);
        """
    )
    assert out == ["startLocalVideo-pending", "stopLocalVideo"]


def test_quick_republish_after_stop_keeps_the_new_publication():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        const orig = h.trtc.startLocalVideo;
        let finishFirst;
        let first = true;
        h.trtc.startLocalVideo = (c) => {
          if (first) { first = false; h.log.push(['start-1-pending']); return new Promise((r) => { finishFirst = r; }); }
          return orig(c);
        };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.ws.serverSend({ type: 'media', publish: false });
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        finishFirst();
        await settle();
        await settle();
        return kinds(h.log).filter((k) => k.indexOf('LocalVideo') !== -1 || k.startsWith('start-'));
        """
    )
    # 顺序执行：第一次发布完成 → 停止 → 第二次发布；最后停在「已发布」
    assert out == ["start-1-pending", "stopLocalVideo", "startLocalVideo"]


def test_superseded_while_publish_in_flight_still_stops_before_destroy():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let finish;
        h.trtc.startLocalVideo = (c) => { h.log.push(['start-pending']); return new Promise((r) => { finish = r; }); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.ws.serverClose(4409);
        await settle();
        finish();
        await settle();
        await settle();
        return kinds(h.log).filter((k) => ['start-pending', 'stopLocalVideo', 'exitRoom', 'destroy'].includes(k));
        """
    )
    # 被顶掉（4409）不离房，但在途的发布完成后必须停掉，然后才销毁实例
    assert out == ["start-pending", "stopLocalVideo", "destroy"]


def test_publish_requested_during_join_is_applied_after_join():
    out = run_visit_js(
        """
        const h = makeController('guest');
        h.c.onAuthToken('tok');
        const ws = sockets[sockets.length - 1];
        ws.serverOpen();
        let finishEnter;
        const origCreate = h.TRTC.create;
        h.TRTC.create = () => {
          const inst = origCreate();
          const origEnter = inst.enterRoom;
          inst.enterRoom = (p) => new Promise((r) => { finishEnter = () => r(origEnter(p)); });
          return inst;
        };
        ws.serverSend(credsFor('guest'));
        await settle();
        // 入房还没完成就收到发布快照
        ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const before = h.log.filter((e) => e[0] === 'startLocalVideo').length;
        finishEnter();
        await settle();
        await settle();
        return { before, after: h.log.filter((e) => e[0] === 'startLocalVideo').length };
        """
    )
    assert out == {"before": 0, "after": 1}


def test_stuck_vendor_exit_ends_in_terminal_instead_of_reconnecting():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.trtc.exitRoom = () => new Promise(() => {});      // 永远不结束
        const n = h.sockets.length;
        h.ws.serverClose(1006);
        await advance(25000);
        return { newSockets: h.sockets.length - n, closed: h.posted.filter((m) => m.t === 'closed').length };
        """
    )
    assert out == {"newSockets": 0, "closed": 1}


def test_leave_completion_waits_for_in_flight_publish():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let finish;
        h.trtc.startLocalVideo = (c) => { h.log.push(['start-pending']); return new Promise((r) => { finish = r; }); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const n = h.sockets.length;
        h.ws.serverClose(1006);
        await advance(3000);
        const reconnectedBeforePublishSettled = h.sockets.length - n;
        finish();
        await settle();
        await settle();
        return { reconnectedBeforePublishSettled, after: h.sockets.length - n,
                 kinds: kinds(h.log).filter((k) => ['start-pending', 'stopLocalVideo', 'exitRoom'].includes(k)) };
        """
    )
    assert out["reconnectedBeforePublishSettled"] == 0
    assert out["after"] == 1
    # exitRoom 立即发出；离房后才完成的发布被停掉
    assert out["kinds"] == ["start-pending", "exitRoom", "stopLocalVideo"]


def test_livekit_initial_publication_uses_the_supplied_profile():
    out = run_visit_js(
        _FAKE_LIVEKIT + """
        const ctx = makeContext();
        load(ctx, FILES);
        const LKV = ctx.NekoVisitTransport.vendors.livekit;
        const log = [];
        const LK = makeFakeLK(log);
        const handlers = { onPiece() {}, onPeer() {}, onState() {}, onRemoteTrack() {}, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const a = LKV.createAdapter({ sdk: LK, handlers });
        await a.join({ vendor: { livekit: { url: 'wss://lk.example.com', token: 't' } }, allowed_hosts: ['lk.example.com'], publish: { codec: 'vp9', bitrate_kbps: 560, fps: 30 } });
        await a.publish({ id: 'pack' }, { width: 160, height: 704, frameRate: 15, bitrate: 300 });
        return log.filter((e) => e[0] === 'setParameters');
        """
    )
    assert out == [["setParameters", 300000, 15]]


def test_leave_completion_also_waits_for_the_late_stop():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let finishStart, finishStop;
        h.trtc.startLocalVideo = () => new Promise((r) => { finishStart = r; });
        h.trtc.stopLocalVideo = () => { h.log.push(['stop-pending']); return new Promise((r) => { finishStop = r; }); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const n = h.sockets.length;
        h.ws.serverClose(1006);
        await advance(1500);
        finishStart();                      // 离房后发布才完成 → 迟到的停止开始、还没结束
        await settle();
        await advance(1500);
        const beforeStop = h.sockets.length - n;
        finishStop();
        await settle();
        await settle();
        return { beforeStop, after: h.sockets.length - n };
        """
    )
    assert out == {"beforeStop": 0, "after": 1}


def test_failed_publish_tells_the_parent_to_stop_capturing():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let fail = true;
        const orig = h.trtc.startLocalVideo;
        h.trtc.startLocalVideo = (c) => { if (fail) { fail = false; return Promise.reject(new Error('x')); } return orig(c); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        const afterFail = h.posted.filter((m) => m.t === 'publish').map((m) => m.on);
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        return { afterFail, afterRetry: h.posted.filter((m) => m.t === 'publish').map((m) => m.on) };
        """
    )
    # 失败时父页从没被告知开始（或已被告知停）；重试成功后才通知开始
    assert out["afterFail"] in ([], [False])
    assert out["afterRetry"][-1] is True


def test_video_gate_requires_a_manual_frame_track():
    out = run_visit_js(
        """
        const ctx = makeContext();
        load(ctx, FILES);
        const doc = ctx.document;
        const orig = doc.createElement.bind(doc);
        doc.createElement = (tag) => {
          const el = orig(tag);
          if (tag === 'canvas') el.captureStream = () => ({ getVideoTracks: () => [{ kind: 'video', stop() {} }] });
          return el;
        };
        const h = { c: new ctx.NekoVisitTransport.loader.VisitTransportController({ window: ctx, document: doc, side: 'guest',
          visitId: VISIT_ID, post: () => {}, log: () => {}, loadSdk: () => Promise.resolve(makeFakeTRTC([])) }) };
        h.c.preflight = { ok: true };
        const sent = [];
        h.c.sendBackend = (m) => sent.push(m);
        h.c.onCredentials(credsFor('guest'));
        await settle();
        const caps = sent.find((m) => m.type === 'caps' && m.stage === 'sdk');
        return { transport_ok: caps.transport_ok, video_ok: caps.video_ok };
        """
    )
    assert out == {"transport_ok": True, "video_ok": False}


def test_stale_loss_sample_expires():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.c.startStats();
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.trtc.emit('NETWORK_QUALITY', { uplinkLoss: 40, uplinkRTT: 100 });   // 一次很差的样本，之后再无回调
        const peerStats = () => {
          const pieces = h.NVT.fragment(JSON.stringify({ t: 'stats', v: 1, rx_fps: 30 }), { visitId: VISIT_ID, msgId: Math.floor(Math.random() * 1e6) });
          h.trtc.emit('CUSTOM_MESSAGE', { userId: 'h_peer', cmdId: 3, data: new TextEncoder().encode(pieces[0]).buffer });
        };
        for (let k = 0; k < 12; k++) { peerStats(); await advance(5000); }
        return h.c.media.ladder;
        """
    )
    # 旧的丢包样本 12 s 后过期，之后只看健康的 rx_fps：至多被它压下一级，不会一路压到最低档
    assert out <= 1


def test_host_rejoin_waits_for_a_pending_remote_subscription():
    out = run_visit_js(
        """
        const h = await joinedController('host');
        let finish;
        h.trtc.startRemoteVideo = () => { h.log.push(['startRemote-pending']); return new Promise((r) => { finish = r; }); };
        h.ws.serverSend({ type: 'media', subscribe: true, peer_vid: 'g_peer' });
        h.trtc.emit('REMOTE_VIDEO_AVAILABLE', { userId: 'g_peer', streamType: 'main' });
        await settle();
        const n = h.sockets.length;
        h.ws.serverClose(1006);
        await advance(3000);
        const before = h.sockets.length - n;
        finish();
        await settle();
        await settle();
        return { before, after: h.sockets.length - n };
        """
    )
    assert out == {"before": 0, "after": 1}


def test_profile_change_during_pending_publish_is_applied_after_it():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let finish;
        const orig = h.trtc.startLocalVideo;
        h.trtc.startLocalVideo = (c) => new Promise((r) => { finish = () => r(orig(c)); });
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.c.setGeometry('upper', 1);          // 发布挂起期间降了一档
        finish();
        await settle();
        await settle();
        const updates = h.log.filter((e) => e[0] === 'updateLocalVideo').map((e) => [e[1].width, e[1].height, e[1].bitrate]);
        return updates[updates.length - 1];
        """
    )
    assert out == [256, 704, 400]


def test_new_socket_clears_backpressure():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        return h.ws.sent.filter((m) => m.type === 'tx_backpressure').map((m) => m.on);
        """
    )
    assert out == [False]


def test_persistent_livekit_quality_keeps_driving_the_ladder():
    out = run_visit_js(
        _FAKE_LIVEKIT + """
        const ctx = makeContext();
        load(ctx, FILES);
        const LKV = ctx.NekoVisitTransport.vendors.livekit;
        const LK = makeFakeLK([]);
        const handlers = { onPiece() {}, onPeer() {}, onState() {}, onRemoteTrack() {}, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const a = LKV.createAdapter({ sdk: LK, handlers });
        const before = a.currentLossPct() ?? null;
        await a.join({ vendor: { livekit: { url: 'wss://lk.example.com', token: 't' } }, allowed_hosts: ['lk.example.com'], publish: { codec: 'vp9' } });
        const room = LK.Room.last;
        room.emit('ConnectionQualityChanged', 'poor', room.localParticipant);
        const poor = [a.currentLossPct(), a.currentLossPct()];
        room.emit('ConnectionQualityChanged', 'good', room.localParticipant);
        const good = a.currentLossPct();
        await a.leave();
        return { before, poor, good, afterLeave: a.currentLossPct() ?? null };
        """
    )
    assert out == {"before": None, "poor": [100, 100], "good": 0, "afterLeave": None}


def test_livekit_reconnect_drops_stale_quality_and_restarts_ladder_timers():
    out = run_visit_js(
        _FAKE_LIVEKIT + """
        const ctx = makeContext();
        load(ctx, FILES);
        const LKV = ctx.NekoVisitTransport.vendors.livekit;
        const LK = makeFakeLK([]);
        const handlers = { onPiece() {}, onPeer() {}, onState() {}, onRemoteTrack() {}, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const a = LKV.createAdapter({ sdk: LK, handlers });
        await a.join({ vendor: { livekit: { url: 'wss://lk.example.com', token: 't' } }, allowed_hosts: ['lk.example.com'], publish: { codec: 'vp9' } });
        const room = LK.Room.last;
        room.emit('ConnectionQualityChanged', 'poor', room.localParticipant);
        const before = a.currentLossPct();
        room.emit('Reconnecting');
        room.emit('Reconnected');
        return { before, after: a.currentLossPct() ?? null };
        """
    )
    assert out == {"before": 100, "after": None}


def test_controller_drops_loss_and_ladder_timers_when_leaving_the_room_state():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.c.loss = { value: 100, at: clock.now };
        h.c.ladder.badSince = 1;
        h.c.ladder.cleanSince = 2;
        const level = h.c.ladder.level;
        h.c.onVendorState('reconnecting', {});
        return { loss: h.c.loss, bad: h.c.ladder.badSince, clean: h.c.ladder.cleanSince, sameLevel: h.c.ladder.level === level };
        """
    )
    assert out == {"loss": None, "bad": None, "clean": None, "sameLevel": True}


def test_livekit_sender_stats_report_tx_kbps():
    out = run_visit_js(
        _FAKE_LIVEKIT + """
        const ctx = makeContext();
        load(ctx, FILES);
        const LKV = ctx.NekoVisitTransport.vendors.livekit;
        const LK = makeFakeLK([]);
        let sample = { framesPerSecond: 30, frameWidth: 256, frameHeight: 704, bytesSent: 0, timestamp: 1000 };
        const handlers = { onPiece() {}, onPeer() {}, onState() {}, onRemoteTrack() {}, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const a = LKV.createAdapter({ sdk: LK, handlers });
        await a.join({ vendor: { livekit: { url: 'wss://lk.example.com', token: 't' } }, allowed_hosts: ['lk.example.com'], publish: { codec: 'vp9' } });
        const room = LK.Room.last;
        room.localParticipant.publishTrack = () => Promise.resolve({ track: { sender: LK.sender, getSenderStats: () => Promise.resolve([sample]) } });
        await a.publish({ id: 'pack' });
        const first = await a.senderStats();
        sample = Object.assign({}, sample, { bytesSent: 350000, timestamp: 6000 });
        const second = await a.senderStats();
        return { first: first.tx_kbps === undefined, second: second.tx_kbps };
        """
    )
    assert out == {"first": True, "second": 560}


def test_queue_reset_clears_the_backpressure_latch():
    out = run_visit_js(
        """
        const ctx = makeContext();
        load(ctx, FILES);
        const reports = [];
        const core = new ctx.NekoVisitTransport.TransportCore({ visitId: VISIT_ID, onRecv() {}, onBackpressure: (b) => reports.push(b.on) });
        core.backpressure = true;
        core.reset();
        return core.backpressure;
        """
    )
    assert out is False


def test_failed_trtc_remote_subscription_is_retried():
    out = run_visit_js(
        """
        const h = await joinedController('host');
        let fails = 2;
        const orig = h.trtc.startRemoteVideo;
        h.trtc.startRemoteVideo = (c) => { if (fails > 0) { fails -= 1; h.log.push(['startRemote-fail']); return Promise.reject(new Error('transient')); } return orig(c); };
        h.ws.serverSend({ type: 'media', subscribe: true, peer_vid: 'g_peer' });
        h.trtc.emit('REMOTE_VIDEO_AVAILABLE', { userId: 'g_peer', streamType: 'main' });
        await settle();
        await advance(2100);
        await settle();
        await advance(4100);
        await settle();
        return kinds(h.log).filter((k) => k.startsWith('startRemote'));
        """
    )
    assert out == ["startRemote-fail", "startRemote-fail", "startRemoteVideo"]


def test_capability_gate_finishing_after_socket_close_does_not_join():
    out = run_visit_js(
        """
        const h = makeController('guest');
        let release;
        h.c.loadSdk = () => new Promise((r) => { release = () => r(h.TRTC); });
        h.c.onAuthToken('tok');
        const ws = sockets[sockets.length - 1];
        ws.serverOpen();
        ws.serverSend(credsFor('guest'));
        await settle();
        ws.serverClose(1006);
        release();
        await settle();
        return { enter: kinds(h.log).filter((k) => k === 'enterRoom').length, sdkCaps: ws.sent.filter((m) => m.type === 'caps' && m.stage === 'sdk').length };
        """
    )
    assert out == {"enter": 0, "sdkCaps": 0}


def test_trtc_remote_subscription_keeps_retrying_with_backoff():
    out = run_visit_js(
        """
        const h = await joinedController('host');
        let fails = 8;
        const orig = h.trtc.startRemoteVideo;
        h.trtc.startRemoteVideo = (c) => { if (fails > 0) { fails -= 1; h.log.push(['startRemote-fail', clock.now]); return Promise.reject(new Error('transient')); } return orig(c); };
        h.ws.serverSend({ type: 'media', subscribe: true, peer_vid: 'g_peer' });
        h.trtc.emit('REMOTE_VIDEO_AVAILABLE', { userId: 'g_peer', streamType: 'main' });
        await settle();
        for (let i = 0; i < 12; i++) { await advance(31000); await settle(); }
        const fails_at = h.log.filter((e) => e[0] === 'startRemote-fail').map((e) => e[1]);
        const gaps = fails_at.slice(1).map((t, i) => t - fails_at[i]);
        return { started: kinds(h.log).includes('startRemoteVideo'), failures: fails_at.length, maxGap: Math.max(...gaps) };
        """
    )
    assert out["started"] is True
    assert out["failures"] == 8
    assert out["maxGap"] <= 31000


def test_rejected_publish_is_retried_with_backoff():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let fail = 2;
        const orig = h.trtc.startLocalVideo;
        h.trtc.startLocalVideo = (c) => { if (fail > 0) { fail -= 1; h.log.push(['startLocalVideo-fail']); return Promise.reject(new Error('transient')); } return orig(c); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        await advance(2100); await settle();
        const afterFirst = kinds(h.log).filter((k) => k.startsWith('startLocalVideo'));
        await advance(4100); await settle();
        return { afterFirst, all: kinds(h.log).filter((k) => k.startsWith('startLocalVideo')),
                 announced: h.posted.filter((m) => m.t === 'publish').map((m) => m.on) };
        """
    )
    assert out["afterFirst"] == ["startLocalVideo-fail", "startLocalVideo-fail"]
    assert out["all"] == ["startLocalVideo-fail", "startLocalVideo-fail", "startLocalVideo"]
    assert out["announced"] == [False, True]


def test_publish_retry_stops_when_publication_is_withdrawn():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.trtc.startLocalVideo = () => { h.log.push(['startLocalVideo-fail']); return Promise.reject(new Error('transient')); };
        h.ws.serverSend({ type: 'media', publish: true, crop: 'upper', ladder: 0 });
        await settle();
        h.ws.serverSend({ type: 'media', publish: false });
        await advance(60000); await settle();
        return kinds(h.log).filter((k) => k.startsWith('startLocalVideo'));
        """
    )
    assert out == ["startLocalVideo-fail"]


def test_trtc_leave_waits_for_a_pending_enter_room():
    out = run_visit_js(
        """
        const ctx = makeContext();
        load(ctx, FILES);
        const log = [];
        const TRTC = makeFakeTRTC(log);
        const states = [];
        const handlers = { onPiece() {}, onPeer() {}, onState(s) { states.push(s); }, onRemoteTrack() {}, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const a = ctx.NekoVisitTransport.vendors.trtc.createAdapter({ sdk: TRTC, handlers });
        let finishEnter;
        const origCreate = TRTC.create;
        TRTC.create = () => { const inst = origCreate(); inst.enterRoom = () => new Promise((r) => { finishEnter = r; }); return inst; };
        const joining = a.join(credsFor('guest')).then(() => 'joined-resolved');
        await settle();
        let left = false;
        a.leave().then(() => { left = true; });
        await settle();
        const leftBefore = left;
        finishEnter();
        await settle();
        await joining;
        return { leftBefore, leftAfter: left, states, canSend: a.canSend() };
        """
    )
    assert out == {"leftBefore": False, "leftAfter": True, "states": ["joining"], "canSend": False}


def test_failed_manual_rejoin_is_retried_within_the_window():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        let fails = 2;
        const orig = h.trtc.enterRoom;
        h.trtc.enterRoom = (p) => { if (fails > 0) { fails -= 1; h.log.push(['enterRoom-fail']); return Promise.reject(new Error('transient')); } return orig(p); };
        h.c.rejoin();
        await settle();
        await advance(1100); await settle();
        await advance(2100); await settle();
        return { calls: kinds(h.log).filter((k) => k.startsWith('enterRoom')),
                 errors: h.ws.sent.filter((m) => m.type === 'state' && m.state === 'error').length };
        """
    )
    assert out == {"calls": ["enterRoom", "enterRoom-fail", "enterRoom-fail", "enterRoom"], "errors": 0}


def test_manual_rejoin_reports_error_once_the_window_is_spent():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.trtc.enterRoom = () => { h.log.push(['enterRoom-fail']); return Promise.reject(new Error('down')); };
        h.c.rejoin();
        await settle();
        for (let i = 0; i < 8; i++) { await advance(5000); await settle(); }
        return { fails: kinds(h.log).filter((k) => k === 'enterRoom-fail').length,
                 errors: h.ws.sent.filter((m) => m.type === 'state' && m.state === 'error').length };
        """
    )
    assert out["errors"] == 1
    assert 3 <= out["fails"] <= 5


def test_leaving_the_room_state_drops_cached_peer_fps():
    out = run_visit_js(
        """
        const h = await joinedController('guest');
        h.c.peerRxFps = { value: 5, at: clock.now };
        h.c.onVendorState('reconnecting', {});
        const afterState = h.c.peerRxFps;
        h.c.peerRxFps = { value: 5, at: clock.now };
        h.c.stopAllMedia();
        return [afterState, h.c.peerRxFps];
        """
    )
    assert out == [None, None]


def test_livekit_late_unsubscribe_of_an_old_track_keeps_the_new_one():
    out = run_visit_js(
        _FAKE_LIVEKIT + """
        const ctx = makeContext();
        load(ctx, FILES);
        const LKV = ctx.NekoVisitTransport.vendors.livekit;
        const LK = makeFakeLK([]);
        const tracks = [];
        const handlers = { onPiece() {}, onPeer() {}, onState() {}, onRemoteTrack(t) { tracks.push(t ? t.id : null); }, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const a = LKV.createAdapter({ sdk: LK, handlers });
        await a.join({ vendor: { livekit: { url: 'wss://lk.example.com', token: 't' } }, allowed_hosts: ['lk.example.com'], publish: { codec: 'vp9' } });
        await a.setSubscribe(true);
        const room = LK.Room.last;
        const oldT = { kind: 'video', mediaStreamTrack: { id: 'old' } };
        const newT = { kind: 'video', mediaStreamTrack: { id: 'new' } };
        room.emit('TrackSubscribed', oldT, {}, { identity: 'g_peer' });
        room.emit('TrackSubscribed', newT, {}, { identity: 'g_peer' });
        room.emit('TrackUnsubscribed', oldT);
        const afterOld = tracks.slice();
        room.emit('TrackUnsubscribed', newT);
        return { afterOld, all: tracks };
        """
    )
    assert out == {"afterOld": ["old", "new"], "all": ["old", "new", None]}


def test_livekit_republish_of_the_same_track_reapplies_the_profile():
    out = run_visit_js(
        _FAKE_LIVEKIT + """
        const ctx = makeContext();
        load(ctx, FILES);
        const LKV = ctx.NekoVisitTransport.vendors.livekit;
        const log = [];
        const LK = makeFakeLK(log);
        const handlers = { onPiece() {}, onPeer() {}, onState() {}, onRemoteTrack() {}, onNetwork() {}, onNeedRejoin() {}, onLog() {} };
        const a = LKV.createAdapter({ sdk: LK, handlers });
        await a.join({ vendor: { livekit: { url: 'wss://lk.example.com', token: 't' } }, allowed_hosts: ['lk.example.com'], publish: { codec: 'vp8', bitrate_kbps: 560, fps: 30 } });
        const track = { id: 'pack' };
        const profile = { width: 256, height: 704, frameRate: 15, bitrate: 400 };
        await a.publish(track, profile);
        // SDK 整体重连按发布选项重发：发送端参数回到默认
        LK.sender.params = { encodings: [{ maxBitrate: 560000, maxFramerate: 30 }] };
        const mark = log.length;
        const ok = await a.publish(track, profile);
        return { ok, again: log.slice(mark).filter((e) => e[0] === 'setParameters'), publishes: log.filter((e) => e[0] === 'publishTrack').length };
        """
    )
    assert out == {"ok": True, "again": [["setParameters", 400000, 15]], "publishes": 1}
