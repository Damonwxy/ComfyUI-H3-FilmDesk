"""Identify a returned source from user-selected bytes and project candidates."""

import hashlib
import json
import re
from pathlib import Path

from localhub_media import candidate_file, file_stamp


def fingerprint(value):
    if (not isinstance(value, dict) or type(value.get("bytes")) is not int or value["bytes"] < 0
            or not isinstance(value.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", value["sha256"])):
        raise ValueError("素材核对需要文件大小与 SHA-256")
    return value["bytes"], value["sha256"]


def delivered_media(manifest, project, revision, base):
    if manifest is None:
        return {}
    if (not isinstance(manifest, dict) or manifest.get("format_version") != 1
            or manifest.get("project") != project or not isinstance(manifest.get("delivery"), dict)
            or manifest["delivery"].get("revision") != revision or manifest.get("timeline") != base
            or not isinstance(manifest.get("media"), list)):
        raise ValueError("交付清单与所选项目或交出剪版不一致")
    keys = {(clip["job_id"], clip["file_index"]) for track in base["tracks"]
            for clip in track["clips"] if not clip.get("gap")}
    records = {}
    for row in manifest["media"]:
        signature = fingerprint(row)
        if not isinstance(row.get("job_id"), str) or type(row.get("file_index")) is not int:
            raise ValueError("交付清单的候选标识不完整")
        key = row["job_id"], row["file_index"]
        if key not in keys or key in records:
            raise ValueError("清单包含重复或无关的候选")
        records[key] = signature
    if keys != records.keys():
        raise ValueError("交付清单缺少剪版使用的候选")
    return records


class ReturnMedia:
    def __init__(self, evidence, delivered):
        if not isinstance(evidence, dict):
            raise ValueError("素材核对记录无效")
        self.evidence = evidence
        self.delivered = delivered
        self.report = {}
        self.hashes = {}

    def available(self, path, info, signature):
        path = Path(path)
        if not candidate_file(path, info):
            return False
        if signature is None:
            return True
        try:
            stamp = file_stamp(path)
            if stamp[0] != signature[0]:
                return False
            cache_key = path, *stamp
            if cache_key not in self.hashes:
                with path.open("rb") as src:
                    digest = hashlib.file_digest(src, "sha256").hexdigest()
                if file_stamp(path) != stamp:
                    raise ValueError("项目素材在比对期间发生变化，请重新核对")
                self.hashes[cache_key] = digest
            return self.hashes[cache_key] == signature[1]
        except OSError:
            return False

    def resolve(self, url, rows, path_matches):
        if url in self.report:
            return self.report[url].get("selected")
        result = self.report[url] = {"choices": []}
        evidence = self.evidence.get(url)
        signature = fingerprint(evidence) if evidence is not None else None
        if signature is None and not path_matches:
            return None
        for row in rows:
            for index, raw in enumerate(json.loads(row["files"])):
                key = row["id"], index
                if signature is None and key not in path_matches:
                    continue
                path = Path(raw)
                if signature is not None and key in self.delivered and self.delivered[key] != signature:
                    continue
                if not self.available(path, json.loads(row["info"]), signature or self.delivered.get(key)):
                    continue
                result["choices"].append({"job_id": key[0], "file_index": index, "shot": row["shot"],
                                          "file": path.name, "delivered": key in self.delivered})
        choices = result["choices"]
        if signature is not None:
            result["verified_content"] = True
        substitute = signature in self.delivered.values() and not any(c["delivered"] for c in choices)
        result["needs_choice"] = bool(choices) and (len(choices) > 1 or substitute)
        chosen = evidence.get("choice") if evidence is not None else None
        if chosen is not None:
            choices = [c for c in choices if [c["job_id"], c["file_index"]] == chosen]
            if not choices:
                raise ValueError("所选候选与本次素材核对不符，请重新选择")
        if len(choices) == 1 and (chosen is not None or not result["needs_choice"]):
            result["selected"] = {k: choices[0][k] for k in ("job_id", "file_index")}
        return result.get("selected")
