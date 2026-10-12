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
    assert "if (code !== CLOSE_SUPERSEDED) self.hooks.leaveRoom(" in onclose


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
    assert out["states"][-2:] == ["joining", "joined"] or out["states"][-1] == "joined"
    assert "reconnecting" in out["states"]


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
