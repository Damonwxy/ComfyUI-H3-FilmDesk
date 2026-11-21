"""Search existing projects without choosing a target or opening a cut."""

import json

def find_projects(jobs, query=""):
    words = query.casefold().split()
    projects = {}

    def entry(name):
        return projects.setdefault(name, {"project": name, "plan_revision": 0, "cut_revision": 0,
                                          "jobs": 0, "title": name, "brief": "", "genre": None})

    with jobs.db() as db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("reference_images", "ad_copy", "deliveries", "legacy_canvas_snapshots"):
            if table in tables:
                for name, in db.execute(f"SELECT DISTINCT project FROM {table}"):
                    entry(name)
        if "jobs" in tables:
            for name, count in db.execute("SELECT project,COUNT(*) FROM jobs GROUP BY project"):
                entry(name)["jobs"] = count
        if "edits" in tables:
            for name, revision in db.execute("SELECT project,MAX(revision) FROM edits GROUP BY project"):
                entry(name)["cut_revision"] = revision
        if "project_plans" in tables:
            rows = db.execute("""SELECT p.project,p.revision,p.data FROM project_plans p
                JOIN (SELECT project,MAX(revision) AS revision FROM project_plans GROUP BY project)
                USING(project,revision)""")
            for name, revision, raw in rows:
                plan = json.loads(raw)
                entry(name).update(plan_revision=revision, title=plan["title"], genre=plan["genre"], brief=plan["brief"])
    matches = []
    for row in sorted(projects.values(), key=lambda item: item["project"].casefold()):
        searchable = " ".join([row["project"], row["title"], row["brief"], row["genre"] or ""]).casefold()
        if all(word in searchable for word in words):
            matches.append({**row, "brief": row["brief"][:160]})
    return {"projects": matches, "needs_choice": len(matches) > 1}


def register_project_search(table, jobs):
    from aiohttp import web

    async def search(request):
        return web.json_response({"ok": True, **find_projects(jobs(), request.query.get("q", ""))})

    table.get("/localhub/v2/projects")(search)
