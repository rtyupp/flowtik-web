(function (root) {
  'use strict';

  function readU32(u8, off) {
    return (
      ((u8[off] << 24) | (u8[off + 1] << 16) | (u8[off + 2] << 8) | u8[off + 3]) >>> 0
    );
  }

  function writeU32(u8, off, val) {
    const v = val >>> 0;
    u8[off] = (v >>> 24) & 0xff;
    u8[off + 1] = (v >>> 16) & 0xff;
    u8[off + 2] = (v >>> 8) & 0xff;
    u8[off + 3] = v & 0xff;
  }

  function readU64(u8, off) {
    const hi = readU32(u8, off);
    const lo = readU32(u8, off + 4);
    return hi * 4294967296 + lo;
  }

  function writeU64(u8, off, val) {
    const hi = Math.floor(val / 4294967296) >>> 0;
    const lo = (val % 4294967296) >>> 0;
    writeU32(u8, off, hi);
    writeU32(u8, off + 4, lo);
  }

  function fourcc(u8, off) {
    return String.fromCharCode(u8[off], u8[off + 1], u8[off + 2], u8[off + 3]);
  }

  function writeFourcc(u8, off, s) {
    u8[off] = s.charCodeAt(0);
    u8[off + 1] = s.charCodeAt(1);
    u8[off + 2] = s.charCodeAt(2);
    u8[off + 3] = s.charCodeAt(3);
  }

  function parseBoxes(u8, start, end) {
    const boxes = [];
    let pos = start;
    while (pos + 8 <= end) {
      const size32 = readU32(u8, pos);
      const type = fourcc(u8, pos + 4);
      let headerSize = 8;
      let boxSize = size32;
      if (size32 === 1) {
        if (pos + 16 > end) break;
        headerSize = 16;
        boxSize = readU64(u8, pos + 8);
      } else if (size32 === 0) {
        boxSize = end - pos;
      }
      if (boxSize < headerSize || pos + boxSize > end) break;
      boxes.push({
        type,
        start: pos,
        end: pos + boxSize,
        headerSize,
        payloadStart: pos + headerSize,
        size: boxSize,
      });
      pos += boxSize;
    }
    return boxes;
  }

  function makeBox(type, payloadChunks) {
    let payloadLen = 0;
    for (let i = 0; i < payloadChunks.length; i++) {
      payloadLen += payloadChunks[i].length;
    }
    const useLarge = payloadLen + 8 > 0xffffffff;
    const headerSize = useLarge ? 16 : 8;
    const total = headerSize + payloadLen;
    const out = new Uint8Array(total);
    if (useLarge) {
      writeU32(out, 0, 1);
      writeFourcc(out, 4, type);
      writeU64(out, 8, total);
    } else {
      writeU32(out, 0, total);
      writeFourcc(out, 4, type);
    }
    let off = headerSize;
    for (let i = 0; i < payloadChunks.length; i++) {
      out.set(payloadChunks[i], off);
      off += payloadChunks[i].length;
    }
    return out;
  }

  function makeIsomFtypBox() {
    // 32-byte standard ftyp: isom / minor 512 / compatible [isom, iso2, avc1, mp41]
    const p = new Uint8Array(24);
    writeFourcc(p, 0, 'isom');
    writeU32(p, 4, 512);
    writeFourcc(p, 8, 'isom');
    writeFourcc(p, 12, 'iso2');
    writeFourcc(p, 16, 'avc1');
    writeFourcc(p, 20, 'mp41');
    return makeBox('ftyp', [p]);
  }

  function concatU8(chunks) {
    let total = 0;
    for (let i = 0; i < chunks.length; i++) total += chunks[i].length;
    const out = new Uint8Array(total);
    let off = 0;
    for (let i = 0; i < chunks.length; i++) {
      out.set(chunks[i], off);
      off += chunks[i].length;
    }
    return out;
  }

  function detectVideoCodec(stsdPayload) {
    if (!stsdPayload || stsdPayload.length < 16) return 'avc1';
    const entryType = fourcc(stsdPayload, 12);
    if (entryType === 'hvc1' || entryType === 'hev1') return entryType;
    return entryType || 'avc1';
  }

  function getMdhdTimescale(mdhdPayload) {
    if (!mdhdPayload || mdhdPayload.length < 24) return 60000;
    const ver = mdhdPayload[0];
    return ver === 1 ? readU32(mdhdPayload, 20) : readU32(mdhdPayload, 12);
  }

  function addMdhdDuration(mdhdPayload, addTicks) {
    const out = mdhdPayload.slice();
    const ver = out[0];
    if (ver === 1) {
      const cur = readU64(out, 24);
      writeU64(out, 24, cur + addTicks);
    } else {
      const cur = readU32(out, 16);
      writeU32(out, 16, Math.min(0xffffffff, cur + addTicks));
    }
    return out;
  }

  function getMvhdTimescaleAndDur(mvhdPayload) {
    if (!mvhdPayload || mvhdPayload.length < 24) return { timescale: 1000, duration: 0 };
    const ver = mvhdPayload[0];
    if (ver === 1) {
      return { timescale: readU32(mvhdPayload, 20), duration: readU64(mvhdPayload, 24) };
    }
    return { timescale: readU32(mvhdPayload, 12), duration: readU32(mvhdPayload, 16) };
  }

  function updateMvhdDuration(mvhdPayload, newDur) {
    const out = mvhdPayload.slice();
    const ver = out[0];
    if (ver === 1) {
      if (newDur > readU64(out, 24)) writeU64(out, 24, newDur);
    } else {
      const clamped = Math.min(0xffffffff, newDur);
      if (clamped > readU32(out, 16)) writeU32(out, 16, clamped);
    }
    return out;
  }

  function getTkhdDuration(tkhdPayload) {
    const ver = tkhdPayload[0];
    return ver === 1 ? readU64(tkhdPayload, 28) : readU32(tkhdPayload, 20);
  }

  function updateTkhdDuration(tkhdPayload, addTicks) {
    const out = tkhdPayload.slice();
    const ver = out[0];
    if (ver === 1) {
      const next = readU64(out, 28) + addTicks;
      writeU64(out, 28, next);
      return { bytes: out, duration: next };
    }
    const next = Math.min(0xffffffff, readU32(out, 20) + addTicks);
    writeU32(out, 20, next);
    return { bytes: out, duration: next };
  }

  function makeEdtsBox(segmentDuration) {
    const p = new Uint8Array(20);
    writeU32(p, 4, 1);
    writeU32(p, 8, Math.min(0xffffffff, segmentDuration));
    writeU32(p, 12, 0);
    p[16] = 0x00; p[17] = 0x01;
    p[18] = 0x00; p[19] = 0x00;
    return makeBox('edts', [makeBox('elst', [p])]);
  }

  function patchVideoStbl(u8, stblBox, ghostCount) {
    const children = parseBoxes(u8, stblBox.payloadStart, stblBox.end);
    const stcoBox = children.find((b) => b.type === 'stco' || b.type === 'co64');
    const oldChunkCount = stcoBox ? readU32(u8, stcoBox.payloadStart + 4) : 0;
    const outChildren = [];

    for (let i = 0; i < children.length; i++) {
      const child = children[i];
      const payload = u8.subarray(child.payloadStart, child.end);

      if (child.type === 'stsz') {
        const sampleSize = readU32(payload, 4);
        const origCount = readU32(payload, 8);
        if (sampleSize === 0) {
          const newPayload = new Uint8Array(12 + (origCount + ghostCount) * 4);
          newPayload.set(payload.subarray(0, 12 + origCount * 4), 0);
          writeU32(newPayload, 8, origCount + ghostCount);
          for (let g = 0; g < ghostCount; g++) {
            writeU32(newPayload, 12 + (origCount + g) * 4, 8);
          }
          outChildren.push(makeBox('stsz', [newPayload]));
        } else {
          outChildren.push(u8.subarray(child.start, child.end));
        }
      } else if (child.type === 'stts') {
        const entryCount = readU32(payload, 4);
        const newPayload = new Uint8Array(8 + (entryCount + 1) * 8);
        newPayload.set(payload.subarray(0, 8 + entryCount * 8), 0);
        writeU32(newPayload, 4, entryCount + 1);
        writeU32(newPayload, 8 + entryCount * 8, ghostCount);
        writeU32(newPayload, 8 + entryCount * 8 + 4, 1);
        outChildren.push(makeBox('stts', [newPayload]));
      } else if (child.type === 'ctts') {
        const entryCount = readU32(payload, 4);
        const newPayload = new Uint8Array(8 + (entryCount + 1) * 8);
        newPayload.set(payload.subarray(0, 8 + entryCount * 8), 0);
        writeU32(newPayload, 4, entryCount + 1);
        writeU32(newPayload, 8 + entryCount * 8, ghostCount);
        writeU32(newPayload, 8 + entryCount * 8 + 4, 0);
        outChildren.push(makeBox('ctts', [newPayload]));
      } else if (child.type === 'stsc') {
        const entryCount = readU32(payload, 4);
        const newPayload = new Uint8Array(8 + (entryCount + 1) * 12);
        newPayload.set(payload.subarray(0, 8 + entryCount * 12), 0);
        writeU32(newPayload, 4, entryCount + 1);
        const tail = 8 + entryCount * 12;
        writeU32(newPayload, tail, oldChunkCount + 1);
        writeU32(newPayload, tail + 4, 1);
        writeU32(newPayload, tail + 8, 1);
        outChildren.push(makeBox('stsc', [newPayload]));
      } else if (child.type === 'stco' || child.type === 'co64') {
        const is64 = child.type === 'co64';
        const entryBytes = is64 ? 8 : 4;
        const entryCount = readU32(payload, 4);
        const newPayload = new Uint8Array(8 + (entryCount + ghostCount) * entryBytes);
        newPayload.set(payload.subarray(0, 8 + entryCount * entryBytes), 0);
        writeU32(newPayload, 4, entryCount + ghostCount);
        outChildren.push(makeBox(child.type, [newPayload]));
      } else {
        outChildren.push(u8.subarray(child.start, child.end));
      }
    }
    return makeBox('stbl', outChildren);
  }

  function applyChunkOffsetFixupsInMoov(moovBoxBytes, ghostCount, delta, fillerFileOffset) {
    const moovBoxes = parseBoxes(moovBoxBytes, 0, moovBoxBytes.length);
    if (!moovBoxes.length) return;
    const moov = moovBoxes[0];
    const moovKids = parseBoxes(moovBoxBytes, moov.payloadStart, moov.end);
    for (let i = 0; i < moovKids.length; i++) {
      const tb = moovKids[i];
      if (tb.type !== 'trak') continue;
      const tKids = parseBoxes(moovBoxBytes, tb.payloadStart, tb.end);
      const mdia = tKids.find((b) => b.type === 'mdia');
      if (!mdia) continue;
      const mKids = parseBoxes(moovBoxBytes, mdia.payloadStart, mdia.end);
      const hdlr = mKids.find((b) => b.type === 'hdlr');
      const isVideo =
        hdlr && hdlr.end - hdlr.payloadStart >= 12 && fourcc(moovBoxBytes, hdlr.payloadStart + 8) === 'vide';
      const minf = mKids.find((b) => b.type === 'minf');
      if (!minf) continue;
      const mfKids = parseBoxes(moovBoxBytes, minf.payloadStart, minf.end);
      const stbl = mfKids.find((b) => b.type === 'stbl');
      if (!stbl) continue;
      const stKids = parseBoxes(moovBoxBytes, stbl.payloadStart, stbl.end);
      for (let j = 0; j < stKids.length; j++) {
        const c = stKids[j];
        if (c.type !== 'stco' && c.type !== 'co64') continue;
        const is64 = c.type === 'co64';
        const totalEntries = readU32(moovBoxBytes, c.payloadStart + 4);
        const origCount = isVideo ? Math.max(0, totalEntries - ghostCount) : totalEntries;
        const tableStart = c.payloadStart + 8;
        if (is64) {
          for (let idx = 0; idx < origCount; idx++) {
            const p = tableStart + idx * 8;
            writeU64(moovBoxBytes, p, readU64(moovBoxBytes, p) + delta);
          }
          if (isVideo) {
            for (let g = 0; g < ghostCount; g++) {
              writeU64(moovBoxBytes, tableStart + (origCount + g) * 8, fillerFileOffset);
            }
          }
        } else {
          for (let idx = 0; idx < origCount; idx++) {
            const p = tableStart + idx * 4;
            writeU32(moovBoxBytes, p, readU32(moovBoxBytes, p) + delta);
          }
          if (isVideo) {
            for (let g = 0; g < ghostCount; g++) {
              writeU32(moovBoxBytes, tableStart + (origCount + g) * 4, fillerFileOffset);
            }
          }
        }
      }
    }
  }

  /**
   * Patches an MP4 container using the isom + 8-byte reusable filler block technique:
   * normalizes ftyp=isom, places moov before mdat, inflates stsz/stts/stsc/stco/ctts
   * without altering visual pixels or inflating declared bit_rate, and preserves exact
   * playback duration via edts/elst.
   * @param {Uint8Array} mp4Bytes
   * @param {number} multiplier
   */
  function patchMp4ForTikTok(mp4Bytes, multiplier) {
    const mult = Math.max(1, Math.min(15, Math.round(Number(multiplier) || 1)));
    if (mult <= 1 || !mp4Bytes || mp4Bytes.length < 32) {
      return { bytes: mp4Bytes, originalFrames: 0, declaredFrames: 0, multiplier: 1, codec: 'none' };
    }

    const topBoxes = parseBoxes(mp4Bytes, 0, mp4Bytes.length);
    const moovBox = topBoxes.find((b) => b.type === 'moov');
    const mdatBox = topBoxes.find((b) => b.type === 'mdat');
    if (!moovBox || !mdatBox) {
      return { bytes: mp4Bytes, originalFrames: 0, declaredFrames: 0, multiplier: 1, codec: 'none' };
    }

    const moovChildren = parseBoxes(mp4Bytes, moovBox.payloadStart, moovBox.end);
    const mvhdBox = moovChildren.find((b) => b.type === 'mvhd');
    const mvhdInfo = mvhdBox
      ? getMvhdTimescaleAndDur(mp4Bytes.subarray(mvhdBox.payloadStart, mvhdBox.end))
      : { timescale: 1000, duration: 0 };

    let videoTrak = null;
    let videoMdhdBox = null;
    let videoStsdBox = null;
    let videoStszBox = null;

    for (let i = 0; i < moovChildren.length; i++) {
      const tb = moovChildren[i];
      if (tb.type !== 'trak') continue;
      const tKids = parseBoxes(mp4Bytes, tb.payloadStart, tb.end);
      const mdia = tKids.find((b) => b.type === 'mdia');
      if (!mdia) continue;
      const mKids = parseBoxes(mp4Bytes, mdia.payloadStart, mdia.end);
      const hdlr = mKids.find((b) => b.type === 'hdlr');
      if (!hdlr || hdlr.end - hdlr.payloadStart < 12) continue;
      if (fourcc(mp4Bytes, hdlr.payloadStart + 8) !== 'vide') continue;

      const minf = mKids.find((b) => b.type === 'minf');
      if (!minf) continue;
      const minfKids = parseBoxes(mp4Bytes, minf.payloadStart, minf.end);
      const stbl = minfKids.find((b) => b.type === 'stbl');
      if (!stbl) continue;
      const stblKids = parseBoxes(mp4Bytes, stbl.payloadStart, stbl.end);
      videoTrak = tb;
      videoMdhdBox = mKids.find((b) => b.type === 'mdhd');
      videoStsdBox = stblKids.find((b) => b.type === 'stsd');
      videoStszBox = stblKids.find((b) => b.type === 'stsz');
      break;
    }

    if (!videoTrak || !videoStszBox) {
      return { bytes: mp4Bytes, originalFrames: 0, declaredFrames: 0, multiplier: 1, codec: 'none' };
    }

    const origFrames = readU32(mp4Bytes, videoStszBox.payloadStart + 8);
    if (origFrames <= 0) {
      return { bytes: mp4Bytes, originalFrames: 0, declaredFrames: 0, multiplier: 1, codec: 'none' };
    }

    const mdhdTs = videoMdhdBox
      ? getMdhdTimescale(mp4Bytes.subarray(videoMdhdBox.payloadStart, videoMdhdBox.end))
      : 60000;
    let ghostCount = origFrames * (mult - 1);
    const maxGhostByDuration = Math.max(1, Math.floor(mdhdTs * 0.5));
    if (ghostCount > maxGhostByDuration) {
      ghostCount = maxGhostByDuration;
    }

    const codec = videoStsdBox
      ? detectVideoCodec(mp4Bytes.subarray(videoStsdBox.payloadStart, videoStsdBox.end))
      : 'avc1';

    const fillerBlock = new Uint8Array([0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00]);
    const addMvhdTicks = Math.max(1, Math.ceil((ghostCount * mvhdInfo.timescale) / Math.max(1, mdhdTs)));
    let maxVideoTkhdDur = mvhdInfo.duration;

    const newMoovChildren = [];
    for (let i = 0; i < moovChildren.length; i++) {
      const child = moovChildren[i];
      if (child === videoTrak) {
        const tKids = parseBoxes(mp4Bytes, child.payloadStart, child.end);
        const hasEdts = tKids.some((b) => b.type === 'edts');
        const newTKids = [];
        for (let j = 0; j < tKids.length; j++) {
          const tk = tKids[j];
          if (tk.type === 'tkhd') {
            const tkPayload = mp4Bytes.subarray(tk.payloadStart, tk.end);
            const origTkhdDur = getTkhdDuration(tkPayload);
            const upd = updateTkhdDuration(tkPayload, addMvhdTicks);
            if (upd.duration > maxVideoTkhdDur) maxVideoTkhdDur = upd.duration;
            newTKids.push(makeBox('tkhd', [upd.bytes]));
            if (!hasEdts && origTkhdDur > 0) {
              newTKids.push(makeEdtsBox(origTkhdDur));
            }
          } else if (tk.type === 'mdia') {
            const mKids = parseBoxes(mp4Bytes, tk.payloadStart, tk.end);
            const newMKids = [];
            for (let k = 0; k < mKids.length; k++) {
              const mk = mKids[k];
              if (mk.type === 'mdhd') {
                const newMdhd = addMdhdDuration(mp4Bytes.subarray(mk.payloadStart, mk.end), ghostCount);
                newMKids.push(makeBox('mdhd', [newMdhd]));
              } else if (mk.type === 'minf') {
                const mfKids = parseBoxes(mp4Bytes, mk.payloadStart, mk.end);
                const newMfKids = [];
                for (let m = 0; m < mfKids.length; m++) {
                  const mfk = mfKids[m];
                  if (mfk.type === 'stbl') {
                    newMfKids.push(patchVideoStbl(mp4Bytes, mfk, ghostCount));
                  } else {
                    newMfKids.push(mp4Bytes.subarray(mfk.start, mfk.end));
                  }
                }
                newMKids.push(makeBox('minf', newMfKids));
              } else {
                newMKids.push(mp4Bytes.subarray(mk.start, mk.end));
              }
            }
            newTKids.push(makeBox('mdia', newMKids));
          } else {
            newTKids.push(mp4Bytes.subarray(tk.start, tk.end));
          }
        }
        newMoovChildren.push(makeBox('trak', newTKids));
      } else if (child.type === 'trak') {
        newMoovChildren.push(mp4Bytes.subarray(child.start, child.end));
      } else if (child.type === 'mvhd') {
        const newMvhd = updateMvhdDuration(mp4Bytes.subarray(child.payloadStart, child.end), maxVideoTkhdDur);
        newMoovChildren.push(makeBox('mvhd', [newMvhd]));
      } else {
        newMoovChildren.push(mp4Bytes.subarray(child.start, child.end));
      }
    }

    const newMoovBox = makeBox('moov', newMoovChildren);

    const beforeBoxes = [makeIsomFtypBox()];
    for (let i = 0; i < topBoxes.length; i++) {
      const tb = topBoxes[i];
      if (tb.type === 'ftyp' || tb.type === 'moov' || tb.type === 'mdat' || tb.type === 'free') continue;
      beforeBoxes.push(mp4Bytes.subarray(tb.start, tb.end));
    }
    let preMoovLen = 0;
    for (let i = 0; i < beforeBoxes.length; i++) preMoovLen += beforeBoxes[i].length;

    const oldMdatDataOffset = mdatBox.payloadStart;
    const oldMdatPayloadLen = mdatBox.end - mdatBox.payloadStart;
    const newMdatPayloadLen = oldMdatPayloadLen + fillerBlock.length;
    const newMdatHeaderSize = newMdatPayloadLen + 8 > 0xffffffff ? 16 : 8;
    const newMdatStart = preMoovLen + newMoovBox.length;
    const newMdatDataOffset = newMdatStart + newMdatHeaderSize;
    const delta = newMdatDataOffset - oldMdatDataOffset;
    const fillerFileOffset = newMdatDataOffset + oldMdatPayloadLen;

    applyChunkOffsetFixupsInMoov(newMoovBox, ghostCount, delta, fillerFileOffset);

    const mdatHeader = new Uint8Array(newMdatHeaderSize);
    if (newMdatHeaderSize === 16) {
      writeU32(mdatHeader, 0, 1);
      writeFourcc(mdatHeader, 4, 'mdat');
      writeU64(mdatHeader, 8, newMdatHeaderSize + newMdatPayloadLen);
    } else {
      writeU32(mdatHeader, 0, newMdatHeaderSize + newMdatPayloadLen);
      writeFourcc(mdatHeader, 4, 'mdat');
    }

    const outBytes = concatU8([
      ...beforeBoxes,
      newMoovBox,
      mdatHeader,
      mp4Bytes.subarray(mdatBox.payloadStart, mdatBox.end),
      fillerBlock,
    ]);

    return {
      bytes: outBytes,
      originalFrames: origFrames,
      declaredFrames: origFrames + ghostCount,
      ghostFrames: ghostCount,
      multiplier: mult,
      codec,
    };
  }

  root.patchMp4ForTikTok = patchMp4ForTikTok;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { patchMp4ForTikTok };
  }
})(typeof self !== 'undefined' ? self : globalThis);
