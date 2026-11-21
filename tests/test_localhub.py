import asyncio
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from aiohttp import ClientConnectionError
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from delivery_pack import make_pack, relink_pack
from legacy_canvas import archive_canvas, read_archives
from localhub_audio_qc import inspect_audio, inspect_cut_audio
from localhub_compare import compare_takes
from localhub_copy import list_copy, update_copy
from localhub_jobs import Jobs, RequestConflict
from localhub_master_review import list_master_reviews, master_review
from localhub_media import file_stamp, probe_media
from localhub_project_plan import bind_h3_shot, plan_cut, read_project_plan, save_project_plan
from localhub_references import list_references, register_reference, resolve_reference
from localhub_storage import ProjectDatabase
from localhub_subtitles import read_subtitles, save_subtitles, subtitle_srt
from localhub_timeline import _draft_span, export_otio, import_otio, list_deliveries, read_edit, register_delivery, save_edit
from project_desk import read_board
from resolve_finish import apply_scaling
from studio_local import create_app


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise ClientConnectionError(f"HTTP {self.status}")

    async def json(self):
        return self.body


class Native:
    def __init__(self, output):
        self.output = output
        self.posts = []
        self.state = "pending"
        self.lose_response = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __call__(self, **kwargs):
        return self

    def post(self, url, json=None):
        if url.endswith("/cancel"):
            self.state = "missing"
            return Response(200, {"cancelled": True})
        self.posts.append(json)
        body = ClientConnectionError("response lost after enqueue") if self.lose_response else {
            "prompt_id": json["prompt_id"],
        }
        return Response(200, body)

    def get(self, url):
        if not self.posts or self.state == "missing":
            return Response(404, {})
        last = self.posts[-1]
        pid = last["prompt_id"]
        extra = last["extra_data"]
        prompt = [0, pid, {}, extra, []]
        if url.endswith("/queue"):
            return Response(200, {"queue_running": [], "queue_pending": [prompt]})
        if "/history/" in url:
            return Response(200, {pid: {"prompt": prompt, "outputs": {"out": []}}})
        return Response(200, {
            "status": self.state,
            "workflow": {"extra_data": extra},
            "execution_status": {"status_str": "success" if self.state == "completed" else "error", "completed": False},
            "execution_error": {"exception_message": "Fixture node failed"} if self.state == "failed" else None,
        })


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory(dir=os.environ.get("LOCALHUB_TEST_ROOT"))
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.file = self.root / "候选 media.mp4"
        self.file.write_bytes(b"fixture")
        self.native = Native(self.file)
        self.patch = patch("localhub_jobs.ClientSession", self.native)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.store = Jobs(self.root / "jobs.sqlite3", lambda: "http://native",
                          lambda outputs, **_: [str(self.file)] if outputs else [])

    async def test_standalone_shares_project_data_without_generation_routes(self):
        host = "127.0.0.1:8210"
        app = create_app(self.store.path, self.root / "input", 8210)
        async with TestClient(TestServer(app), headers={"Host": host}) as client:
            async with client.get("/localhub/v2/studio/status") as response:
                self.assertEqual(await response.json(), {"ok": True, "generation": False})
            endpoint = "/localhub/v2/plan?project=fixture"
            async with client.get(endpoint) as response:
                plan = (await response.json())["plan"]
            plan["script"] = "取用已经登记的候选。"
            draft = {"expected_revision": 0, "plan": plan}
            async with client.put(endpoint, json=draft, headers={"Origin": "http://example.test"}) as response:
                self.assertEqual(response.status, 403)
            async with client.put(endpoint, json=draft, headers={"Origin": "http://" + host}) as response:
                self.assertEqual((await response.json())["revision"], 1)
            async with client.post("/localhub/v2/jobs", json={}) as response:
                self.assertEqual(response.status, 405)
        self.assertEqual(read_project_plan(self.store, "fixture", 1)["plan"]["script"], plan["script"])
        self.assertEqual(self.native.posts, [])

    def test_legacy_canvas_preserves_original_ids_groups_and_relinked_source(self):
        if not shutil.which("ffprobe"):
            self.skipTest("ffprobe is required")
        source = self.root / "restored.wav"
        with wave.open(str(source), "wb") as data:
            data.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            data.writeframes(bytes(2 * 48000))
        originals = [
            {"id": "asset_1", "path": str(source), "meta": {"role": "first"}},
            {"id": "asset_2", "path": str(source), "meta": {"role": "repeat"}},
            {"id": "asset_3", "path": str(self.root / "lost.wav"), "meta": {"role": "missing"}},
        ]
        groups = [{"id": "group_1", "name": "旧版本", "asset_ids": ["asset_3", "asset_2"]}]
        canvas = self.root / "canvas.json"
        raw = json.dumps({"assets": originals, "groups": groups, "next_id": 4})
        canvas.write_text(raw, encoding="utf8")
        directory = self.root / "staged"
        first = archive_canvas(self.store, "fixture", canvas, directory)
        archive, = first["archives"]
        self.assertEqual(archive["groups"], groups)
        self.assertEqual([row["original"] for row in archive["assets"]], originals)
        self.assertEqual([row["state"] for row in archive["assets"]], ["linked", "linked", "missing"])
        self.assertEqual(first, archive_canvas(self.store, "fixture", canvas, directory))
        relink = {"asset_3": str(source)}
        recovered = archive_canvas(self.store, "fixture", canvas, directory, relink=relink)
        tail = recovered["archives"][0]["assets"][-1]
        self.assertEqual(tail["state"], "linked")
        self.assertEqual(tail["recovered_from"], str(source.resolve()))
        self.assertEqual(tail["job_id"], archive["assets"][0]["job_id"])
        self.assertEqual(recovered, archive_canvas(self.store, "fixture", canvas, directory, relink=relink))
        self.assertEqual(len(self.store.recent("fixture", 10)), 1)
        self.assertEqual(read_archives(self.store, "other"), [])
        self.assertEqual(canvas.read_text(encoding="utf8"), raw)

    async def accept(self, key="retry-key", prompt="同一个镜头"):
        return await self.store.accept("video", "fixture", "S01", key, {"prompt": prompt},
                                       lambda params: ({}, {"frames": 124, "fps": 24}))

    async def send_pending(self):
        await asyncio.gather(*list(self.store.sending.values()))

    async def test_same_key_only_enqueues_once(self):
        replies = await asyncio.gather(*(self.accept() for _ in range(16)))
        await self.send_pending()
        self.assertEqual(len({reply["id"] for reply in replies}), 1)
        self.assertEqual(len(self.native.posts), 1)
        with self.assertRaises(RequestConflict):
            await self.accept(prompt="改过的镜头")

    async def test_lost_response_reconciles_and_returns_original_job(self):
        self.native.lose_response = True
        row = await self.accept()
        await self.send_pending()
        self.assertEqual(self.store.find(row["id"])["state"], "needs_reconciliation")
        self.native.state = "completed"
        resumed = Jobs(self.store.path, self.store.base_url, self.store.output_paths)
        result = resumed.public(await resumed.refresh(resumed.find(row["id"])))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["delivery"], "ready")
        self.assertEqual(result["files"], [str(self.file)])
        self.assertEqual(len(self.native.posts), 1)
        self.file.unlink()
        self.assertEqual(resumed.public(resumed.find(row["id"]))["delivery"], "missing")

    async def test_failed_node_is_terminal(self):
        row = await self.accept()
        await self.send_pending()
        self.native.state = "failed"
        failed = await self.store.refresh(self.store.find(row["id"]))
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "Fixture node failed")

    async def test_delivery_pins_a_saved_cut_and_survives_retries(self):
        row = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(row["id"]))
        clip = dict(job_id=row["id"], file_index=0, start_frame=0, duration_frames=10, rate="24")
        cut = {"rate": "24", "tracks": [{"kind": "Video", "name": "画面", "clips": [clip]}]}
        save_edit(self.store, "fixture", 0, cut)
        wide = dict(name="新品", market="测试", language="中文", aspect="16:9", revision=1)
        register_delivery(self.store, "fixture", wide)
        cut["tracks"][0]["clips"][0]["duration_frames"] = 8
        save_edit(self.store, "fixture", 1, cut)
        tall = dict(wide, aspect="9:16", revision=2)
        register_delivery(self.store, "fixture", tall)
        register_delivery(self.store, "fixture", wide)
        reopened = Jobs(self.store.path, self.store.base_url, self.store.output_paths)
        self.assertEqual(list_deliveries(reopened, "fixture"), [tall, wide])
        self.assertEqual(list_deliveries(reopened, "other"), [])
        with self.assertRaises(LookupError):
            register_delivery(reopened, "other", wide)
        with self.assertRaises(ValueError):
            register_delivery(reopened, "fixture", dict(wide, market=" "))
        with self.assertRaises(LookupError):
            register_delivery(reopened, "fixture", dict(wide, revision=3))
        save_edit(reopened, "fixture", 2, {"rate": "24", "tracks": []})
        with self.assertRaises(ValueError):
            register_delivery(reopened, "fixture", dict(wide, revision=3))

    def test_offline_first_cut_and_stale_live_edit(self):
        board = read_board(self.store.path, "new")
        self.assertEqual((board["revision"], board["candidates"]), (0, []))
        with self.store.db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='edits'").fetchone())
        cut = {"rate": "24", "tracks": [{"kind": "Video", "clips": [{"gap": True, "duration_frames": 8}]}]}
        offline = ProjectDatabase(self.store.path, writable=True)
        saved = save_edit(offline, "new", 0, cut)
        self.assertEqual(saved, read_edit(self.store, "new"))
        with self.assertRaises(RequestConflict):
            save_edit(self.store, "new", 0, cut)
        with self.assertRaises(FileNotFoundError):
            ProjectDatabase(self.root / "wrong.sqlite", writable=True)

    async def test_project_plan_history_pins_references_selection_and_episode_cut(self):
        row = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(row["id"]))
        stage = self.root / "localhub_media"
        stage.mkdir()
        asset = stage / "actor.png"
        Image.new("RGB", (16, 16), "red").save(asset)
        register_reference(self.store, self.root, "fixture", "演员", asset.name)
        clip = dict(job_id=row["id"], file_index=0, start_frame=12, duration_frames=48, rate="24")
        cut = {"rate": "24", "tracks": [{"kind": "Video", "clips": [clip]}]}
        save_edit(self.store, "fixture", 0, cut)
        plan = dict(title="短剧", genre="series", script="INT. 工坊\n人物：灯亮了", episodes=[
            dict(id="E1", title="第一集", cut_revision=1), dict(id="E2", title="第二集")],
            scenes=[dict(id="C1", title="工坊", episode_id="E1", script_line=1),
                    dict(id="C2", title="工坊返场", episode_id="E2", script_line=1)],
            shots=[dict(id="S2", title="返场", scene_id="C2", frames=36),
                   dict(id="S1", title="亮灯", scene_id="C1", frames=48, script_line=2, picture=clip,
                        action="人物举灯", references=[dict(asset_id="演员", revision=1, use="first", role="角色")])])
        offline = ProjectDatabase(self.store.path, writable=True)
        one = save_project_plan(offline, "fixture", 0, plan)
        self.assertEqual(save_project_plan(self.store, "fixture", 0, plan), one)
        self.assertEqual(plan_cut(offline, "fixture", 1, "E1")["timeline"]["tracks"][0]["clips"], [clip])
        self.assertEqual(plan_cut(offline, "fixture", 1, "E2")["unselected"], ["S2"])
        self.assertEqual(plan_cut(offline, "fixture", 1)["timeline"]["tracks"][0]["clips"],
                         [clip, {"gap": True, "duration_frames": 36}])
        Image.new("RGB", (16, 16), "green").save(asset)
        register_reference(self.store, self.root, "fixture", "演员", asset.name)
        plan["shots"][0]["references"] = [dict(asset_id="演员", revision=2, use="first")]
        plan["shots"][0]["prompt"] = "人物回到工坊"
        two = save_project_plan(self.store, "fixture", 1, plan)
        self.assertEqual(two["revision"], 2)
        with self.assertRaises(RequestConflict):
            save_project_plan(self.store, "fixture", 1, dict(plan, title="覆盖旧稿"))
        with self.assertRaisesRegex(ValueError, "当前项目"):
            save_project_plan(self.store, "other", 0, dict(plan, episodes=[dict(id="E1", title="A"),
                                                                         dict(id="E2", title="B")],
                                                         shots=[dict(plan["shots"][1], references=[])]))
        old = read_project_plan(offline, "fixture", 1)
        self.assertEqual(old["plan"]["episodes"][0]["cut_revision"], 1)
        self.assertEqual(old["plan"]["shots"][1]["references"][0]["revision"], 1)
        title, params, take, identity = bind_h3_shot(offline, "fixture", dict(revision=2, shot_id="S2"), {}, None)
        self.assertEqual((title, take, identity), ("返场", {"start_frame": 0, "end_frame": 36}, {"revision": 2, "shot_id": "S2"}))
        self.assertEqual((params["mode"], params["first_frame"]), ("i2v", {"asset_id": "演员", "revision": 2}))
        self.file.unlink()
        self.assertEqual(save_project_plan(offline, "fixture", 2, dict(plan, brief="离线改简报"))["revision"], 3)
        with self.assertRaisesRegex(ValueError, "缺失"):
            plan_cut(offline, "fixture", 2, "E1")

    def test_take_choice_checks_reviewed_files_and_preserves_other_clip_occurrences(self):
        media = []
        for name in ("A", "B"):
            path = self.root / (name + ".mp4")
            path.write_bytes(name.encode())
            job = self.store.register_media("ad", name, str(path), "video", {"frames": 120})[0]
            media.append(job["id"])
        rate = "24000/1001"
        clip = dict(job_id=media[0], file_index=0, start_frame=3, duration_frames=9, rate=rate)
        cut = {"rate": rate, "tracks": [{"kind": "Video", "clips": [clip, clip]}]}
        original = save_edit(self.store, "ad", 0, cut)["timeline"]
        spec = dict(revision=1, track_index=0, clip_index=1, job_id=media[1], file_index=0, offset_frames=2)
        info = {"fps": "48000/1001", "frames": 24}
        def probe(path, ffprobe):
            return str(path), "video", info if path.stem == "B" else {"fps": rate, "frames": 12}
        with patch("localhub_compare.inspect_media", side_effect=probe), patch("localhub_compare.video_frame_png", return_value=b"x"):
            preview = compare_takes(self.store, "ad", spec)
            self.assertEqual([take["source_frame"] for take in preview["takes"]], [5, 10])
            receipt = preview["binding"]
            self.assertEqual(compare_takes(self.store, "ad", receipt, "A")["revision"], 1)
            (self.root / "B.mp4").write_bytes(b"changed")
            with self.assertRaises(RequestConflict):
                compare_takes(self.store, "ad", receipt, "B")
            info["fps"] = 24
            with self.assertRaisesRegex(ValueError, "精确对齐"):
                compare_takes(self.store, "ad", spec)
            info.update(fps="48000/1001", frames=23)
            with self.assertRaisesRegex(ValueError, "太短"):
                compare_takes(self.store, "ad", spec)
            info["frames"] = 24
            receipt = compare_takes(self.store, "ad", spec)["binding"]
            self.assertEqual(compare_takes(self.store, "ad", receipt, "B")["revision"], 2)
            with self.assertRaises(RequestConflict):
                compare_takes(self.store, "ad", receipt, "B")
        self.assertEqual(read_edit(self.store, "ad", 1)["timeline"], original)
        first, second = read_edit(self.store, "ad", 2)["timeline"]["tracks"][0]["clips"]
        self.assertEqual(first, clip)
        self.assertEqual(second, dict(job_id=media[1], file_index=0, start_frame=6, duration_frames=18, rate="48000/1001"))

    async def test_master_notes_survive_reopen_and_are_scoped_to_exact_review(self):
        row = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(row["id"]))
        master = {"job_id": row["id"], "file_index": 0}
        clip = {**master, "duration_frames": 10, "start_frame": 0}
        cut = {"rate": "24", "tracks": [{"kind": "Video", "clips": [clip]}]}
        save_edit(self.store, "fixture", 0, cut)
        delivery = {"revision": 1, "name": "Preview", "market": "Demo", "language": "und", "aspect": "9:16"}
        register_delivery(self.store, "fixture", delivery)
        spec = {"delivery": delivery, "master": master}
        info = {"width": 288, "height": 512, "fps": 24, "frames": 10, "has_audio": False}
        annotation = {"frame": 9, "note": "Check the wordmark", "region": [0.1, 0.2, 0.7, 0.6]}
        with patch("localhub_delivery.inspect_media", return_value=(str(self.file), "video", info)):
            binding = master_review(self.store, "fixture", spec, "read")["binding"]
            payload = dict(spec, binding=binding, annotation=annotation)
            for _ in range(2):
                self.assertEqual(master_review(self.store, "fixture", payload, "save")["notes"], [annotation])
            with self.assertRaises(ValueError):
                master_review(self.store, "fixture", dict(payload, annotation=dict(annotation, frame=10)), "save")
            alternate = dict(payload, placement="youtube-vertical-2026-11-20")
            with self.assertRaises(RequestConflict):
                master_review(self.store, "fixture", alternate, "save")
            self.assertEqual(master_review(self.store, "fixture", alternate, "read")["notes"], [])
            other = dict(delivery, name="Second delivery")
            register_delivery(self.store, "fixture", other)
            self.assertEqual(master_review(self.store, "fixture", dict(spec, delivery=other), "read")["notes"], [])
        self.file.unlink()
        reopened = ProjectDatabase(self.store.path)
        history = list_master_reviews(reopened, "fixture")["reviews"]
        self.assertEqual([(r["binding"], r["notes"]) for r in history], [(binding, [annotation])])
        self.assertEqual(list_master_reviews(reopened, "other")["reviews"], [])

    async def test_relocate_delivery_and_detect_changed_media(self):
        row = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(row["id"]))
        master = dict(job_id=row["id"], file_index=0)
        clip = dict(**master, duration_frames=10, start_frame=3)
        cut = {"rate": "24", "tracks": [{"kind": "Video", "clips": [clip, clip]}]}
        save_edit(self.store, "fixture", 0, cut)
        delivery = dict(name="验收", market="内部", language="中文", aspect="16:9", revision=1)
        register_delivery(self.store, "fixture", delivery)
        copy = dict(name="字幕", wording="红色杯身，银色杯口", evidence="样例图 v1", scope="内部中文", expected_revision=0)
        update_copy(self.store, "fixture", copy, review=False)
        update_copy(self.store, "fixture", dict(name="字幕", expected_revision=1, reviewer="QA",
                    reason="外观一致", decision="approved"), review=True)
        save_subtitles(self.store, "fixture", delivery, 0,
                       [dict(start_frame=1, end_frame=19, name="字幕", revision=1)])
        spec = dict(project="fixture", delivery=delivery, master=master)
        destination = self.root / "delivery.zip"
        info = dict(width=512, height=288, fps=24, frames=20, has_audio=True, video_codec="h264",
                    sample_aspect_ratio="1:1", rotation=0, audio_offset=["0", "0"])
        with patch("localhub_delivery.inspect_media", return_value=(str(self.file), "video", info)):
            with self.assertRaisesRegex(ValueError, "通过记录"):
                make_pack(self.store.path, dict(spec, copy=[dict(name="片尾", revision=2)]), destination, "ffprobe")
            info["frames"] = 19
            with self.assertRaisesRegex(ValueError, "时长"):
                make_pack(self.store.path, spec, destination, "ffprobe")
            info["frames"] = 20
            profile = dict(width=512, height=288, video_codec="h264")
            with self.assertRaisesRegex(ValueError, "像素宽度"):
                make_pack(self.store.path, dict(spec, master_profile=dict(profile, width=1920)), destination, "ffprobe")
            info["audio_offset"] = ["0", "1/12"]
            with self.assertRaisesRegex(ValueError, "偏差不超过一帧"):
                make_pack(self.store.path, dict(spec, master_profile=profile), destination, "ffprobe")
            info["audio_offset"] = ["0", "1/24"]
            finishing = dict(**master, method="resolve_super_scale_2x")
            for invalid in ([dict(finishing, job_id="off-cut")], [finishing, finishing]):
                with self.assertRaisesRegex(ValueError, "画面源片"):
                    make_pack(self.store.path, dict(spec, video_finishing=invalid), destination, "ffprobe")
            result = make_pack(self.store.path, dict(spec, master_profile=profile, subtitle_revision=1,
                                                   video_finishing=[finishing]), destination, "ffprobe")
            with self.assertRaises(FileExistsError):
                make_pack(self.store.path, spec, destination, "ffprobe")
        moved = self.root / "接收方"
        with ZipFile(destination) as archive:
            archive.extractall(moved)
        self.assertEqual(len(result["media"]), 1)
        self.assertEqual(result["media"][0]["raster"], {"width": 512, "height": 288})
        self.assertEqual(result["media"][0]["finishing"], {"method": "resolve_super_scale_2x", "recorded_by_editor": True})
        self.assertEqual([row["name"] for row in result["copy"]], ["字幕"])
        srt = moved / "subtitles.srt"
        srt_content = srt.read_bytes()
        self.assertIn("00:00:00,042 --> 00:00:00,792", srt_content.decode("utf-8"))
        src = moved / result["media"][0]["path"]
        import opentimelineio as otio
        linked = relink_pack(moved)
        clips = list(otio.adapters.read_from_file(str(linked)).find_clips())
        self.assertEqual([(c.media_reference.target_url, c.source_range.start_time.value,
                           c.duration().value) for c in clips], [(src.as_uri(), 3, 10)] * 2)
        self.assertEqual(relink_pack(moved), linked)
        # A recipient may rename any media file. The chooser supplies the bytes
        # from that copy; an ambiguous candidate identity still needs a choice.
        url = src.as_uri()
        app = create_app(self.store.path, self.root / "input", 8210)
        endpoint = "/localhub/v2/timeline/otio?project=fixture"
        payload = dict(preview=True, base_revision=1, manifest=result, otio=linked.read_text(encoding="utf-8"))
        async with TestClient(TestServer(app), headers={"Host": "127.0.0.1:8210"}) as client:
            proof = await (await client.post("/localhub/v2/timeline/source-fingerprint", data=src.read_bytes())).json()
            self.assertEqual(proof["sha256"], result["media"][0]["sha256"])
            payload["source_evidence"] = {url: proof}
            unique = await (await client.post(endpoint, json=payload)).json()
            self.assertEqual(unique["timeline"], result["timeline"])
            self.assertEqual(unique["source_review"][url]["selected"], master)
            wrong = dict(payload, source_evidence={url: dict(proof, sha256="0" * 64)})
            self.assertIsNone((await (await client.post(endpoint, json=wrong)).json())["timeline"])
            foreign = dict(payload, manifest=dict(result, project="other"))
            self.assertEqual((await client.post(endpoint, json=foreign)).status, 400)
            copied = self.root / "other-record.mp4"
            shutil.copyfile(src, copied)
            other, _ = self.store.register_media("fixture", "另一记录", str(copied), "video",
                                                 dict(source="imported", file_stamp=file_stamp(copied)))
            duplicate = await (await client.post(endpoint, json=payload)).json()
            self.assertIsNone(duplicate["timeline"])
            self.assertTrue(duplicate["source_review"][url]["needs_choice"])
            proof["choice"] = [row["id"], 0]
            chosen = await (await client.post(endpoint, json=payload)).json()
            self.assertEqual(chosen["timeline"], result["timeline"])
            self.file.unlink()
            self.assertEqual((await client.post(endpoint, json=payload)).status, 400)
            del proof["choice"]
            orphaned = await (await client.post(endpoint, json=payload)).json()
            self.assertTrue(orphaned["source_review"][url]["needs_choice"])
            self.assertIsNone(orphaned["timeline"])
            proof["choice"] = [other["id"], 0]
            rebound = await (await client.post(endpoint, json=payload)).json()
            self.assertTrue(all(c["job_id"] == other["id"] for c in rebound["timeline"]["tracks"][0]["clips"]))
        self.assertEqual(read_edit(self.store, "fixture")["revision"], 1)
        srt.write_bytes(srt_content.replace(b",792", b",793"))
        with self.assertRaisesRegex(ValueError, "校验失败 subtitles.srt"):
            relink_pack(moved)
        srt.write_bytes(srt_content)
        src.write_bytes(b"x" * src.stat().st_size)
        with self.assertRaisesRegex(ValueError, "校验失败"):
            relink_pack(moved)

    async def test_subtitle_revisions_pin_approved_words_and_frame_ranges(self):
        row = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(row["id"]))
        clip = dict(job_id=row["id"], file_index=0, start_frame=0, duration_frames=20)
        save_edit(self.store, "fixture", 0, {"rate": "24000/1001", "tracks": [{"kind": "Video", "clips": [clip]}]})
        delivery = dict(name="验收", market="内部", language="中文", aspect="16:9", revision=1)
        other = dict(delivery, aspect="9:16")
        for spec in (delivery, other):
            register_delivery(self.store, "fixture", spec)
        store = ProjectDatabase(self.store.path, writable=True)
        self.assertEqual(read_subtitles(store, "fixture", delivery)["revision"], 0)
        copy = dict(name="短句", wording="杯口", evidence="商品图", scope="样例", expected_revision=0)
        update_copy(self.store, "fixture", copy, review=False)
        cue = dict(start_frame=2, end_frame=20, name="短句", revision=1, text="不采信前端文本")
        with self.assertRaisesRegex(ValueError, "已审核通过"):
            save_subtitles(store, "fixture", delivery, 0, [cue])
        update_copy(self.store, "fixture", dict(name="短句", expected_revision=1, reviewer="QA",
                    reason="已核对", decision="approved"), review=True)
        for cues in ([cue, cue], [dict(cue, end_frame=21)]):
            with self.assertRaisesRegex(ValueError, "交叠或超出"):
                save_subtitles(store, "fixture", delivery, 0, cues)
        self.assertEqual(save_subtitles(store, "fixture", delivery, 0, [cue]), 1)
        self.assertEqual(save_subtitles(store, "fixture", delivery, 0, [cue]), 1)
        self.assertEqual(read_subtitles(store, "fixture", other)["revision"], 0)
        self.assertEqual(save_subtitles(store, "fixture", delivery, 1, [dict(cue, start_frame=3)]), 2)
        with self.assertRaises(RequestConflict):
            save_subtitles(store, "fixture", delivery, 1, [cue])
        pinned = read_subtitles(ProjectDatabase(self.store.path), "fixture", delivery, 1)
        self.assertEqual((pinned["revision"], pinned["latest_revision"]), (1, 2))
        self.assertEqual(subtitle_srt(pinned), "1\n00:00:00,083 --> 00:00:00,834\n杯口\n")

    def test_resolve_scaling_rolls_back_when_editor_refuses_one_clip(self):
        first, second = [self.root / filename for filename in ("one.mp4", "two.mp4")]
        for path in (first, second):
            path.write_bytes(b"fixture")
        (self.root / "manifest.json").write_text(json.dumps({"media": [
            {"path": path.name, "finishing": {"method": "resolve_super_scale_2x"}} for path in (first, second)
        ]}), encoding="utf-8")

        class Media:
            def __init__(self, path, accepts):
                self.path, self.accepts, self.scale = path, accepts, 1

            def GetClipProperty(self, key):
                return str(self.path) if key == "File Path" else str(self.scale)

            def GetMediaId(self):
                return self.path.name

            def SetClipProperty(self, key, value):
                if not self.accepts:
                    return False
                self.scale = value
                return True

        clips = [Media(first, True), Media(second, False)]
        items = [types.SimpleNamespace(GetMediaPoolItem=lambda clip=clip: clip) for clip in clips]
        project = types.SimpleNamespace(IsRenderingInProgress=lambda: False)
        timeline = types.SimpleNamespace(GetTrackCount=lambda kind: 1, GetItemListInTrack=lambda kind, track: items)
        with patch("resolve_finish.relink_pack"):
            with self.assertRaisesRegex(RuntimeError, "未应用 Super Scale"):
                apply_scaling(project, timeline, self.root)
        self.assertEqual([clip.scale for clip in clips], [1, 1])

    def test_frame_notes_follow_one_output_and_file_version(self):
        note = {"frame": 12, "note": "复核杯口高光", "stamp": "100:1"}
        for _ in range(2):
            rows = self.store.frame_notes("wide", 0, "100:1", 39, note)
            self.assertEqual(rows, [{"frame": 12, "note": "复核杯口高光"}])
        for job, index, stamp in [("tall", 0, "100:1"), ("wide", 1, "100:1"), ("wide", 0, "100:2")]:
            self.assertEqual(self.store.frame_notes(job, index, stamp, 39), [])
        with self.assertRaises(RequestConflict):
            self.store.frame_notes("wide", 0, "100:2", 39, note)
        with self.assertRaises(ValueError):
            self.store.frame_notes("wide", 0, "100:1", 39, dict(note, frame=39))
        for region in ([0, 0, .1, .2], [.5, 0, 1, .2], [0, 0, .1, .2]):
            rows = self.store.frame_notes("wide", 0, "100:1", 39, dict(note, region=region))
        self.assertEqual(len(rows), 3)
        self.assertNotIn("region", rows[0])
        self.assertEqual([row["region"] for row in rows[1:]], [[0, 0, .1, .2], [.5, 0, 1, .2]])
        for invalid in (None, [False, 0, 1, 1], [0, 0, 0, .5], [0, 0, 1.2, 1]):
            with self.assertRaises(ValueError):
                self.store.frame_notes("wide", 0, "100:1", 39, dict(note, region=invalid))
        self.assertEqual(self.store.frame_notes("wide", 1, "100:1", 39), [])

    def test_copy_revisions_and_review_isolation(self):
        def save(data, review=False, project="sample"):
            return update_copy(self.store, project, data, review=review)
        draft = dict(name="片尾", wording="红色杯身", evidence="商品图 v1", scope="内部中文", expected_revision=0)
        decision = dict(name="片尾", expected_revision=1, reviewer="验收", reason="已核对实物图", decision="approved")
        self.assertEqual(save(draft), 1)
        self.assertEqual(save(draft), 1)
        self.assertEqual(save(decision, True), 1)
        self.assertEqual(save(decision, True), 1)
        with self.assertRaises(RequestConflict):
            save(dict(decision, decision="returned"), True)
        with self.assertRaises(RequestConflict):
            save(decision, True, "other")
        self.assertEqual(save(dict(draft, evidence="商品图 v2", expected_revision=1)), 2)
        with self.assertRaises(RequestConflict):
            save(decision, True)
        with self.assertRaises(RequestConflict):
            save(dict(draft, wording="过期草稿"))
        rows = list_copy(self.store, "sample")
        self.assertEqual([(r["decision"], r["evidence"]) for r in rows], [("pending", "商品图 v2"), ("approved", "商品图 v1")])
        self.assertEqual(list_copy(self.store, "other"), [])

    async def test_pending_cancel_is_not_claimed_as_confirmed(self):
        row = await self.accept()
        await self.send_pending()
        result = await self.store.cancel(self.store.find(row["id"]))
        self.assertEqual(result["cancel_requested"], 1)
        self.assertEqual(result["state"], "needs_reconciliation")

    async def test_dual_track_roundtrip_and_revision_conflict(self):
        try:
            import opentimelineio
        except ImportError:
            self.skipTest("OpenTimelineIO dependency not installed")
        row = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(row["id"]))
        clip = dict(job_id=row["id"], file_index=0, start_frame=27, duration_frames=48, rate="24")
        cut = {"rate": "24", "tracks": [
            {"kind": "Video", "name": "画面", "clips": [clip]},
            {"kind": "Audio", "name": "对白", "clips": [{"gap": True, "duration_frames": 6}, clip]},
        ]}
        new = save_edit(self.store, "fixture", 0, cut)
        with self.assertRaises(RequestConflict):
            save_edit(self.store, "fixture", 0, cut)
        exported = export_otio(self.store, "fixture", new["timeline"])
        self.assertIn("Timeline.1", exported)
        loaded = opentimelineio.adapters.read_from_string(exported, adapter_name="otio_json")
        self.assertEqual(len(loaded.tracks), 2)
        self.assertEqual(loaded.tracks[0][0].source_range.start_time.value, 27)
        self.assertEqual(import_otio(self.store, "fixture", exported, "24"), cut)
        effect = opentimelineio.schema.Effect(effect_name="Resolve Effect", metadata={
            "Resolve_OTIO": {"Enabled": True, "Parameters": [{
                "Default Parameter Value": 1.0, "Parameter Value": 1.0,
            }]},
        })
        loaded.tracks[0][0].effects.append(effect)
        resolve_defaults = opentimelineio.adapters.write_to_string(loaded, adapter_name="otio_json")
        self.assertEqual(import_otio(self.store, "fixture", resolve_defaults, "24"), cut)
        loaded.tracks[0][0].effects.append(opentimelineio.schema.LinearTimeWarp(time_scalar=2.0))
        modified_speed = opentimelineio.adapters.write_to_string(loaded, adapter_name="otio_json")
        with self.assertRaises(ValueError):
            import_otio(self.store, "fixture", modified_speed, "24")
        self.assertEqual(save_edit(self.store, "fixture", 1, cut)["revision"], 2)
        self.assertEqual(read_edit(self.store, "fixture")["revision"], 2)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM edits WHERE project='fixture'").fetchone()[0], 2)

    def test_review_maps_local_insertions_and_rejects_edits_across_a_boundary(self):
        incoming = {"before_at": 2, "before": ["C", "D"]}
        insertion = {"before_at": 0, "before": [], "after": ["new"]}
        self.assertEqual(_draft_span(incoming, [insertion]), (3, 5))
        crossing = {"before_at": 1, "before": ["B", "C"], "after": ["combined"]}
        self.assertIsNone(_draft_span(incoming, [crossing]))
        self.assertEqual(_draft_span({"before_at": 0, "before": []}, [insertion]), (0, 1))

    async def test_return_review_uses_the_delivered_cut_and_keeps_unregistered_layers(self):
        try:
            import opentimelineio as otio
        except ImportError:
            self.skipTest("OpenTimelineIO dependency not installed")
        row = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(row["id"]))
        clip = dict(job_id=row["id"], file_index=0, start_frame=0, duration_frames=12, rate="24")
        base = {"rate": "24", "tracks": [{"kind": "Video", "name": "画面", "clips": [clip, clip]}]}
        save_edit(self.store, "fixture", 0, base)
        newer = {"rate": "24", "tracks": [{**base["tracks"][0], "clips": [clip, dict(clip, start_frame=3)]}]}
        save_edit(self.store, "fixture", 1, newer)
        original = export_otio(self.store, "fixture", base)
        timeline = otio.adapters.read_from_string(original, adapter_name="otio_json")
        cut = timeline.tracks[0][0]
        cut.source_range = otio.opentime.TimeRange(cut.source_range.start_time, otio.opentime.RationalTime(11, 24))
        overlay = cut.deepcopy()
        overlay.media_reference.target_url = (self.root / "title.mov").as_uri()
        layer = otio.schema.Track(name="尾卡", kind="Video")
        layer.append(overlay)
        timeline.tracks.append(layer)
        edited = otio.adapters.write_to_string(timeline, adapter_name="otio_json")
        app = create_app(self.store.path, self.root / "input", 8210)
        endpoint = "/localhub/v2/timeline/otio?project=fixture"
        async with TestClient(TestServer(app), headers={"Host": "127.0.0.1:8210"}) as client:
            for suffix in ("", "&target=resolve"):
                exported = await client.get(endpoint + "&revision=1" + suffix)
                self.assertEqual(exported.status, 200)
                payload = await exported.text()
                media = otio.adapters.read_from_string(payload, adapter_name="otio_json").tracks[0][0]
                self.assertEqual(media.media_reference.target_url.startswith("file:"), not suffix or os.name != "nt")
                self.assertEqual(import_otio(self.store, "fixture", payload, "24"), base)
            unchanged = await client.post(endpoint, json=dict(preview=True, base_revision=1, otio=original))
            report = await unchanged.json()
            self.assertEqual((report["revision"], report["changes"], report["timeline"]), (2, [], base))
            self.assertTrue(report["current_differs"])
            preview = await client.post(endpoint, json=dict(preview=True, base_revision=1, otio=edited))
            report = await preview.json()
            self.assertIsNone(report["timeline"])
            main, added = report["changes"]
            self.assertEqual((main["index"], added["after_name"]), (1, "尾卡"))
            delta, = main["ranges"]
            self.assertEqual((delta["before_at"], delta["after_at"]), (0, 0))
            self.assertEqual([c["duration_frames"] for c in delta["after"]], [11])
            self.assertIn("unresolved", added["ranges"][0]["after"][0])
            self.assertIn("blocked", added["pick"])
            self.assertEqual((delta["pick"]["op"], delta["pick"]["at"], delta["pick"]["conflict"]),
                             ("splice", 0, False))
            draft = {"rate": "24", "tracks": [{**base["tracks"][0], "clips": [dict(clip, duration_frames=10), clip]}]}
            dirty = await client.post(endpoint, json=dict(preview=True, base_revision=1, otio=edited, draft=draft))
            conflict = (await dirty.json())["changes"][0]["ranges"][0]["pick"]
            self.assertTrue(conflict["conflict"])
            self.assertEqual(conflict["local"], draft["tracks"][0]["clips"][:1])
            strict = await client.post(endpoint, json=dict(expected_revision=2, otio=edited))
            self.assertEqual(strict.status, 400)
            missing = await client.post(endpoint, json=dict(preview=True, base_revision=3, otio=original))
            self.assertEqual(missing.status, 404)
        self.assertEqual(read_edit(self.store, "fixture"), {"revision": 2, "timeline": newer})

    async def test_imported_media_joins_generated_cut_without_a_queue_job(self):
        footage = (self.root / "足音.mov").resolve()
        room = (self.root / "room.wav").resolve()
        footage.write_bytes(b"camera")
        room.write_bytes(b"sound")
        real, created = self.store.register_media("fixture", "S01 camera", str(footage), "video", {
            "source": "imported", "frames": 92, "audio_frames": 90, "fps": 24, "has_audio": True,
            "file_stamp": file_stamp(footage),
        })
        self.assertTrue(created)
        self.assertIsNone(self.store.public(real)["prompt_id"])
        self.assertEqual(self.store.public(real)["delivery"], "ready")
        retry, created = self.store.register_media("fixture", "S01 camera", str(footage), "video", json.loads(real["info"]))
        self.assertEqual(retry["id"], real["id"])
        self.assertFalse(created)
        voice, _ = self.store.register_media("fixture", "Room", str(room), "audio", {
            "source": "imported", "frames": 150, "audio_frames": 150, "fps": 24, "has_audio": True,
            "file_stamp": file_stamp(room),
        })
        self.assertEqual(self.native.posts, [])
        generated = await self.accept()
        await self.send_pending()
        self.native.state = "completed"
        await self.store.refresh(self.store.find(generated["id"]))
        def cut_item(row, start, duration):
            return dict(job_id=row["id"], file_index=0, start_frame=start, duration_frames=duration, rate="24")
        cut = {"rate": "24", "tracks": [
            {"kind": "Video", "name": "Picture", "clips": [cut_item(real, 12, 50), cut_item(generated, 2, 44)]},
            {"kind": "Audio", "name": "Sound", "clips": [cut_item(real, 12, 55), cut_item(generated, 7, 39)]},
            {"kind": "Audio", "name": "Room", "clips": [{"gap": True, "duration_frames": 4}, cut_item(voice, 8, 90)]},
        ]}
        self.assertEqual(save_edit(self.store, "fixture", 0, cut)["timeline"], cut)
        with self.assertRaisesRegex(ValueError, "没有该轨道"):
            save_edit(self.store, "fixture", 1, {"tracks": [{"kind": "Video", "clips": [cut_item(voice, 8, 90)]}]})
        with self.assertRaisesRegex(ValueError, "超过候选长度"):
            save_edit(self.store, "fixture", 1, {"tracks": [{"kind": "Audio", "clips": [cut_item(real, 89, 2)]}]})
        try:
            import opentimelineio  # noqa: F401
        except ImportError:
            pass
        else:
            self.assertEqual(import_otio(self.store, "fixture", export_otio(self.store, "fixture", cut), "24"), cut)
        footage.write_bytes(b"different edit source")
        self.assertEqual(self.store.public(real)["delivery"], "missing")
        with self.assertRaisesRegex(ValueError, "缺失或已变动"):
            save_edit(self.store, "fixture", 1, cut)
        with self.assertRaises(RequestConflict):
            self.store.register_media("fixture", "S01 camera", str(footage), "video", {
                **json.loads(real["info"]), "file_stamp": file_stamp(footage),
            })

    def test_import_is_limited_to_staged_media(self):
        staged = self.root / "localhub_media"
        staged.mkdir()
        (staged / "manifest.m3u8").write_text("placeholder", encoding="utf-8")
        for path in ("../候选 media.mp4", str(self.file), "https://example.com/a.mp4", "manifest.m3u8"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                probe_media(staged, path, None)

    async def test_content_address_and_multimodal_graph_slots(self):
        active = types.SimpleNamespace(get_input_directory=lambda: str(self.root))
        with patch.dict(sys.modules, {"folder_paths": active}):
            import h3_graph
            source = self.root / "ref.png"
            source.write_bytes(b"first ref")
            first = h3_graph._copy_to_input(source)
            source.write_bytes(b"second ref")
            second = h3_graph._copy_to_input(source)
            self.assertNotEqual(first, second)
            self.assertEqual((self.root / first).read_bytes(), b"first ref")
            models = {"unet_ref2va": "ref2va", "text_encoder": "enc", "video_vae": "vvae", "audio_vae": "avae"}
            with patch.object(h3_graph, "_reference_video", return_value="ref24.mp4"):
                graph, info = h3_graph.build_h3_graph({
                    "mode": "r2v", "models": models, "prompt": "<Picture 1> <Audio 1> <Video 1> <Audio 2>",
                    "ref_images": [str(source)], "ref_videos": [{"path": "clip.mp4", "include_audio": True}],
                    "ref_audios": [str(self.file)],
                })
            inputs = graph["7"]["inputs"]
            self.assertEqual(inputs["ref_video_audios.ref_video_audio_0"], ["ref-video-components-0", 1])
            self.assertEqual(inputs["ref_videos.ref_video_0"], ["ref-video-components-0", 0])
            self.assertIn("ref_audios.ref_audio_0", inputs)
            self.assertEqual([ref["tag"] for ref in info["references"]], ["<Picture 1>", "<Audio 1>", "<Video 1>", "<Audio 2>"])
            self.assertEqual(graph["1"]["inputs"]["unet_name"], "ref2va")

    def test_reviewed_preflight_binds_take_and_reference_content(self):
        active = types.SimpleNamespace(get_input_directory=lambda: str(self.root))
        source = self.root / "frame.png"
        source.write_bytes(b"original picture")
        params = {
            "mode": "r2v", "models": {
                "unet_ref2va": "ref2va", "text_encoder": "enc", "video_vae": "vvae", "audio_vae": "avae",
            },
            "width": 390, "height": 230, "duration_s": 2.25, "seed": 17,
            "attention_backend": None, "prompt": "Continue <Picture 1>.", "ref_images": [str(source)],
        }
        take = {"start_frame": 2, "end_frame": 54}
        with patch.dict(sys.modules, {"folder_paths": active}):
            from localhub_preflight import compile_plan, confirm_plan

            _, info = compile_plan("drama", "S02", params, take)
            self.assertEqual((info["width"], info["height"], info["frames"]), (384, 224, 56))
            self.assertEqual(info["references"][0]["tag"], "<Picture 1>")
            approved = dict(params, take=take, plan_sha256=info["preflight"]["plan_sha256"], prefix="new/output")
            self.assertEqual(confirm_plan("drama", "S02", approved)[1]["preflight"], info["preflight"])
            with self.assertRaises(RequestConflict):
                confirm_plan("drama", "S02", dict(approved, take={"start_frame": 2, "end_frame": 53}))
            source.write_bytes(b"new picture at the same path")
            with self.assertRaises(RequestConflict):
                confirm_plan("drama", "S02", approved)
            with self.assertRaisesRegex(ValueError, "<Picture 2>"):
                compile_plan("drama", "S02", dict(params, prompt="Continue <Picture 2>."), take)

    async def test_upload_product_image_preserves_versions_and_rejects_invalid_bytes(self):
        root = self.root / "input"
        app = create_app(self.store.path, root, 8210)
        url = "/localhub/v2/references/upload?project=ad&asset_id=kettle&filename=hero.PNG"
        image = self.root / "chosen.png"
        async with TestClient(TestServer(app), headers={"Host": "127.0.0.1:8210"}) as client:
            self.assertEqual((await client.post(url, data=b"bad image")).status, 400)
            self.assertEqual(list(root.glob("localhub_*png")), [])
            Image.new("RGB", (40, 30), "red").save(image)
            r = await client.post(url, data=image.read_bytes())
            self.assertEqual(r.status, 201)
            first = (await r.json())["reference"]
            self.assertEqual((first["revision"], first["width"], first["height"]), (1, 40, 30))
            self.assertEqual((await client.post(url, data=image.read_bytes())).status, 200)
            self.assertEqual((await client.post(url.replace("hero.PNG", "hero.jpeg"), data=image.read_bytes())).status, 400)
            Image.new("RGB", (30, 40), "blue").save(image)
            second = (await (await client.post(url, data=image.read_bytes())).json())["reference"]
            self.assertEqual((second["revision"], second["width"], second["height"]), (2, 30, 40))
            self.assertEqual(list_references(self.store, "ad"), [second, first])
            self.assertEqual(resolve_reference(self.store, root, "ad", {"asset_id":"kettle","revision":1})[1], first)

    def test_product_reference_keeps_old_revision_after_replacing_same_file(self):
        stage = self.root / "localhub_media"
        stage.mkdir()
        image = stage / "hero.png"
        Image.new("RGB", (48, 32), "red").save(image)
        first, _ = register_reference(self.store, self.root, "ad", "mug", image.name)
        self.assertEqual(register_reference(self.store, self.root, "ad", "mug", image.name), (first, False))
        Image.new("RGB", (32, 48), "blue").save(image)
        second, created = register_reference(self.store, self.root, "ad", "mug", image.name)
        self.assertTrue(created)
        self.assertEqual((first["revision"], second["revision"]), (1, 2))
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(list_references(self.store, "ad"), [second, first])
        old, _ = resolve_reference(self.store, self.root, "ad", first)
        with Image.open(old) as snapshot:
            self.assertEqual(snapshot.getpixel((0, 0)), (255, 0, 0))
        with self.assertRaises(ValueError):
            resolve_reference(self.store, self.root, "other", first)

        with patch.dict(sys.modules, {"folder_paths": types.SimpleNamespace(get_input_directory=lambda: str(self.root))}):
            from localhub_preflight import compile_plan, confirm_plan

            def resolve(choice):
                return resolve_reference(self.store, self.root, "ad", choice)

            params = {
                "mode": "i2v", "models": {"unet": "u", "text_encoder": "e", "video_vae": "v", "audio_vae": "a"},
                "prompt": "Slow push toward the mug.", "first_frame": {"asset_id": "mug", "revision": 1},
                "attention_backend": None, "width": 512, "height": 288, "seed": 7,
            }
            graph, info = compile_plan("ad", "S02", params, None, resolve)
            self.assertEqual(graph["20"]["inputs"]["image"], first["input_name"])
            self.assertEqual(info["references"][0]["version"]["revision"], 1)
            confirmed = dict(params, plan_sha256=info["preflight"]["plan_sha256"], take=None)
            confirm_plan("ad", "S02", confirmed, resolve)
            with self.assertRaises(RequestConflict):
                confirm_plan("ad", "S02", dict(confirmed, first_frame={"asset_id": "mug", "revision": 2}), resolve)
            Path(old).write_bytes(b"corrupt snapshot")
            with self.assertRaisesRegex(RequestConflict, "快照"):
                confirm_plan("ad", "S02", confirmed, resolve)

    def test_read_specific_edit_revision(self):
        v1 = {"rate": "24", "tracks": [{"kind": "Video", "clips": [{"gap": True, "duration_frames": 12}]}]}
        v2 = {"rate": "24", "tracks": [{"kind": "Video", "clips": [{"gap": True, "duration_frames": 36}]}]}
        original = save_edit(self.store, "ad", 0, v1)
        latest = save_edit(self.store, "ad", 1, v2)
        self.assertEqual(read_edit(self.store, "ad", 1), original)
        self.assertEqual(read_edit(self.store, "ad"), latest)
        with self.assertRaises(LookupError):
            read_edit(self.store, "ad", 3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe are required")
    def test_cut_audio_measures_only_used_samples_and_maps_gaps_and_repeated_clips(self):
        source = self.root / "dialogue.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            tone = (b"\x00\x20" * 48 + b"\x00\xe0" * 48) * 500
            wav.writeframes(bytes(96000) + tone)
        path, kind, info = probe_media(self.root, source.name, "ffprobe")
        job = self.store.register_media("dialogue", "S02", path, kind, info)[0]
        clip = dict(job_id=job["id"], file_index=0, rate="48", duration_frames=48, start_frame=48)
        gap = dict(gap=True, duration_frames=6)
        cut = dict(rate="24", tracks=[dict(kind="Audio", name="对白", clips=[gap, clip, dict(clip, start_frame=0)])])
        save_edit(self.store, "dialogue", 0, cut)
        cut["tracks"][0]["clips"] = [clip]
        save_edit(self.store, "dialogue", 1, cut)
        report = inspect_cut_audio(self.store, "dialogue", 1, "ffmpeg", "ffprobe")
        lead, spoken, silent = report["segments"]
        self.assertTrue(lead["gap"])
        self.assertEqual((spoken["start_frame"], spoken["end_frame"]), (6, 30))
        self.assertEqual(spoken["silent_ranges"], [])
        self.assertIn("no_signal", silent["flags"])
        self.assertEqual(silent["silent_ranges"], [dict(start_frame=30, end_frame=54)])
        updated = inspect_cut_audio(self.store, "dialogue", 2, "ffmpeg", "ffprobe", track_index=0)
        self.assertEqual(updated["status"], "pass")
        with self.assertRaises(ValueError):
            inspect_cut_audio(self.store, "dialogue", 2, "ffmpeg", "ffprobe", track_index=1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe are required")
    def test_audio_report_detects_full_scale_silence_and_timing(self):
        candidate = self.root / "dialogue-fault.mov"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
            "-f", "lavfi", "-i", "color=black:size=96x64:rate=24:duration=3",
            "-itsoffset", "0.375", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=0.7",
            "-map", "0:v", "-map", "1:a", "-af", r"aeval=clip(val(0)*24\,-1\,1),apad=pad_dur=0.65",
            "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(candidate),
        ], check=True, timeout=20)
        before = file_stamp(candidate)
        report = inspect_audio(candidate, "ffmpeg", "ffprobe")
        self.assertEqual(report["status"], "review")
        self.assertCountEqual(report["flags"], ["sample_at_full_scale", "extended_silence", "av_timing"])
        self.assertEqual(report["timing"]["audio_start_delta_s"], 0.375)
        self.assertGreater(report["audio"]["longest_silence_s"], 0.6)
        self.assertEqual(file_stamp(candidate), before)
        job = self.store.register_media("drama", "S02", str(candidate), "video",
                                        {"source": "imported", "file_stamp": before})[0]
        self.store.save_audio_report(job["id"], 0, before, report)
        self.assertEqual(self.store.audio_report(job["id"], 0, before), report)
        self.assertIsNone(self.store.audio_report(job["id"], 0, [before[0] + 1, before[1]]))
