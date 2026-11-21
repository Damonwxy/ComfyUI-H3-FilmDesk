// An edit range is measured in source frames. Convert both boundaries without rounding.
function fraction(value) {
  const match = /^(\d+)(?:\.(\d+)|\/(\d+))?$/.exec(String(value));
  if (!match) throw new Error("无法核对素材帧率");
  const numerator = BigInt(match[1] + (match[2] || ""));
  const denominator = match[3] ? BigInt(match[3]) : 10n ** BigInt(match[2]?.length || 0);
  if (!numerator || !denominator) throw new Error("无法核对素材帧率");
  return [numerator, denominator];
}

export function matchedRange(clip, editRate, media) {
  if (!Number.isSafeInteger(clip.start_frame) || clip.start_frame < 0 ||
      !Number.isSafeInteger(clip.duration_frames) || clip.duration_frames < 1 ||
      !Number.isSafeInteger(media.frames) || media.frames < 1) throw new Error("请检查片段入点、时长和素材帧数");
  const [oldN, oldD] = fraction(clip.rate || editRate);
  const [newN, newD] = fraction(media.fps);
  const denominator = oldN * newD;
  function convert(frames) {
    const numerator = frames * newN * oldD;
    if (numerator % denominator) throw new Error("两版帧率无法精确对齐原切点，请在剪辑清单单独调整。");
    return numerator / denominator;
  }
  const start = convert(BigInt(clip.start_frame));
  const end = convert(BigInt(clip.start_frame) + BigInt(clip.duration_frames));
  if (end > BigInt(media.frames)) throw new Error("这版素材太短，无法覆盖原片段。");
  return { start_frame: Number(start), duration_frames: Number(end - start), rate: String(media.fps) };
}
