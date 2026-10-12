/*
 * 串门传输页：LiveKit 适配器（livekit-client 2.22.3，设计稿 §3.4.3 / §3.5.1 / §4.3）。
 *
 * 单层编码：scalabilityMode 必须显式 'L1T1'（SDK 对 vp9 默认三层 SVC）、simulcast 关、
 * maintain-framerate；autoSubscribe:false，host 收到 media{subscribe:true} 才订阅对端视频。
 * 串门不传音频：不开麦克风、不建本地音频轨。数据通道 topic visit.ctl / visit.text 可靠、
 * visit.lossy 不可靠；对端 vid 已知后定向发送。
 */
(function (root) {
  'use strict';

  var NVT = root.NekoVisitTransport = root.NekoVisitTransport || {};

  function reasonName(LK, reason) {
    var table = LK && LK.DisconnectReason;
    if (table && typeof table[reason] === 'string') return table[reason];
    return reason === undefined || reason === null ? '' : String(reason);
  }

  function hostAllowed(url, allowedHosts) {
    var host;
    try {
      var parsed = new URL(url);
      if (parsed.protocol !== 'wss:') return false;
      host = parsed.hostname.toLowerCase();
    } catch (_) {
      return false;
    }
    return Array.isArray(allowedHosts) && allowedHosts.some(function (h) {
      return typeof h === 'string' && h.toLowerCase() === host;
    });
  }

  function createLivekitAdapter(opts) {
    var LK = opts.sdk;
    var handlers = opts.handlers;
    var room = null;
    var creds = null;
    var joined = false;
    var leaving = false;
    var publication = null;
    var publishedTrack = null;
    var subscribeWanted = false;
    var peerVid = opts.peerVid || null;
    var remotePublication = null;
    var lastQuality = null;

    function vendorOf(c) {
      return (c && c.vendor && c.vendor.livekit) || {};
    }

    function codecPref(c) {
      var codec = c && c.publish && c.publish.codec;
      return codec === 'vp8' ? 'vp8' : 'vp9';
    }

    function isPeer(identity) {
      return !peerVid || identity === peerVid;
    }

    function newRoom(c) {
      var publishCfg = (c && c.publish) || {};
      return new LK.Room({
        adaptiveStream: false,
        dynacast: false,
        publishDefaults: {
          simulcast: false,
          videoCodec: codecPref(c),
          scalabilityMode: 'L1T1',
          videoEncoding: { maxBitrate: (publishCfg.bitrate_kbps || 560) * 1000, maxFramerate: publishCfg.fps || 30 },
          degradationPreference: 'maintain-framerate',
        },
      });
    }

    function considerPublication(pub, participant) {
      if (!participant || !isPeer(participant.identity)) return;
      if (!pub || pub.kind !== 'video') return;
      remotePublication = pub;
      if (subscribeWanted) pub.setSubscribed(true);
    }

    function bind() {
      var E = LK.RoomEvent;
      room.on(E.ParticipantConnected, function (p) {
        if (isPeer(p.identity)) handlers.onPeer('enter', p.identity, null);
      });
      room.on(E.ParticipantDisconnected, function (p, reason) {
        if (!isPeer(p.identity)) return;
        var name = reasonName(LK, reason);
        // 对端主动断开 = 暂定离开（35 s 重入宽限）；其它原因不上报，交给心跳判死
        handlers.onPeer('exit', p.identity, name, name === 'CLIENT_INITIATED');
        if (remotePublication && p.identity === (remotePublication.participantIdentity || p.identity)) {
          remotePublication = null;
          handlers.onRemoteTrack(null);
        }
      });
      room.on(E.TrackPublished, function (pub, participant) {
        considerPublication(pub, participant);
      });
      room.on(E.TrackUnpublished, function (pub, participant) {
        if (pub === remotePublication && participant && isPeer(participant.identity)) {
          remotePublication = null;
          handlers.onRemoteTrack(null);
        }
      });
      room.on(E.TrackSubscribed, function (track, pub, participant) {
        if (!participant || !isPeer(participant.identity) || track.kind !== 'video' || !subscribeWanted) return;
        handlers.onRemoteTrack(track.mediaStreamTrack);
      });
      room.on(E.TrackUnsubscribed, function (track) {
        if (track && track.kind === 'video') handlers.onRemoteTrack(null);
      });
      room.on(E.DataReceived, function (payload, participant, kind, topic) {
        if (!participant) return;
        var cmd = NVT.cmdForTopic(topic);
        handlers.onPiece(participant.identity, cmd, payload);
      });
      room.on(E.Reconnecting, function () {
        if (!leaving) handlers.onState('reconnecting', {});
      });
      room.on(E.Reconnected, function () {
        if (!leaving) handlers.onState('connected', {});
      });
      room.on(E.Disconnected, function (reason) {
        var name = reasonName(LK, reason);
        var wasJoined = joined;
        joined = false;
        if (leaving || name === 'CLIENT_INITIATED') return;
        if (name === 'DUPLICATE_IDENTITY') {
          // 同一 vid 的新 iframe 入房把这里顶下线：只结束自己
          handlers.onState('kicked', { vendor_reason: 'kick' });
          return;
        }
        if (name === 'ROOM_DELETED') {
          handlers.onState('kicked', { vendor_reason: 'room_disband' });
          return;
        }
        if (name === 'PARTICIPANT_REMOVED') {
          handlers.onState('kicked', { vendor_reason: 'banned' });
          return;
        }
        if (wasJoined) {
          // SDK 重连失败（多为旧 token 过期）：用最新凭证手动重新入房
          handlers.onState('reconnecting', { vendor_reason: name });
          handlers.onNeedRejoin();
        }
      });
      room.on(E.ConnectionQualityChanged, function (quality, participant) {
        if (participant && room && participant === room.localParticipant) {
          lastQuality = quality;
          // poor / lost 视同上行丢包超阈值（拥塞阶梯用）
          var bad = quality === 'poor' || quality === 'lost';
          handlers.onNetwork({ lossPct: bad ? 100 : 0 });
        }
      });
    }

    return {
      name: 'livekit',
      join: function (c) {
        creds = c;
        leaving = false;
        var v = vendorOf(c);
        if (!hostAllowed(v.url, c.allowed_hosts)) {
          return Promise.reject(Object.assign(new Error('host not allowed'), { errorCode: 'host_not_allowed' }));
        }
        if (room) {
          try {
            room.removeAllListeners();
          } catch (_) {
            // 旧房间对象
          }
        }
        room = newRoom(c);
        bind();
        handlers.onState('joining', {});
        return room.connect(v.url, v.token, { autoSubscribe: false }).then(function () {
          joined = true;
          room.remoteParticipants.forEach(function (p) {
            // 入房前就在的对端不会再触发 ParticipantConnected
            if (isPeer(p.identity)) handlers.onPeer('enter', p.identity, null);
            p.trackPublications.forEach(function (pub) {
              considerPublication(pub, p);
            });
          });
          handlers.onState('joined', {});
        });
      },
      updateCredentials: function (c) {
        creds = c;
      },
      canSend: function () {
        return joined && !leaving && !!room;
      },
      sendPiece: function (cmd, bytes, o) {
        var options = { reliable: cmd !== 3, topic: o.topic };
        if (o.peerVid) options.destinationIdentities = [o.peerVid];
        var sending = room.localParticipant.publishData(bytes, options);
        if (sending && typeof sending.catch === 'function') sending.catch(function () {});
      },
      publish: function (track) {
        if (!room || !joined) return Promise.resolve(false);
        if (publication && publishedTrack === track) return Promise.resolve(true);
        var before = publication ? room.localParticipant.unpublishTrack(publishedTrack, false) : Promise.resolve();
        return Promise.resolve(before).then(function () {
          return room.localParticipant.publishTrack(track, {
            source: LK.Track.Source.Camera,
            simulcast: false,
            videoCodec: codecPref(creds),
            scalabilityMode: 'L1T1',
            degradationPreference: 'maintain-framerate',
          });
        }).then(function (pub) {
          publication = pub;
          publishedTrack = track;
          return true;
        });
      },
      updateProfile: function () {
        // 同一条轨就地改尺寸后 LiveKit 照常编码新尺寸（T14 验证；不接受时退 replaceTrack）
        return Promise.resolve(true);
      },
      unpublish: function () {
        if (!room || !publication) return Promise.resolve();
        var track = publishedTrack;
        publication = null;
        publishedTrack = null;
        // stopOnUnpublish=false：画布轨留着复用
        return room.localParticipant.unpublishTrack(track, false).catch(function () {});
      },
      setPeerVid: function (vid) {
        peerVid = vid || peerVid;
      },
      setSubscribe: function (on) {
        subscribeWanted = !!on;
        if (remotePublication) remotePublication.setSubscribed(subscribeWanted);
        if (!subscribeWanted) handlers.onRemoteTrack(null);
        return Promise.resolve();
      },
      /** 离房；stopTracks=false，画布轨不随之结束。 */
      leave: function () {
        leaving = true;
        joined = false;
        publication = null;
        publishedTrack = null;
        remotePublication = null;
        if (!room) return Promise.resolve();
        return room.disconnect(false).catch(function () {});
      },
      destroy: function () {
        room = null;
      },
      remoteVideoAvailable: function () {
        return !!remotePublication;
      },
      stats: function () {
        var out = {};
        if (lastQuality) out.qlr_hint = String(lastQuality);
        return out;
      },
      senderStats: function () {
        var track = publication && publication.track;
        if (!track || typeof track.getSenderStats !== 'function') return Promise.resolve(null);
        return track.getSenderStats().then(function (list) {
          var s = Array.isArray(list) ? list[0] : list;
          if (!s) return null;
          return {
            enc_fps: s.framesPerSecond,
            tx_w: s.frameWidth,
            tx_h: s.frameHeight,
            qlr: s.qualityLimitationReason,
            enc: s.encoderImplementation,
          };
        }).catch(function () { return null; });
      },
    };
  }

  /** 能力门 ③：RTCRtpSender / Receiver 的编解码列表。 */
  function livekitCapabilities(LK) {
    var send = [];
    var recv = [];
    try {
      send = (root.RTCRtpSender.getCapabilities('video') || {}).codecs || [];
      recv = (root.RTCRtpReceiver.getCapabilities('video') || {}).codecs || [];
    } catch (_) {
      // 没有能力查询 = 没有可用编解码
    }
    function names(list) {
      var out = [];
      list.forEach(function (c) {
        var n = String(c.mimeType || '').toLowerCase().replace('video/', '');
        if ((n === 'vp9' || n === 'vp8' || n === 'h264') && out.indexOf(n) === -1) out.push(n);
      });
      return out;
    }
    var enc = names(send);
    var dec = names(recv);
    var supported = typeof LK.isBrowserSupported === 'function' ? LK.isBrowserSupported() : true;
    return Promise.resolve({
      transportOk: !!supported && typeof root.RTCPeerConnection === 'function',
      encode: enc.indexOf('vp9') !== -1 || enc.indexOf('vp8') !== -1,
      decode: dec.indexOf('vp9') !== -1 || dec.indexOf('vp8') !== -1,
      codecs: enc.concat(dec.filter(function (n) { return enc.indexOf(n) === -1; })),
    });
  }

  NVT.vendors = NVT.vendors || {};
  NVT.vendors.livekit = {
    globalName: 'LivekitClient',
    script: '/static/libs/livekit-client.umd.js',
    createAdapter: createLivekitAdapter,
    capabilities: livekitCapabilities,
    hostAllowed: hostAllowed,
  };
})(typeof window !== 'undefined' ? window : globalThis);
