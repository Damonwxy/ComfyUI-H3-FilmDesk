"""Read and revise a project plan or cut without starting ComfyUI."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from localhub_storage import ProjectDatabase


def read_board(database, project, revision=None):
    from localhub_media import candidate_file
    from localhub_timeline import read_edit_in_db

    db = ProjectDatabase(database)
    with db.db() as connection:
        connection.execute("BEGIN")
        edit = read_edit_in_db(connection, project, revision)
        rows = connection.execute("SELECT id,shot,kind,state,files,info FROM jobs WHERE project=? ORDER BY created,id", (project,))
        candidates = []
        for row in rows:
            info = json.loads(row["info"])
            files = [{"index": i, "path": path, "available": candidate_file(path, info)}
                     for i, path in enumerate(json.loads(row["files"]))]
            candidates.append({"id": row["id"], "shot": row["shot"], "kind": row["kind"], "state": row["state"],
                               "source": info.get("source", "generated"), "outputs": files})
            if "planned_shot" in info:
                candidates[-1]["planned_shot"] = info["planned_shot"]
    return {"project": project, "candidates": candidates, **edit}


def main():
    from localhub_project_plan import plan_cut, read_project_plan, save_project_plan
    from localhub_timeline import read_edit, save_edit

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("project")
    commands = parser.add_subparsers(dest="operation", required=True)
    read = commands.add_parser("read", help="读取候选与剪辑，并可导出可编辑的草稿")
    read.add_argument("--revision", type=int)
    read.add_argument("--draft", type=Path)
    save = commands.add_parser("save", help="核对项目与基准版号后保存新剪辑")
    save.add_argument("draft", type=Path)
    plan = commands.add_parser("plan", help="读取项目编排，含剧本、分集、场次和镜头")
    plan.add_argument("--revision", type=int)
    plan.add_argument("--draft", type=Path)
    plan_save = commands.add_parser("save-plan", help="核对基准版号后保存项目编排")
    plan_save.add_argument("draft", type=Path)
    assemble = commands.add_parser("plan-cut", help="从已存编排按镜头顺序起剪辑草稿")
    assemble.add_argument("--revision", type=int)
    assemble.add_argument("--episode")
    assemble.add_argument("--draft", type=Path)
    args = parser.parse_args()
    if not args.project.strip() or len(args.project) > 120:
        parser.error("项目名须为 1–120 字符")
    args.project = args.project.strip()
    if args.operation in ("save", "save-plan"):
        draft = json.loads(args.draft.read_text(encoding="utf-8-sig"))
        if not isinstance(draft, dict) or draft.get("project") != args.project:
            raise ValueError("草稿属于另一个项目")
        operation, field = (save_edit, "timeline") if args.operation == "save" else (save_project_plan, "plan")
        result = operation(ProjectDatabase(args.database, writable=True), args.project,
                           draft.get("expected_revision"), draft.get(field))
    else:
        db = ProjectDatabase(args.database)
        if args.operation == "read":
            result = read_board(args.database, args.project, args.revision)
            expected, field = result["revision"], "timeline"
        elif args.operation == "plan":
            result = read_project_plan(db, args.project, args.revision)
            expected, field = result["revision"], "plan"
        else:
            expected = read_edit(db, args.project)["revision"]
            result = plan_cut(db, args.project, args.revision, args.episode)
            field = "timeline"
        if args.draft:
            draft = {"project": args.project, "expected_revision": expected, field: result[field]}
            with args.draft.open("x", encoding="utf-8") as output:
                json.dump(draft, output, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, LookupError, OSError, sqlite3.Error) as error:
        sys.exit(str(error))
