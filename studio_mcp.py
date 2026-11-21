"""Optional Codex MCP entry point for the local studio."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel

from localhub_project_plan import plan_cut, read_project_plan, save_project_plan
from localhub_storage import ProjectDatabase


mcp = FastMCP("Local Hub Studio")
base = os.environ.get("LOCALHUB_URL", "http://127.0.0.1:8188").rstrip("/")
database = os.environ.get("LOCALHUB_DATABASE")
url = urlsplit(base)
if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1", "localhost")
        or url.username is not None or url.path or url.query or url.fragment):
    raise ValueError("LOCALHUB_URL must be an HTTP loopback origin")

http = build_opener(ProxyHandler({}))


class CutRevision(BaseModel):
    project: str
    revision: int


class ProjectBoard(CutRevision):
    studio: str
    timeline: dict
    jobs: list[dict]


def bridge(path, project, value=None, *, method="PUT", headers=None, timeout=10, **query):
    target = f"{base}/localhub/v2/{path}?{urlencode({'project': project, **query})}"
    req = Request(target)
    if value is not None:
        req = Request(target, data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
                      method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with http.open(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        error = f"Bridge returned HTTP {exc.code}"
        if exc.headers.get_content_type() == "application/json":
            error = json.load(exc).get("error") or error
        raise ValueError(error) from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError("本机生成服务未响应。若刚提交过，请使用原请求键重试") from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, untrustedContentHint=True))
def localhub_find_projects(query: str = "") -> dict:
    """Find existing projects by name, title, genre or brief. Empty query lists the library.

    When several projects fit the user's request, present distinguishing details and ask which
    one to use before changing anything. A search does not select or create a project.
    """
    if database:
        from localhub_projects import find_projects
        return find_projects(ProjectDatabase(database), query)
    return bridge("projects", "", q=query)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def localhub_read_project(project: str = "本地项目", revision: int | None = None) -> ProjectBoard:
    """Read current shot candidates alongside a saved cut. Omit revision for the latest cut."""
    jobs = bridge("jobs", project)["jobs"]
    edit = bridge("timeline", project, **({"revision": revision} if revision is not None else {}))
    return ProjectBoard(**{
        "project": project,
        "studio": f"{base}/localhub/studio?{urlencode({'project': project})}",
        "revision": edit["revision"],
        "timeline": edit["timeline"],
        "jobs": [{
            **{key: job.get(key) for key in ("id", "shot", "kind", "source", "state", "delivery", "outputs")},
            "frames": job["info"].get("frames"),
            "fps": job["info"].get("fps"),
        } for job in jobs],
    })


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def localhub_save_cut(project: str, expected_revision: int, timeline: dict) -> CutRevision:
    """Save a new cut revision. Use the revision and track/clip structure from localhub_read_project."""
    edit = bridge("timeline", project, {"expected_revision": expected_revision, "timeline": timeline})
    return CutRevision(project=project, revision=edit["revision"])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def localhub_read_plan(project: str, revision: int | None = None) -> dict:
    """Read a film, ad or series plan and its exact script, scenes, shot references and selections."""
    if database:
        return read_project_plan(ProjectDatabase(database), project, revision)
    return bridge("plan", project, **({"revision": revision} if revision is not None else {}))


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
def localhub_save_plan(project: str, expected_revision: int, plan: dict) -> dict:
    """Save a new plan revision. Read the plan first; a concurrent update prevents a stale save."""
    if database:
        return save_project_plan(ProjectDatabase(database, writable=True), project, expected_revision, plan)
    return bridge("plan", project, {"expected_revision": expected_revision, "plan": plan})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def localhub_draft_plan_cut(project: str, revision: int, episode: str | None = None) -> dict:
    """Assemble an unsaved cut from one saved plan. An episode ID limits the draft to one episode."""
    if database:
        return plan_cut(ProjectDatabase(database), project, revision, episode)
    query = {"revision": revision}
    if episode is not None:
        query["episode"] = episode
    return bridge("plan/cut", project, **query)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def localhub_preflight_shot(project: str, revision: int, shot_id: str, params: dict | None = None) -> dict:
    """Prepare a saved script shot with H3. Optional params include size, a decimal seed and extra references.

    Review info before submitting. Keep the returned submission, including its request key.
    """
    return bridge("video/preflight", project, {"project": project, "plan_shot": {"revision": revision, "shot_id": shot_id},
                                               "params": params or {}}, method="POST", timeout=60)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
def localhub_submit_shot(project: str, submission: dict) -> dict:
    """Queue the reviewed submission from localhub_preflight_shot. Retry with the same body and key."""
    body, key = submission.get("body"), submission.get("key")
    if not isinstance(body, dict) or body.get("project") != project or not body.get("plan_shot") or not isinstance(key, str):
        raise ValueError("请使用当前项目的镜头预检回执")
    return bridge("jobs", project, body, method="POST", headers={"Idempotency-Key": key}, timeout=60)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, untrustedContentHint=True), structured_output=False)
def localhub_compare_takes(project: str, revision: int, track_index: int, clip_index: int, job_id: str,
                           file_index: int = 0, offset_frames: int = 0) -> list[TextContent | ImageContent]:
    """Show two aligned frames for one saved video clip: A is in the cut, B is the proposed candidate.

    Use zero-based track/clip indices from read_project. Offset is in project frames from the clip start.
    Ask which project, clip or candidate the creator means if ambiguous. Present the frames and ask which
    take to keep unless they already chose. Pass the binding unchanged to choose_take.
    """
    spec = dict(revision=revision, track_index=track_index, clip_index=clip_index, job_id=job_id,
                file_index=file_index, offset_frames=offset_frames)
    result = bridge("timeline/takes/preview", project, spec, method="POST", timeout=300)
    takes = result.pop("takes")
    content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    for side, take in zip(("A", "B"), takes):
        png = take.pop("png")
        label = {"side": side, **take}
        content.extend([TextContent(type="text", text=json.dumps(label, ensure_ascii=False)),
                        ImageContent(type="image", mimeType="image/png", data=png)])
    return content


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def localhub_choose_take(project: str, binding: dict, choice: str) -> dict:
    """Apply the creator's explicit A/B choice to the single clip in the comparison receipt.

    A retains the current edit. B saves a new revision with original timing. If stale, read the latest
    edit and compare again. On a lost response read the cut before attempting another choice.
    """
    return bridge("timeline/takes/choose", project, {"binding": binding, "choice": choice},
                  method="POST", timeout=120)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, untrustedContentHint=True))
def localhub_check_cut_audio(project: str, revision: int, track_index: int | None = None) -> dict:
    """Measure the used audio ranges of a saved cut, one clip at a time. Check the dialogue track when known.

    Track indices refer to the full saved timeline. Results use zero-based, end-exclusive cut frames.
    Silence has localized spans. Peak and loudness flags apply to the reported clip range.
    Gaps are editorial choices. Speech intelligibility and final mix loudness require separate review.
    """
    if database:
        import ffmpeg_tools
        from localhub_audio_qc import inspect_cut_audio
        return inspect_cut_audio(ProjectDatabase(database), project, revision, ffmpeg_tools.locate_ffmpeg(),
                                 ffmpeg_tools.locate_ffprobe(), track_index)
    query = {"revision": revision}
    if track_index is not None:
        query["track_index"] = track_index
    return bridge("timeline/audio-qc", project, timeout=600, **query)["report"]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, untrustedContentHint=True))
def localhub_check_delivery_master(project: str, delivery: dict, master: dict, master_profile: dict | None = None) -> dict:
    """Check a registered MP4/MOV against this delivery and list source resolutions.

    Use a job_id and file_index from read_project. Pass width and height in master_profile when required;
    it can also specify video_codec, pixel_format, audio_codec, audio_sample_rate and audio_channels.
    """
    return bridge("deliveries/master-check", project, {"delivery": delivery, "master": master,
                  "master_profile": master_profile}, method="POST", timeout=120)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, untrustedContentHint=True))
def localhub_prepare_delivery(project: str, revision: int, name: str, market: str, language: str, aspect: str) -> dict:
    """Register a delivery for an exact saved cut, e.g. aspect 9:16, and return its pinned subtitle sheet.

    This prepares the specification. Rendering and master verification are separate steps.
    """
    delivery = bridge("deliveries", project, {"revision": revision, "name": name, "market": market,
                      "language": language, "aspect": aspect}, method="POST")["delivery"]
    sheet = bridge("deliveries/subtitles", project, **delivery)["sheet"]
    return {"delivery": delivery, "subtitles": sheet}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, untrustedContentHint=True))
def localhub_list_master_reviews(project: str) -> dict:
    """Find registered deliveries, original master notes and followups, including historical cuts.

    Each followup links one original annotation to a revised master binding and frame. Its version
    counts events for that annotation, starting at 1. A submitted fix is awaiting human review.
    """
    if database:
        from localhub_master_review import list_master_reviews
        return list_master_reviews(ProjectDatabase(database), project)
    return bridge("deliveries/master-review", project)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, untrustedContentHint=True))
def localhub_read_master_review(project: str, delivery: dict, master: dict, placement: str = "original") -> dict:
    """Verify a specific master against a delivery and return its current binding, frame count and notes.

    Select a delivery from localhub_list_master_reviews; master contains job_id and file_index.
    """
    return bridge("deliveries/master-review/read", project, {"delivery": delivery, "master": master,
                  "placement": placement}, method="POST", timeout=120)["review"]


def review_scope(project, binding):
    scope = json.loads(binding)
    if not isinstance(scope, dict) or scope.get("project") != project:
        raise ValueError("请使用当前项目的母版审片回执")
    return {**scope, "binding": binding}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=False)
def localhub_read_master_frame(project: str, binding: str, frame: int) -> ImageContent:
    """View the exact PNG frame of a checked master in chat. Frame numbers start at zero."""
    spec = review_scope(project, binding)
    review = bridge("deliveries/master-review/frame", project, {**spec, "frame": frame},
                    method="POST", timeout=150)["review"]
    return ImageContent(type="image", mimeType="image/png", data=review["png"])


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, untrustedContentHint=True))
def localhub_save_master_note(project: str, binding: str, frame: int, note: str, region: list[float] | None = None) -> dict:
    """Save an opinion at a zero-based frame using the exact binding from read_master_review.

    Optional region is normalized left, top, right, bottom. A changed master must be reviewed again.
    """
    spec = review_scope(project, binding)
    annotation = {"frame": frame, "note": note}
    if region is not None:
        annotation["region"] = region
    return bridge("deliveries/master-review/save", project, {**spec,
                  "annotation": annotation}, method="POST", timeout=120)["review"]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, untrustedContentHint=True))
def localhub_read_job(project: str, job_id: str) -> dict:
    """Recover the native status and media of one job in the given project."""
    return bridge("jobs/" + str(UUID(job_id)), project)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
def localhub_submit_master_fix(project: str, source_binding: str, annotation: dict, target_binding: str,
                               target_frame: int, comment: str, expected_version: int) -> dict:
    """Link an original saved note to the actual revised master/frame and describe the change.

    Use the exact annotation including region from list_master_reviews. Use expected_version=0
    for its first event, otherwise the latest event version. Submission leaves the fix pending review.
    """
    payload = dict(source_binding=source_binding, annotation=annotation, target_binding=target_binding,
                   target_frame=target_frame, comment=comment, expected_version=expected_version, state="submitted")
    return bridge("deliveries/master-review/followup", project, payload, method="POST", timeout=120)["event"]


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
def localhub_record_master_verdict(project: str, source_binding: str, annotation: dict, target_binding: str,
                                   target_frame: int, expected_version: int, decision: str, reviewer: str,
                                   comment: str) -> dict:
    """Record a human reviewer's explicit decision on this pending fix: accepted or changes_requested.

    Show the relevant frames with read_master_frame. Wait for the human's verdict and use their feedback
    and name or handle. A successful render, test or the assistant's own opinion is never a human verdict.
    """
    if decision not in ("accepted", "changes_requested"):
        raise ValueError("请选择接受本次修改或继续修改")
    payload = dict(source_binding=source_binding, annotation=annotation, target_binding=target_binding,
                   target_frame=target_frame, expected_version=expected_version, state=decision,
                   reviewer=reviewer, comment=comment)
    return bridge("deliveries/master-review/followup", project, payload, method="POST", timeout=120)["event"]


if __name__ == "__main__":
    mcp.run(transport="stdio")
