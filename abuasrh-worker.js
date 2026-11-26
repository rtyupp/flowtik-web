'use strict';

function parseTime(t) {
  const e = String(t || '').trim().split(':');
  return 3600 * parseFloat(e[0] || '0') + 60 * parseFloat(e[1] || '0') + parseFloat(e[2] || '0');
}

function isOomMessage(msg) {
  return /out of memory|memory access out of bounds|cannot enlarge memory|rangeerror|abort/i.test(String(msg || ''));
}

function detectHdrFromLogs(lines) {
  const joined = lines.join('\n');
  return /bt2020|arib-std-b67|smpte2084|yuv420p10le|p010le|dolbyvision/i.test(joined);
}

function buildFfmpegArgs(opts, hdrDetected) {
  const inName = opts.inName || 'in_video.mp4';
  const outName = opts.outName || 'out_video.mp4';
  const fpsNum = opts.fps === 60 ? 60 : 120;
  const slowMo = !!opts.slowMo;
  const casSharp = !!opts.casSharp;
  const reencode = !!(opts.reencode || casSharp);

  if (!reencode) {
    const mkCopy = (audioCopy) => {
      const args = [];
      if (slowMo) args.push('-itsscale', '2');
      args.push(
        '-i', inName,
        '-map', '0:v:0',
        '-map', '0:a?',
        '-c:v', 'copy',
        ...(audioCopy ? ['-c:a', 'copy'] : ['-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '192k']),
        '-video_track_timescale', fpsNum === 120 ? '120000' : '60000',
        '-color_primaries', 'bt709',
        '-color_trc', 'bt709',
        '-colorspace', 'bt709',
        '-color_range', 'tv',
        '-map_metadata', '-1',
        '-map_metadata:s:v', '-1',
        '-map_metadata:s:a', '-1',
        '-map_chapters', '-1',
        '-metadata', 'title=AbuAsrh',
        '-metadata', 'artist=AbuAsrh',
        '-metadata', `comment=AbuAsrh Studio ${fpsNum}FPS`,
        '-brand', 'isom',
        '-movflags', '+faststart',
        outName
      );
      return args;
    };
    return { primary: mkCopy(true), fallback: mkCopy(false), mode: 'copy' };
  }

  const vf = [
    "scale=w='if(gte(iw,ih),min(1920,iw),-2)':h='if(gte(iw,ih),-2,min(1920,ih))':flags=lanczos",
  ];
  if (hdrDetected) {
    vf.push('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p');
  }
  if (slowMo) {
    vf.push('setpts=2.0*PTS');
  }
  vf.push('setsar=1', `fps=${fpsNum}`);
  if (casSharp) {
    vf.push('cas=strength=0.35');
  }
  vf.push('setparams=range=limited:colorspace=bt709:color_trc=bt709:color_primaries=bt709');

  const level = fpsNum <= 60 ? '4.2' : '5.1';
  const maxrate = fpsNum <= 60 ? '12M' : '18M';
  const bufsize = fpsNum <= 60 ? '24M' : '36M';
  const gop = String(fpsNum * 2);
  const keyintMin = String(fpsNum);
  const timescale = fpsNum > 60 ? '120000' : '60000';

  const args = [
    '-i', inName,
    '-map', '0:v:0',
    '-map', '0:a?',
    '-vf', vf.join(','),
    '-pix_fmt', 'yuv420p',
    '-c:v', 'libx264',
    '-preset', 'superfast',
    '-profile:v', 'high',
    '-level:v', level,
    '-crf', '19',
    '-maxrate', maxrate,
    '-bufsize', bufsize,
    '-g', gop,
    '-keyint_min', keyintMin,
    '-sc_threshold', '0',
    '-fps_mode', 'cfr',
    '-r', String(fpsNum),
    '-threads', '1',
    '-x264-params', 'colorprim=bt709:transfer=bt709:colormatrix=bt709:open-gop=0:rc-lookahead=10:ref=2:bframes=0:sync-lookahead=0:threads=1',
    '-color_primaries', 'bt709',
    '-color_trc', 'bt709',
    '-colorspace', 'bt709',
    '-color_range', 'tv',
    '-video_track_timescale', timescale,
    '-map_metadata', '-1',
    '-map_metadata:s:v', '-1',
    '-map_metadata:s:a', '-1',
    '-map_chapters', '-1',
    '-metadata', 'title=AbuAsrh',
    '-metadata', 'artist=AbuAsrh',
  ];

  if (slowMo) {
    args.push('-af', 'atempo=0.5');
  }

  args.push(
    '-c:a', 'aac',
    '-ar', '48000',
    '-ac', '2',
    '-b:a', '192k',
    '-avoid_negative_ts', 'make_zero',
    '-brand', 'isom',
    '-movflags', '+faststart',
    outName
  );

  return { primary: args, fallback: null, mode: 'reencode' };
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};
  if (msg.type !== 'run') return;

  let { coreUrl, wasmUrl, patcherUrl, fileBuffer, inName, outName, options } = msg;
  const opts = Object.assign({ inName, outName, fps: 120, reencode: false, casSharp: false, autoHdr: true, slowMo: false, patchMultiplier: 10 }, options || {});
  let core = null;
  let dur = 0;
  let lastProgressAt = 0;
  let tailLogs = [];
  let hdrDetected = false;

  try {
    self.postMessage({ type: 'status', text: '# Loading AbuAsrh WASM core (~32MB)...' });
    if (typeof createFFmpegCore !== 'function') {
      importScripts(coreUrl);
    }
    if (opts.patchMultiplier > 1 && typeof self.patchMp4ForTikTok !== 'function' && patcherUrl) {
      importScripts(patcherUrl);
    }

    core = await createFFmpegCore({
      locateFile: (f) => (f.endsWith('.wasm') ? wasmUrl : f),
    });

    core.setLogger(({ type, message }) => {
      if (type !== 'stderr' || !message) return;
      tailLogs.push(message);
      if (tailLogs.length > 40) tailLogs.shift();

      if (message.startsWith('  Duration')) {
        const m = message.match(/Duration:\s*(\S+)/);
        if (m) dur = parseTime(m[1].replace(/,$/, ''));
      } else if (message.startsWith('frame=')) {
        const m = message.match(/time=(\S+)/);
        if (m) {
          const now = Date.now();
          if (now - lastProgressAt >= 200) {
            lastProgressAt = now;
            const cur = parseTime(m[1]);
            const pct = dur > 0 ? Math.min(96, Math.max(1, Math.round((cur / dur) * 95))) : 0;
            self.postMessage({ type: 'progress', pct, timeStr: m[1] });
          }
        }
      }
    });

    self.postMessage({ type: 'status', text: 'Core ready', level: 'success' });

    core.FS.writeFile(inName, new Uint8Array(fileBuffer));
    fileBuffer = null;

    if ((opts.reencode || opts.casSharp) && opts.autoHdr) {
      tailLogs = [];
      core.exec('-hide_banner', '-i', inName);
      hdrDetected = detectHdrFromLogs(tailLogs);
      if (hdrDetected) {
        self.postMessage({ type: 'status', text: '# HDR detected -> Tone-mapping to BT.709 SDR...' });
      }
      tailLogs = [];
    }

    const plan = buildFfmpegArgs(opts, hdrDetected);
    self.postMessage({
      type: 'status',
      text: plan.mode === 'copy'
        ? `# Stream Copy ${opts.fps} FPS (BT.709 · ${opts.fps === 120 ? '120K' : '60K'} timescale)...`
        : `# Pro CFR Encode ${opts.fps} FPS${opts.casSharp ? ' + CAS' : ''}${hdrDetected ? ' + HDR->SDR' : ''}...`,
    });

    let rc = core.exec(...plan.primary);
    if (rc !== 0 && Array.isArray(plan.fallback) && plan.fallback.length > 0) {
      try { core.FS.unlink(outName); } catch (_) {}
      tailLogs = [];
      self.postMessage({ type: 'status', text: '# Retrying audio as AAC 48kHz (video copy preserved)...' });
      rc = core.exec(...plan.fallback);
    }

    if (rc !== 0) {
      try { core.FS.unlink(inName); } catch (_) {}
      try { core.FS.unlink(outName); } catch (_) {}
      const detailText = tailLogs.slice(-8).join('\n');
      const hint = isOomMessage(detailText)
        ? ' (نفدت الذاكرة — جرّب وضع النسخ السريع بدون إعادة ترميز)'
        : '';
      self.postMessage({
        type: 'error',
        message: 'Engine exited with code ' + rc + hint,
        details: detailText,
      });
      return;
    }

    try { core.FS.unlink(inName); } catch (_) {}
    const rawU8 = core.FS.readFile(outName);
    try { core.FS.unlink(outName); } catch (_) {}

    // Copy out before freeing the WASM heap so Atom Patcher runs with minimal peak RAM
    let finalU8 = (rawU8.buffer !== core.HEAPU8.buffer && rawU8.byteOffset === 0 && rawU8.byteLength === rawU8.buffer.byteLength)
      ? rawU8
      : rawU8.slice();
    try { core.exit && core.exit(); } catch (_) {}
    core = null;

    let patchInfo = null;
    if (opts.patchMultiplier > 1 && typeof self.patchMp4ForTikTok === 'function') {
      self.postMessage({ type: 'status', text: `# Patching MP4 Atoms (${opts.patchMultiplier}x TikTok FPS protection)...` });
      self.postMessage({ type: 'progress', pct: 98, timeStr: 'MP4 Atom' });
      const res = self.patchMp4ForTikTok(finalU8, opts.patchMultiplier);
      if (res && res.bytes) {
        finalU8 = res.bytes;
        patchInfo = {
          originalFrames: res.originalFrames,
          declaredFrames: res.declaredFrames,
          multiplier: res.multiplier,
          codec: res.codec,
        };
        if (res.originalFrames > 0) {
          self.postMessage({
            type: 'status',
            text: `# MP4 Atom Patch: ${res.originalFrames} -> ${res.declaredFrames} frames (${res.multiplier}x · ${res.codec})`,
            level: 'success',
          });
        }
      }
    }

    const outBuf = (finalU8.byteOffset === 0 && finalU8.byteLength === finalU8.buffer.byteLength)
      ? finalU8.buffer
      : finalU8.slice().buffer;

    self.postMessage(
      { type: 'done', buffer: outBuf, ext: 'mp4', patchInfo },
      [outBuf]
    );
  } catch (err) {
    try {
      if (core) {
        try { core.FS.unlink(inName); } catch (_) {}
        try { core.FS.unlink(outName); } catch (_) {}
        try { core.exit && core.exit(); } catch (_) {}
      }
    } catch (_) {}
    const errMsg = err && err.message ? err.message : String(err);
    const hint = isOomMessage(errMsg)
      ? ' (نفدت ذاكرة المتصفح — جرّب إغلاق التبويبات أو استخدام النسخ السريع)'
      : '';
    self.postMessage({
      type: 'error',
      message: errMsg + hint,
      details: tailLogs.slice(-8).join('\n'),
    });
  }
};
