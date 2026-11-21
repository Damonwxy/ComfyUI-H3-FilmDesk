"""Durable receipts for work executed by the native ComfyUI queue."""

import asyncio
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout

from localhub_media import candidate_file
from localhub_storage import RequestConflict


FINAL = {"completed", "failed", "cancelled"}
ACTIVE = {"pending", "in_progress"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _error_text(error):
    if isinstance(error, dict):
        return error.get("exception_message") or error.get("message") or _json(error)
    return str(error) if error else None


def register_media_in_db(db, project, shot, path, kind, info):
    identity = _json(["imported", os.path.normcase(path)])
    row = db.execute("SELECT * FROM jobs WHERE project=? AND intent=?", (project, identity)).fetchone()
    if row is not None:
        if row["shot"] != shot:
            raise RequestConflict(f"素材已登记在镜头 · {row['shot']}")
        if json.loads(row["info"])["file_stamp"] != info["file_stamp"]:
            raise RequestConflict("素材文件已变动。请用新文件名登记新版本")
        return row, False
    media_id, now = str(uuid4()), time.time()
    # The sentinel satisfies the old schema without entering the native queue.
    db.execute("""
        INSERT INTO jobs (id,prompt_id,project,shot,kind,intent,graph,info,state,files,created,updated)
        VALUES (?,?,?,?,?,?,'{}',?,'completed',?,?,?)
    """, (media_id, f"file:{media_id}", project, shot, kind, identity, _json(info), _json([path]), now, now))
    return db.execute("SELECT * FROM jobs WHERE id=?", (media_id,)).fetchone(), True


async def observe_native(session, base, prompt_id, job_id=None):
    async with session.get(f"{base}/api/jobs/{prompt_id}") as response:
        if response.status == 404:
            return "missing", {}, None
        response.raise_for_status()
        native = await response.json()

    state = native.get("status")
    if state in ACTIVE:
        if job_id:
            async with session.get(f"{base}/queue") as response:
                response.raise_for_status()
                queue = await response.json()
            entries = [
                row for key in ("queue_running", "queue_pending") for row in queue[key]
                if row[1] == prompt_id
            ]
            if len(entries) != 1 or entries[0][3].get("localhub_job_id") != job_id:
                return "unverified", {}, None
        return state, {}, None

    if state in FINAL:
        if job_id and native.get("workflow", {}).get("extra_data", {}).get("localhub_job_id") != job_id:
            return "unverified", {}, None
        if state != "completed":
            return state, {}, _error_text(native.get("execution_error"))
        if native.get("execution_status", {}).get("status_str") != "success":
            return "unverified", {}, "ComfyUI 没有明确的成功记录"
        async with session.get(f"{base}/history/{prompt_id}") as response:
            response.raise_for_status()
            history = (await response.json()).get(prompt_id)
        if not history:
            return "missing", {}, None
        if job_id and history["prompt"][3].get("localhub_job_id") != job_id:
            return "unverified", {}, None
        return "completed", history.get("outputs", {}), None

    return "unverified", {}, None


class Jobs:
    def __init__(self, path, base_url, output_paths):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url
        self.output_paths = output_paths
        self.sending = {}
        with self.db() as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    prompt_id TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    shot TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    request_key TEXT,
                    intent TEXT NOT NULL,
                    graph TEXT NOT NULL,
                    info TEXT NOT NULL,
                    state TEXT NOT NULL,
                    files TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    UNIQUE(project, request_key)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS jobs_project_created ON jobs(project, created DESC)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS ad_copy (
                    project TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
                    wording TEXT NOT NULL, scope TEXT NOT NULL, evidence TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'pending', reviewer TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(project,name,revision)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS frame_notes (
                    job_id TEXT NOT NULL, file_index INTEGER NOT NULL, stamp TEXT NOT NULL,
                    frame INTEGER NOT NULL, note TEXT NOT NULL,
                    PRIMARY KEY(job_id, file_index, stamp, frame, note)
                )
            """)
            db.execute("""CREATE TABLE IF NOT EXISTS frame_region_notes (
                job_id TEXT NOT NULL, file_index INTEGER NOT NULL, stamp TEXT NOT NULL,
                frame INTEGER NOT NULL, note TEXT NOT NULL, region TEXT NOT NULL,
                PRIMARY KEY(job_id,file_index,stamp,frame,note,region)
            )""")
            db.execute("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    project TEXT NOT NULL, revision INTEGER NOT NULL, name TEXT NOT NULL,
                    market TEXT NOT NULL, language TEXT NOT NULL, aspect TEXT NOT NULL,
                    PRIMARY KEY(project, revision, name, market, language, aspect)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS audio_reports (
                    job_id TEXT NOT NULL,
                    file_index INTEGER NOT NULL,
                    stamp TEXT NOT NULL,
                    report TEXT NOT NULL,
                    PRIMARY KEY(job_id, file_index)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS reference_images (
                    project TEXT NOT NULL, asset_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, input_name TEXT NOT NULL, source TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL,
                    PRIMARY KEY(project, asset_id, revision)
                )
            """)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def find(self, job_id=None, *, prompt_id=None):
        with self.db() as db:
            if job_id:
                return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return db.execute("SELECT * FROM jobs WHERE prompt_id=?", (prompt_id,)).fetchone()

    def recent(self, project, limit):
        with self.db() as db:
            return db.execute(
                "SELECT * FROM jobs WHERE project=? ORDER BY created DESC LIMIT ?", (project, limit),
            ).fetchall()

    def audio_report(self, job_id, index, stamp):
        with self.db() as db:
            row = db.execute("SELECT report FROM audio_reports WHERE job_id=? AND file_index=? AND stamp=?",
                             (job_id, index, _json(stamp))).fetchone()
        return json.loads(row["report"]) if row else None

    def frame_notes(self, job_id, index, stamp, frames, new=None):
        with self.db() as db:
            if new is not None:
                if not isinstance(new, dict):
                    raise ValueError("请提交帧号和批注")
                if new.get("stamp") != stamp:
                    raise RequestConflict("候选文件已变更，请重新打开候选")
                frame, note = new.get("frame"), new.get("note")
                if type(frame) is not int or not 0 <= frame < frames:
                    raise ValueError("批注源帧超出候选范围")
                if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
                    raise ValueError("批注须为 1–1000 个字符")
                values = (job_id, index, stamp, frame, note.strip())
                if "region" in new:
                    region = new["region"]
                    if (not isinstance(region, list) or len(region) != 4
                            or not all(type(v) in (int, float) and 0 <= v <= 1 for v in region)
                            or region[0] >= region[2] or region[1] >= region[3]):
                        raise ValueError("区域须为源画面内的左、上、右、下四个归一化坐标")
                    db.execute("INSERT OR IGNORE INTO frame_region_notes VALUES (?,?,?,?,?,?)",
                               (*values, _json([float(v) for v in region])))
                else:
                    db.execute("INSERT OR IGNORE INTO frame_notes VALUES (?,?,?,?,?)", values)
            keys = (job_id, index, stamp)
            rows = db.execute("""SELECT frame,note,NULL AS region,0 AS kind,rowid AS seq FROM frame_notes
                WHERE job_id=? AND file_index=? AND stamp=?
                UNION ALL SELECT frame,note,region,1,rowid FROM frame_region_notes
                WHERE job_id=? AND file_index=? AND stamp=? ORDER BY frame,kind,seq""", keys + keys)
            return [dict(frame=row["frame"], note=row["note"],
                         **({"region": json.loads(row["region"])} if row["region"] else {})) for row in rows]

    def save_audio_report(self, job_id, index, stamp, report):
        with self.db() as db:
            db.execute("""
                INSERT INTO audio_reports (job_id,file_index,stamp,report) VALUES (?,?,?,?)
                ON CONFLICT(job_id,file_index) DO UPDATE SET stamp=excluded.stamp, report=excluded.report
            """, (job_id, index, _json(stamp), _json(report)))

    def register_media(self, project, shot, path, kind, info):
        """Catalogue a local source in the same candidate list without enqueuing it."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            return register_media_in_db(db, project, shot, path, kind, info)

    def _replay(self, db, project, key, intent):
        if key:
            row = db.execute(
                "SELECT * FROM jobs WHERE project=? AND request_key=?", (project, key),
            ).fetchone()
            if row and row["intent"] != intent:
                raise RequestConflict("该请求键已用于另一项生成。新候选请使用新的请求键。")
            return row
        return None

    async def accept(self, kind, project, shot, key, params, compile_graph):
        intent = _json([kind, shot, params])
        with self.db() as db:
            row = self._replay(db, project, key, intent)
        if row is None:
            job_id, prompt_id = str(uuid4()), str(uuid4())
            options = dict(params)
            if not options.get("prefix"):
                options["prefix"] = f"localhub/{kind}/{job_id}"
            graph, info = await asyncio.to_thread(compile_graph, options)
            now = time.time()
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = self._replay(db, project, key, intent)
                if row is None:
                    db.execute("""
                        INSERT INTO jobs (id,prompt_id,project,shot,kind,request_key,intent,graph,info,state,created,updated)
                        VALUES (?,?,?,?,?,?,?,?,?,'accepted',?,?)
                    """, (job_id, prompt_id, project, shot, kind, key, intent, _json(graph), _json(info), now, now))
                    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.schedule(row)
        return row

    def schedule(self, row):
        job_id = row["id"]
        if row["state"] == "accepted" and job_id not in self.sending:
            self.sending[job_id] = asyncio.create_task(self._send(row))

    def update(self, job_id, state, *, files=(), error=None):
        with self.db() as db:
            db.execute("""
                UPDATE jobs SET state=?,files=?,error=?,updated=?
                WHERE id=? AND state NOT IN ('completed','failed','cancelled')
            """, (state, _json(list(files)), error, time.time(), job_id))

    async def _send(self, row):
        job_id = row["id"]
        try:
            with self.db() as db:
                claimed = db.execute(
                    "UPDATE jobs SET state='submitting',updated=? WHERE id=? AND state='accepted'",
                    (time.time(), job_id),
                ).rowcount
            if not claimed:
                return
            body = {
                "prompt": json.loads(row["graph"]),
                "prompt_id": row["prompt_id"],
                "client_id": "localhub_bridge",
                "extra_data": {"localhub_job_id": job_id},
            }
            try:
                async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                    async with session.post(f"{self.base_url()}/prompt", json=body) as response:
                        result = await response.json()
                        if response.status == 200 and result.get("prompt_id") == row["prompt_id"]:
                            self.update(job_id, "pending")
                        elif response.status == 400 and "error" in result:
                            reason = _error_text(result["error"])
                            if result.get("node_errors"):
                                reason = f"{reason} · {_json(result['node_errors'])}"
                            self.update(job_id, "failed", error=reason)
                        else:
                            self.update(job_id, "needs_reconciliation", error="ComfyUI 提交回执无法确认")
            except (ClientError, TimeoutError, ValueError):
                self.update(job_id, "needs_reconciliation", error="ComfyUI 提交已发出，回执待核对")
        finally:
            self.sending.pop(job_id, None)

    async def refresh(self, row):
        self.schedule(row)
        if row["state"] in FINAL or row["id"] in self.sending:
            return self.find(row["id"])
        try:
            async with ClientSession(timeout=ClientTimeout(total=8)) as session:
                state, outputs, error = await observe_native(
                    session, self.base_url(), row["prompt_id"], row["id"],
                )
        except (ClientError, TimeoutError):
            return row
        if state in {"missing", "unverified"}:
            error = error or "原生队列和历史暂时无法证明该任务的状态"
            state = "needs_reconciliation"
        files = self.output_paths(outputs, include_temp=False)
        if state != row["state"] or error != row["error"]:
            self.update(row["id"], state, files=files, error=error)
        return self.find(row["id"])

    async def cancel(self, row):
        job_id = row["id"]
        with self.db() as db:
            removed = db.execute("""
                UPDATE jobs SET state='cancelled',updated=? WHERE id=? AND state='accepted'
            """, (time.time(), job_id)).rowcount
        if removed or row["state"] in FINAL:
            return self.find(job_id)
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            base, prompt_id = self.base_url(), row["prompt_id"]
            state, _, _ = await observe_native(session, base, prompt_id, job_id)
            if state in ACTIVE:
                async with session.post(f"{base}/api/jobs/{prompt_id}/cancel") as response:
                    response.raise_for_status()
                    sent = (await response.json()).get("cancelled")
                if sent:
                    with self.db() as db:
                        db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        return await self.refresh(self.find(job_id))

    def public(self, row):
        fields = (
            "id", "prompt_id", "project", "shot", "kind", "state", "error", "cancel_requested",
            "created", "updated",
        )
        result = {field: row[field] for field in fields}
        info = json.loads(row["info"])
        result["source"] = info.get("source", "generated")
        if result["source"] == "imported":
            result["prompt_id"] = None
        expected = json.loads(row["files"])
        result["outputs"] = [
            {"index": i, "name": Path(path).name, "available": candidate_file(path, info),
             "url": f"/localhub/v2/jobs/{row['id']}/media/{i}"}
            for i, path in enumerate(expected)
        ]
        result["files"] = [expected[output["index"]] for output in result["outputs"] if output["available"]]
        result["info"] = info
        if row["state"] == "completed":
            if expected and len(result["files"]) == len(expected):
                result["delivery"] = "ready"
            else:
                result["delivery"] = "partial" if result["files"] else "missing"
        return result
