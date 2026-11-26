'use strict';

function parseTime(t) {
  const e = String(t || '').trim().split(':');
  return 3600 * parseFloat(e[0] || '0') + 60 * parseFloat(e[1] || '0') + parseFloat(e[2] || '0');
}

function isOomMessage(msg) {
  return /out of memory|memory access out of bounds|cannot enlarge memory|rangeerror|abort/i.test(String(msg || ''));
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};
  if (msg.type !== 'run') return;

  let { coreUrl, wasmUrl, fileBuffer, inName, outName, cmdArgs, fallbackCmdArgs, ext } = msg;
  let core = null;
  let dur = 0;
  let lastProgressAt = 0;
  let tailLogs = [];

  try {
    self.postMessage({ type: 'status', text: '# Loading AbuAsrh WASM core (~32MB)...' });
    if (typeof createFFmpegCore !== 'function') {
      importScripts(coreUrl);
    }
    core = await createFFmpegCore({
      locateFile: (f) => (f.endsWith('.wasm') ? wasmUrl : f),
    });

    core.setLogger(({ type, message }) => {
      if (type !== 'stderr' || !message) return;
      tailLogs.push(message);
      if (tailLogs.length > 25) tailLogs.shift();

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
            const pct = dur > 0 ? Math.min(99, Math.max(1, Math.round((cur / dur) * 100))) : 0;
            self.postMessage({ type: 'progress', pct, timeStr: m[1] });
          }
        }
      }
    });

    self.postMessage({ type: 'status', text: 'Core ready', level: 'success' });

    core.FS.writeFile(inName, new Uint8Array(fileBuffer));
    fileBuffer = null;

    self.postMessage({ type: 'status', text: '# Processing inside AbuAsrh Worker...' });
    let rc = core.exec(...cmdArgs);

    if (rc !== 0 && Array.isArray(fallbackCmdArgs) && fallbackCmdArgs.length > 0) {
      try { core.FS.unlink(outName); } catch (_) {}
      tailLogs = [];
      self.postMessage({ type: 'status', text: '# Retrying audio as AAC (video stream copy preserved)...' });
      rc = core.exec(...fallbackCmdArgs);
    }

    if (rc !== 0) {
      try { core.FS.unlink(inName); } catch (_) {}
      try { core.FS.unlink(outName); } catch (_) {}
      const detailText = tailLogs.slice(-8).join('\n');
      const hint = isOomMessage(detailText)
        ? ' (نفدت الذاكرة — جرّب خفض الدقة إلى 720p أو اختيار وضع سريع ومستقر)'
        : '';
      self.postMessage({
        type: 'error',
        message: 'Engine exited with code ' + rc + hint,
        details: detailText,
      });
      return;
    }

    try { core.FS.unlink(inName); } catch (_) {}
    const outU8 = core.FS.readFile(outName);
    try { core.FS.unlink(outName); } catch (_) {}
    const outBuf = (outU8.buffer !== core.HEAPU8.buffer && outU8.byteOffset === 0 && outU8.byteLength === outU8.buffer.byteLength)
      ? outU8.buffer
      : outU8.slice().buffer;
    try { core.exit && core.exit(); } catch (_) {}
    core = null;

    self.postMessage(
      { type: 'done', buffer: outBuf, ext },
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
      ? ' (نفدت ذاكرة المتصفح — جرّب دقة 720p أو وضع "سريع ومستقر")'
      : '';
    self.postMessage({
      type: 'error',
      message: errMsg + hint,
      details: tailLogs.slice(-8).join('\n'),
    });
  }
};
