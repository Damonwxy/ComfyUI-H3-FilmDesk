import assert from "node:assert/strict";
import { test } from "node:test";
import { matchedRange } from "../studio/compare-cut.mjs";

test("preserves source time and duration exactly across fractional frame rates", () => {
  const clip = { start_frame: 3, duration_frames: 9, rate: "24000/1001" };
  assert.deepEqual(matchedRange(clip, 24, { fps: "48000/1001", frames: 24 }),
    { start_frame: 6, duration_frames: 18, rate: "48000/1001" });
  assert.throws(() => matchedRange(clip, 24, { fps: 24, frames: 99 }), /精确对齐/);
  assert.throws(() => matchedRange(clip, 24, { fps: "48000/1001", frames: 23 }), /太短/);
  assert.deepEqual(matchedRange({ start_frame: 0, duration_frames: 10 }, 29.97,
    { fps: "2997/100", frames: 10 }), { start_frame: 0, duration_frames: 10, rate: "2997/100" });
});
