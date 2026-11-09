#!/usr/bin/env python3
"""Serve lesson 02 on the teacher's network and submit group proposals to GitHub."""

import argparse
import base64
import hmac
import ipaddress
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import subprocess
import threading
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "GiorgosBouh/movable_school_submissions"
TASKS = ("01-eisodos", "02-diadromos", "03-ergastirio", "04-parousiasi")
TITLES = (
    "Είσοδος και αλλαγή αίθουσας", "Διάλειμμα, κυλικείο και τουαλέτα",
    "Τάξη και εργαστήριο", "Συγκέντρωση και Γυμναστική",
)
MAX_BODY = 20000
MAX_TEXT = 5000


def github(path, payload=None):
    command = ["gh", "api", path]
    if payload is not None:
        command += ["--method", "PUT", "--input", "-"]
    return subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        text=True, capture_output=True, timeout=25, check=False,
    )


def verify_destination():
    response = github(f"repos/{REPOSITORY}")
    if response.returncode:
        raise RuntimeError(f"Συνδέσου στο GitHub με gh auth login και έλεγξε την πρόσβαση στο {REPOSITORY}.")
    repository = json.loads(response.stdout)
    if not repository["private"] or not repository["permissions"]["push"]:
        raise RuntimeError("Η υποβολή χρειάζεται ιδιωτικό αποθετήριο με δικαίωμα εγγραφής.")


def save_submission(path, proposal):
    endpoint = f"repos/{REPOSITORY}/contents/{path}"
    content = base64.b64encode(proposal.encode("utf-8")).decode("ascii")
    response = github(endpoint, {"message": f"Add group proposal for {path.split('/')[-2]}", "content": content})
    if response.returncode == 0:
        return True
    # A retry may be for a commit that succeeded before the browser received its reply.
    existing = github(endpoint)
    if existing.returncode:
        return False
    stored = json.loads(existing.stdout)
    return stored.get("encoding") == "base64" and "".join(stored.get("content", "").split()) == content


class LessonServer(ThreadingHTTPServer):
    def __init__(self, address, code, date):
        super().__init__(address, LessonHandler)
        self.origin = f"http://{address[0]}:{self.server_port}"
        self.code = code
        self.date = date
        self.writes = threading.Lock()


class LessonHandler(BaseHTTPRequestHandler):
    timeout = 15

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, **result):
        self.send_body(status, json.dumps(result).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            links = "".join(
                f'<li><a href="/lesson-02/{task}.html">Ομάδα {index} · {title}</a></li>'
                for index, (task, title) in enumerate(zip(TASKS, TITLES), 1)
            )
            html = f"""<!doctype html><html lang="el"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MoveAble School · Υποβολές</title>
<style>body{{max-width:42rem;margin:3rem auto;padding:0 1rem;font:1.1rem/1.8 system-ui;color:#152820}}li{{margin:.6rem 0}}a{{color:#155f53}}</style>
</head><body><main><h1>MoveAble School</h1><p>Μάθημα 2 · Επιλέξτε την ομάδα σας.</p>
<ul>{links}</ul><p>Στο τέλος, ζητήστε από τον εκπαιδευτικό τον κωδικό του μαθήματος.</p></main></body></html>"""
            self.send_body(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        for task in TASKS:
            if path == f"/lesson-02/{task}.html":
                html = (ROOT / path.lstrip("/")).read_text(encoding="utf-8")
                html = html.replace('<body>', f'<body data-classroom="true" data-classroom-date="{self.server.date}">', 1)
                self.send_body(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/api/submissions":
            self.send_json(404, error="Η διεύθυνση υποβολής δεν βρέθηκε.")
            return
        if self.headers.get("Origin") != self.server.origin:
            self.send_json(403, error="Ανοίξτε τη δραστηριότητα από τον σύνδεσμο της τάξης.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get_content_type() != "application/json":
                raise ValueError
            data = json.loads(self.rfile.read(length))
            task, code, proposal, request_id = (data[key] for key in ("task", "code", "proposal", "requestId"))
            if not all(isinstance(value, str) for value in (task, code, proposal, request_id)):
                raise ValueError
            if task not in TASKS or not 0 < len(proposal.strip()) <= MAX_TEXT or str(UUID(request_id)) != request_id:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            self.send_json(400, error="Η πρόταση δεν ήταν έγκυρη. Ετοιμάστε την ξανά και δοκιμάστε.")
            return
        if not hmac.compare_digest(code.encode("utf-8"), self.server.code.encode("utf-8")):
            self.send_json(403, error="Ο κωδικός δεν είναι σωστός. Ζητήστε τον κωδικό του μαθήματος.")
            return

        path = f"submissions/{self.server.date}/lesson-02/{task}/{request_id}.txt"
        try:
            with self.server.writes:
                verify_destination()
                saved = save_submission(path, proposal)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            saved = False
        if not saved:
            self.send_json(503, error="Δεν επιβεβαιώθηκε η υποβολή. Η πρόταση είναι στην καρτέλα σας. Δοκιμάστε ξανά.")
            return
        self.send_json(201, path=path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="IPv4 διεύθυνση του υπολογιστή στο δίκτυο της τάξης")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        address = ipaddress.IPv4Address(args.host)
        if address.is_unspecified or not (address.is_private or address.is_loopback):
            raise ValueError
    except ValueError:
        parser.error("Δώσε τη συγκεκριμένη ιδιωτική IPv4 του υπολογιστή, π.χ. 192.168.1.25.")
    try:
        verify_destination()
        with LessonServer((str(address), args.port), secrets.token_urlsafe(9), datetime.now(ZoneInfo("Europe/Athens")).date().isoformat()) as server:
            print(f"Σύνδεσμος τάξης: {server.origin}/\nΚωδικός μαθήματος: {server.code}\nCtrl+C για τερματισμό.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
