"""Run Local Hub Studio for editing and review without starting ComfyUI."""

import argparse
from pathlib import Path

from aiohttp import web

from localhub_jobs import Jobs
from localhub_project_http import _err, register_project_routes


def create_app(database, input_directory, port):
    input_directory = Path(input_directory).resolve()
    (input_directory / "localhub_media").mkdir(parents=True, exist_ok=True)
    jobs = Jobs(Path(database).resolve(), base_url=None, output_paths=None)
    host = "127.0.0.1" if port == 80 else f"127.0.0.1:{port}"
    origin = "http://" + host

    @web.middleware
    async def loopback_only(request, handler):
        if request.host != host or request.headers.get("Origin", origin) != origin:
            return _err(403, "请从制作台的本机地址访问")
        return await handler(request)

    app = web.Application(middlewares=[loopback_only])
    table = web.RouteTableDef()
    register_project_routes(app, table, jobs=lambda: jobs, input_directory=lambda: input_directory,
                            generation_enabled=False)
    app.add_routes(table)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="本机项目的 SQLite 文件")
    parser.add_argument("--input", required=True, type=Path, help="素材输入目录，内有 localhub_media")
    parser.add_argument("--port", type=int, default=8210)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    print(f"Local Hub Studio · http://127.0.0.1:{args.port}/localhub/studio")
    web.run_app(create_app(args.database, args.input, args.port), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
