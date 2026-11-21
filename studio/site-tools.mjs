const clip = {
  type: "object", additionalProperties: false, required: ["duration_frames"],
  properties: {
    job_id: { type: "string" },
    file_index: { type: "integer", minimum: 0 },
    gap: { type: "boolean" },
    start_frame: { type: "integer", minimum: 0 },
    duration_frames: { type: "integer", minimum: 1 },
    rate: { type: "string" },
  },
};

const cut = {
  type: "object", additionalProperties: false, required: ["tracks"],
  properties: {
    rate: { type: "string", description: "Project frame rate, for example 24 or 24000/1001" },
    tracks: { type: "array", items: {
      type: "object", additionalProperties: false, required: ["kind", "clips"],
      properties: {
        kind: { type: "string", enum: ["Video", "Audio"] },
        name: { type: "string" },
        clips: { type: "array", items: clip },
      },
    } },
  },
};

const supportsTools = (context) => typeof context?.registerTool === "function";

export async function registerSiteTools(document, navigator, { read, save, readPlan, savePlan, preflightShot, submitShot, readJob, readFrameNotes }) {
  const modern = document.modelContext;
  const context = supportsTools(modern) ? modern : navigator.modelContext;
  if (!supportsTools(context)) return null;

  // Older Chromium previews used the Navigator API and MCP content blocks.
  const reply = (result) => context === modern ? result : { content: [{ type: "text", text: JSON.stringify(result) }] };
  const execute = (handler) => async (args, options) => {
    try {
      return reply(await handler(args, options?.signal));
    } catch (error) {
      // The current browser API may discard exception messages; return the reason for a failed operation.
      if (options?.signal?.aborted) throw error;
      const result = { error: error.message || "Local Hub operation failed" };
      return context === modern ? result : { ...reply(result), isError: true };
    }
  };
  await context.registerTool({
    name: "localhub_read_project",
    description: "Read the project displayed in Local Hub Studio, including its shot candidates and saved cut revision.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    execute: execute((_input, signal) => read(signal)),
  });
  await context.registerTool({
    name: "localhub_save_cut",
    description: "Save a new cut revision for the project shown on this page. Read the project first. Preserves previous revisions and rejects an outdated revision or unsaved page edits.",
    inputSchema: {
      type: "object", additionalProperties: false, required: ["project", "expected_revision", "timeline"],
      properties: {
        project: { type: "string" },
        expected_revision: { type: "integer", minimum: 0 },
        timeline: cut,
      },
    },
    execute: execute(save),
  });
  if (readPlan && savePlan) {
    await context.registerTool({
      name: "localhub_read_plan",
      description: "Read the saved script, episodes, scenes, shots, references and selected clips in this project.",
      inputSchema: { type: "object", additionalProperties: false, properties: {
        revision: { type: "integer", minimum: 1, description: "An exact older revision, or omit to read the latest." },
      } },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: execute(readPlan),
    });
    await context.registerTool({
      name: "localhub_save_plan",
      description: "Save a project plan revision. Use the project, revision and plan object from localhub_read_plan.",
      inputSchema: { type: "object", additionalProperties: false, required: ["project", "expected_revision", "plan"], properties: {
        project: { type: "string" },
        expected_revision: { type: "integer", minimum: 0 },
        plan: { type: "object", description: "The updated plan including its saved episodes, scenes and shots." },
      } },
      execute: execute(savePlan),
    });
  }
  if (preflightShot && submitShot) {
    await context.registerTool({
      name: "localhub_preflight_shot",
      description: "Prepare a saved shot for H3 in the current project. The result has references, trim, seed and a submission key.",
      inputSchema: {
        type: "object", additionalProperties: false, required: ["revision", "shot_id"],
        properties: { revision: { type: "integer", minimum: 1 }, shot_id: { type: "string" },
          params: { type: "object", description: "Optional H3 parameters such as width, height, seed (a decimal string) and extra references." } },
      },
      execute: execute(preflightShot),
    });
    await context.registerTool({
      name: "localhub_submit_shot",
      description: "Generate the reviewed shot. Pass the exact submission from preflight. Keep that key to retry a lost receipt.",
      inputSchema: { type: "object", additionalProperties: false, required: ["submission"], properties: {
        submission: { type: "object", description: "The body and key returned by localhub_preflight_shot." },
      } },
      annotations: { consequentialHint: true },
      execute: execute(submitShot),
    });
  }
  if (readJob) {
    await context.registerTool({
      name: "localhub_read_job",
      description: "Get the recorded status and media of a job in the current project. Use its job ID.",
      inputSchema: { type: "object", additionalProperties: false, required: ["job_id"], properties: {
        job_id: { type: "string" },
      } },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: execute(readJob),
    });
  }
  if (readFrameNotes) {
    await context.registerTool({
      name: "localhub_read_frame_notes",
      description: "Read saved review notes for an exact candidate output in this project, including zero-based source frames and normalized regions [left, top, right, bottom].",
      inputSchema: { type: "object", additionalProperties: false, required: ["job_id", "file_index"],
        properties: { job_id: { type: "string" }, file_index: { type: "integer", minimum: 0 } } },
      annotations: { readOnlyHint: true, untrustedContentHint: true }, execute: execute(readFrameNotes),
    });
  }
  return context === modern ? "document" : "navigator";
}
