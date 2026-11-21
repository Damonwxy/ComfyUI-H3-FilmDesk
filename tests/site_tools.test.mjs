import { test } from "node:test";
import assert from "node:assert/strict";
import { registerSiteTools } from "../studio/site-tools.mjs";

test("registers current document site tools and passes results and cancellation through", async () => {
  const registrations = new Map();
  const controller = new AbortController();
  const board = { project: "Fixture", revision: 3, timeline: { tracks: [] } };
  const next = { project: board.project, expected_revision: 3, timeline: { tracks: [] } };
  const document = { modelContext: { async registerTool(tool) { registrations.set(tool.name, tool); } } };
  const navigator = { modelContext: { registerTool() { assert.fail("Must prefer the documented API"); } } };
  const result = await registerSiteTools(document, navigator, {
    async read(signal) { assert.equal(signal, controller.signal); return board; },
    async save(args, signal) {
      assert.equal(signal, controller.signal);
      if (args.expected_revision !== 3) throw new Error("stale revision");
      assert.deepEqual(args, next);
      return { project: args.project, revision: 4 };
    },
  });
  assert.equal(result, "document");
  const read = registrations.get("localhub_read_project");
  const save = registrations.get("localhub_save_cut");
  assert.equal(read.annotations.readOnlyHint, true);
  assert.deepEqual(await read.execute({}, { signal: controller.signal }), board);
  assert.deepEqual(await save.execute(next, { signal: controller.signal }), { project: "Fixture", revision: 4 });
  assert.deepEqual(await save.execute({ ...next, expected_revision: 2 }, { signal: controller.signal }),
    { error: "stale revision" });
  controller.abort();
  await assert.rejects(save.execute({ ...next, expected_revision: 2 }, { signal: controller.signal }), /stale revision/);
});

test("awaits async failures and leaves browsers without site tools usable", async () => {
  assert.equal(await registerSiteTools({}, {}, {}), null);
  await assert.rejects(registerSiteTools({ modelContext: { async registerTool(tool) {
    if (tool.name === "localhub_save_cut") throw new Error("unsupported schema");
  } } }, {}, {}), /unsupported schema/);
});

test("discovers project plan tools and preserves the requested history revision", async () => {
  const tools = new Map();
  const document = { modelContext: { registerTool(tool) { tools.set(tool.name, tool); } } };
  const old = { project: "series", revision: 1, latest_revision: 2, plan: { title: "先确认灯" } };
  await registerSiteTools(document, {}, {
    async readPlan(args) {
      assert.deepEqual(args, { revision: 1 });
      return old;
    },
    async savePlan(args) {
      assert.equal(args.project, "series");
      if (args.expected_revision !== 2) throw new Error("stale plan");
      return { ...old, revision: 3, latest_revision: 3, plan: args.plan };
    },
  });
  assert.deepEqual(await tools.get("localhub_read_plan").execute({ revision: 1 }), old);
  const args = { project: old.project, expected_revision: 2, plan: { title: "回到工作间" } };
  assert.deepEqual(await tools.get("localhub_save_plan").execute(args),
    { project: old.project, revision: 3, latest_revision: 3, plan: args.plan });
  assert.deepEqual(await tools.get("localhub_save_plan").execute({ ...args, expected_revision: 1 }), { error: "stale plan" });
});

test("keeps the prepared H3 body and request key across site tool calls", async () => {
  const tools = new Map();
  const signal = new AbortController().signal;
  const prepared = { key: "same-h3-request", body: { project: "series", plan_shot: { revision: 4, shot_id: "S03" } } };
  const document = { modelContext: { registerTool(tool) { tools.set(tool.name, tool); } } };
  await registerSiteTools(document, {}, {
    async preflightShot(args, passed) {
      assert.equal(passed, signal);
      assert.deepEqual(args, { revision: 4, shot_id: "S03" });
      return { submission: prepared };
    },
    async submitShot({ submission }, passed) {
      assert.equal(passed, signal);
      assert.deepEqual(submission, prepared);
      return { job: { id: "stable-job" } };
    },
    async readJob({ job_id }) {
      assert.equal(job_id, "stable-job");
      return { job: { id: job_id, state: "completed" } };
    },
  });
  const { submission } = await tools.get("localhub_preflight_shot").execute({ revision: 4, shot_id: "S03" }, { signal });
  const submit = tools.get("localhub_submit_shot");
  assert.equal(submit.annotations.consequentialHint, true);
  for (let attempt = 0; attempt < 2; attempt++) {
    const { job } = await submit.execute({ submission }, { signal });
    const result = await tools.get("localhub_read_job").execute({ job_id: job.id });
    assert.equal(result.job.state, "completed");
  }
});

test("offline review keeps job lookup available while generation tools are absent", async () => {
  const tools = new Map();
  const document = { modelContext: { registerTool(tool) { tools.set(tool.name, tool); } } };
  await registerSiteTools(document, {}, { readJob: ({ job_id }) => ({ state: "completed", id: job_id }) });
  assert.ok(tools.has("localhub_read_job"));
  assert.ok(!tools.has("localhub_preflight_shot"));
  assert.ok(!tools.has("localhub_submit_shot"));
});

test("the browser can discover all tools and read the displayed master frame", async () => {
  const tools = new Map();
  const context = { registerTool(tool) {
    assert.equal(typeof tool.annotations?.readOnlyHint === "boolean" || tool.annotations?.consequentialHint === true, true, tool.name);
    tools.set(tool.name, tool);
  } };
  const review = { displayed: {frame: 73, region: [0.5, 0.5, 0.55, 0.55]} };
  const noop = () => {};
  await registerSiteTools({ modelContext: context }, {}, { read: noop, save: noop, readPlan: noop, savePlan: noop,
    preflightShot: noop, submitShot: noop, readJob: noop, readFrameNotes: noop, readMasterReview: () => review });
  assert.deepEqual(await tools.get("localhub_read_master_review").execute({}), review);
  assert.equal(tools.get("localhub_save_plan").annotations.readOnlyHint, false);
  assert.equal(tools.get("localhub_save_cut").annotations.readOnlyHint, false);
});
