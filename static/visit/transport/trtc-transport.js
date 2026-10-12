/*
 * 串门传输页：TRTC 适配器（trtc-sdk-v5 5.20.1，设计稿 §3.5.1 / §4.3）。
 *
 * 串门不传音频：autoReceiveAudio:false，全目录不调用麦克风采集。视频只发自定义画布轨，
 * 远端视频 startRemoteVideo({view:null}) 只取轨不渲染。数据通道用 sendCustomMessage
 * （cmdId 1/2/3，全房广播，靠信封 r 与发送者绑定过滤）。
 *
 * 5.20.1 没有 updatePrivateMapKey：续期凭证只替换本地保存的那份，手动重新入房时用最新的。
 */
(function (root) {
  'use strict';

  var NVT = root.NekoVisitTransport = root.NekoVisitTransport || {};

  function createTrtcAdapter(opts) {
    var TRTC = opts.sdk;
    var handlers = opts.handlers;
    var trtc = null;
    var creds = null;
    var joined = false;
    var leaving = false;
    var publishing = false;
    var publishedTrack = null;
    // 发布代数：每次发布 / 撤销 / 离房都加一；发布完成时代数已变说明期间被停过，立即撤销
    var publishGen = 0;
    var subscribeWanted = false;
    var peerVid = opts.peerVid || null;
    var remoteVideoUser = null;
    var remoteStarted = false;
    var lastStats = null;
    var lastNetwork = null;

    function vendorOf(c) {
      return (c && c.vendor && c.vendor.trtc) || {};
    }

    function isPeer(userId) {
      // host 在对端 hello 核验前不知道 peer_vid：房里另一个人就是候选对端（Servers 保证每房 2 人）
      return !peerVid || userId === peerVid;
    }

    function bind() {
      var E = TRTC.EVENT;
      trtc.on(E.REMOTE_USER_ENTER, function (ev) {
        if (isPeer(ev.userId)) handlers.onPeer('enter', ev.userId, null);
      });
      trtc.on(E.REMOTE_USER_EXIT, function (ev) {
        if (!isPeer(ev.userId)) return;
        // reason 0 = 对端主动离开（暂定离开，后端起 35 s 重入宽限）；1 超时交给心跳，不上报
        var explicit = ev.reason === 0 || ev.reason === undefined;
        handlers.onPeer('exit', ev.userId, explicit ? '0' : String(ev.reason), explicit);
        if (ev.userId === remoteVideoUser) {
          remoteVideoUser = null;
          remoteStarted = false;
          handlers.onRemoteTrack(null);
        }
      });
      trtc.on(E.REMOTE_VIDEO_AVAILABLE, function (ev) {
        if (!isPeer(ev.userId) || ev.streamType !== TRTC.TYPE.STREAM_TYPE_MAIN) return;
        remoteVideoUser = ev.userId;
        maybeStartRemote();
      });
      trtc.on(E.REMOTE_VIDEO_UNAVAILABLE, function (ev) {
        if (ev.userId !== remoteVideoUser) return;
        remoteVideoUser = null;
        remoteStarted = false;
        handlers.onRemoteTrack(null);
      });
      trtc.on(E.TRACK, function (ev) {
        if (ev && ev.track && ev.track.kind === 'video' && ev.userId && ev.userId === remoteVideoUser && subscribeWanted) {
          handlers.onRemoteTrack(ev.track);
        }
      });
      trtc.on(E.CUSTOM_MESSAGE, function (ev) {
        handlers.onPiece(ev.userId, ev.cmdId, new Uint8Array(ev.data));
      });
      trtc.on(E.CONNECTION_STATE_CHANGED, function (ev) {
        if (leaving) return;
        if (ev.state === 'RECONNECTING') {
          handlers.onState('reconnecting', {});
        } else if (ev.state === 'CONNECTED' && ev.prevState !== 'CONNECTING') {
          handlers.onState('connected', {});
        } else if (ev.state === 'DISCONNECTED' && joined) {
          // SDK 放弃自动重连（常见于旧凭证鉴权失败）：用最新凭证手动重新入房
          joined = false;
          handlers.onState('reconnecting', {});
          handlers.onNeedRejoin();
        }
      });
      trtc.on(E.KICKED_OUT, function (ev) {
        joined = false;
        handlers.onState('kicked', { vendor_reason: String(ev && ev.reason) });
      });
      trtc.on(E.NETWORK_QUALITY, function (ev) {
        lastNetwork = ev;
        handlers.onNetwork({ lossPct: ev.uplinkLoss, rttMs: ev.uplinkRTT });
      });
      trtc.on(E.STATISTICS, function (s) {
        lastStats = s;
      });
      trtc.on(E.ERROR, function (err) {
        handlers.onLog('warn', 'trtc error', String(err && err.code));
      });
    }

    function maybeStartRemote() {
      if (!subscribeWanted || !remoteVideoUser || remoteStarted || !trtc) return;
      remoteStarted = true;
      var user = remoteVideoUser;
      trtc.startRemoteVideo({ userId: user, streamType: TRTC.TYPE.STREAM_TYPE_MAIN, view: null })
        .then(function () {
          var track = trtc.getVideoTrack({ userId: user, streamType: TRTC.TYPE.STREAM_TYPE_MAIN });
          if (track && subscribeWanted && user === remoteVideoUser) handlers.onRemoteTrack(track);
        })
        .catch(function (err) {
          remoteStarted = false;
          handlers.onLog('warn', 'trtc startRemoteVideo failed', String(err && err.code));
        });
    }

    function enterRoom() {
      var v = vendorOf(creds);
      return trtc.enterRoom({
        sdkAppId: v.sdk_app_id,
        userId: v.user_id,
        userSig: v.user_sig,
        privateMapKey: v.private_map_key,
        strRoomId: v.str_room_id,
        scene: TRTC.TYPE.SCENE_RTC,
        role: TRTC.TYPE.ROLE_ANCHOR,
        autoReceiveVideo: false,
        autoReceiveAudio: false,
      });
    }

    return {
      name: 'trtc',
      join: function (c) {
        creds = c;
        leaving = false;
        if (!trtc) {
          trtc = TRTC.create();
          bind();
        }
        handlers.onState('joining', {});
        return enterRoom().then(function () {
          joined = true;
          // 防 SDK 自动把远端切到小流（串门只有一路主流）
          var stopping = trtc.stopPlugin('SmallStreamAutoSwitcher');
          if (stopping && typeof stopping.catch === 'function') stopping.catch(function () {});
          handlers.onState('joined', {});
        });
      },
      updateCredentials: function (c) {
        creds = c;
      },
      canSend: function () {
        return joined && !leaving;
      },
      sendPiece: function (cmd, bytes) {
        var buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        trtc.sendCustomMessage({ cmdId: cmd, data: buf });
      },
      publish: function (track, profile) {
        if (!trtc || !joined) return Promise.resolve(false);
        // 同一条轨已在发布：重复的 media 快照不再触发 SDK 调用（尺寸变化走 updateProfile）
        if (publishing && publishedTrack === track) return Promise.resolve(true);
        var config = { publish: true, option: { videoTrack: track, profile: profile } };
        var gen = ++publishGen;
        var p = publishing ? trtc.updateLocalVideo(config) : trtc.startLocalVideo(config);
        // 发布成功才记状态：失败（暂时性）时下一次 media{publish:true} 还要真的再发一次
        return p.then(function () {
          if (gen !== publishGen) {
            // 发布进行中收到了停止（media{publish:false} / 4409 / 离房）：不能在停止之后又发出去
            if (trtc && joined) trtc.stopLocalVideo().catch(function () {});
            return false;
          }
          publishing = true;
          publishedTrack = track;
          return true;
        }, function (err) {
          if (publishedTrack !== track) publishedTrack = null;
          throw err;
        });
      },
      updateProfile: function (profile) {
        if (!trtc || !publishing) return Promise.resolve(false);
        return trtc.updateLocalVideo({ option: { profile: profile } }).then(function () { return true; });
      },
      unpublish: function () {
        publishGen += 1;
        if (!trtc || !publishing) return Promise.resolve();
        publishing = false;
        publishedTrack = null;
        return trtc.stopLocalVideo().catch(function () {});
      },
      setPeerVid: function (vid) {
        peerVid = vid || peerVid;
      },
      setSubscribe: function (on) {
        subscribeWanted = !!on;
        if (subscribeWanted) {
          maybeStartRemote();
          return Promise.resolve();
        }
        handlers.onRemoteTrack(null);
        if (!trtc || !remoteStarted || !remoteVideoUser) return Promise.resolve();
        remoteStarted = false;
        return trtc.stopRemoteVideo({ userId: remoteVideoUser, streamType: TRTC.TYPE.STREAM_TYPE_MAIN }).catch(function () {});
      },
      /** 停媒体并离房；画布轨不 stop（自定义轨归调用方，重连后复用）。 */
      leave: function () {
        leaving = true;
        joined = false;
        remoteStarted = false;
        publishGen += 1;
        publishing = false;
        publishedTrack = null;
        if (!trtc) return Promise.resolve();
        return trtc.exitRoom().catch(function () {});
      },
      destroy: function () {
        if (trtc) {
          try {
            trtc.destroy();
          } catch (_) {
            // 已销毁
          }
        }
        trtc = null;
      },
      remoteVideoAvailable: function () {
        return !!remoteVideoUser;
      },
      stats: function () {
        var out = {};
        if (lastStats) {
          var big = (lastStats.localStatistics && lastStats.localStatistics.video || []).filter(function (v) { return v.videoType === 'big'; })[0];
          if (big) {
            out.enc_fps = big.frameRate;
            out.tx_kbps = big.bitrate;
            out.tx_w = big.width;
            out.tx_h = big.height;
          }
          var remote = (lastStats.remoteStatistics || [])[0];
          var rv = remote && (remote.video || []).filter(function (v) { return v.videoType === 'big'; })[0];
          if (rv) out.rx_kbps = rv.bitrate;
          out.rtt_ms = lastStats.rtt;
          out.loss_pct = lastStats.upLoss;
        }
        if (lastNetwork && typeof lastNetwork.uplinkLoss === 'number') out.loss_pct = lastNetwork.uplinkLoss;
        return out;
      },
    };
  }

  /** 能力门 ③：TRTC.isSupported() 的 detail → transport_ok / 编解码列表。 */
  function trtcCapabilities(TRTC) {
    return TRTC.isSupported().then(function (res) {
      var d = (res && res.detail) || {};
      var codecs = [];
      if (d.isH264EncodeSupported || d.isH264DecodeSupported) codecs.push('h264');
      if (d.isVp8EncodeSupported || d.isVp8DecodeSupported) codecs.push('vp8');
      return {
        transportOk: d.isWebRTCSupported !== false && typeof root.RTCPeerConnection === 'function',
        encode: !!(d.isH264EncodeSupported || d.isVp8EncodeSupported),
        decode: !!(d.isH264DecodeSupported || d.isVp8DecodeSupported),
        codecs: codecs,
      };
    });
  }

  NVT.vendors = NVT.vendors || {};
  NVT.vendors.trtc = {
    globalName: 'TRTC',
    script: '/static/libs/trtc.js',
    createAdapter: createTrtcAdapter,
    capabilities: trtcCapabilities,
  };
})(typeof window !== 'undefined' ? window : globalThis);
