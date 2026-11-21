"""Open an existing project database without initializing the generation service."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class RequestConflict(ValueError):
    pass


class ProjectDatabase:
    def __init__(self, database, *, writable=False):
        mode = "rw" if writable else "ro"
        self.uri = Path(database).resolve(strict=True).as_uri() + "?mode=" + mode

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def find(self, job_id):
        with self.db() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
