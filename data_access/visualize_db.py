#!/usr/bin/env python3
import argparse
import os
import sys
from datetime import datetime
from typing import Any, Tuple

import sqlite3

# Allow running this file directly (python data_access/visualize_db.py)
# by ensuring the repository root is on sys.path, then import data_access.db
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from data_access.db import DB_PATH as DB_PATH_DEFAULT, open_db

DOCS_REPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'db_report.html')


def _fetchall(conn: sqlite3.Connection, query: str, params: Tuple[Any, ...] = ()) -> list:
    cur = conn.cursor()
    cur.execute(query, params)
    return cur.fetchall()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def summarize_db(conn: sqlite3.Connection, limit_meetings: int = 10):
    faces = []
    emb_stats = {}
    meetings = []

    if table_exists(conn, 'faces'):
        faces = _fetchall(conn, "SELECT id, name, created_at, last_seen_at, seen_count FROM faces ORDER BY id")

    if table_exists(conn, 'face_embeddings'):
        emb_counts = _fetchall(conn, "SELECT face_id, COUNT(*), ROUND(AVG(COALESCE(quality, 0)), 2) FROM face_embeddings GROUP BY face_id")
        emb_stats = {row[0]: (row[1], row[2]) for row in emb_counts}

    if table_exists(conn, 'meetings'):
        meetings = _fetchall(
            conn,
            (
                """
                SELECT m.id, m.person_id, f.name, m.started_at, m.ended_at,
                       COALESCE(m.transcript, '') as transcript,
                       COALESCE(LENGTH(m.transcript), 0) as tlen,
                       COALESCE(m.summary, '') as summary,
                       m.audio_path
                FROM meetings m
                LEFT JOIN faces f ON f.id = m.person_id
                ORDER BY COALESCE(m.started_at, m.id) DESC
                LIMIT ?
                """
            ),
            (limit_meetings,),
        )

    return faces, emb_stats, meetings


def print_console(faces, emb_stats, meetings, show_text: bool = False):
    print("\n=== Faces ===")
    if not faces:
        print("(none)")
    else:
        print(f"{'ID':>3}  {'Name':<20}  {'Seen':>4}  {'Last Seen':<20}  {'Templates':>9}  {'AvgQ':>5}")
        for fid, name, created_at, last_seen_at, seen_count in faces:
            c, q = emb_stats.get(fid, (0, None))
            qstr = f"{q:.2f}" if q is not None else "n/a"
            last_str = last_seen_at or '—'
            print(f"{fid:>3}  {name:<20}  {seen_count:>4}  {last_str:<20}  {c:>9}  {qstr:>5}")

    print("\n=== Recent meetings ===")
    if not meetings:
        print("(none)")
    else:
        print(f"{'ID':>3}  {'Person':<20}  {'Start':<19}  {'End':<19}  {'Transcript chars':>16}  {'Audio':<30}")
        for mid, pid, name, started_at, ended_at, transcript, tlen, summary, audio_path in meetings:
            name = name or f"person {pid}"
            start = started_at or '—'
            end = ended_at or '—'
            audio = (audio_path or '—')
            if len(audio) > 28:
                audio = '…' + audio[-27:]
            print(f"{mid:>3}  {name:<20}  {start:<19}  {end:<19}  {tlen:>16}  {audio:<30}")

            if show_text:
                s = (summary or '').strip()
                t = (transcript or '').strip()
                print(f"      Summary: {s if s else '—'}")
                if t:
                    print("      Transcript:")
                    for ln in t.splitlines():
                        print(f"        {ln}")
                else:
                    print("      Transcript: —")

    print("\nTip: run with --html to generate an HTML report under docs/\n")


def write_html(faces, emb_stats, meetings, out_path: str, include_transcripts: bool = True):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    def esc(s: Any) -> str:
        return ('' if s is None else str(s)).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    rows_faces = []
    for fid, name, created_at, last_seen_at, seen_count in faces:
        c, q = emb_stats.get(fid, (0, None))
        qstr = f"{q:.2f}" if q is not None else "n/a"
        rows_faces.append(
            f"<tr><td>{fid}</td><td>{esc(name)}</td><td>{seen_count}</td><td>{esc(last_seen_at) or '—'}</td><td>{c}</td><td>{qstr}</td></tr>"
        )

    rows_meet = []
    disabled_html = "<span class='muted'>(disabled)</span>"
    for mid, pid, name, started_at, ended_at, transcript, tlen, summary, audio_path in meetings:
        transcript_html = ""
        if include_transcripts:
            t = esc(transcript)
            transcript_html = (
                f"<details><summary class='nowrap'>view ({tlen} chars)</summary>"
                f"<pre style='white-space:pre-wrap; margin:8px 0 0'>{t}</pre></details>"
            )
        rows_meet.append(
            "<tr>"
            f"<td>{mid}</td>"
            f"<td>{esc(name) or f'person {pid}'}</td>"
            f"<td class='nowrap'>{esc(started_at) or '—'}</td>"
            f"<td class='nowrap'>{esc(ended_at) or '—'}</td>"
            f"<td>{tlen}</td>"
            f"<td>{esc(audio_path) or '—'}</td>"
            f"<td>{esc(summary) or '—'}</td>"
            f"<td>{transcript_html if transcript_html else disabled_html}</td>"
            "</tr>"
        )

    html = f"""
<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<title>TrueVision Database Report</title>
<style>
 body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; margin: 24px; }}
 h1, h2 {{ margin: 0 0 12px; }}
 table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; }}
 th, td {{ border: 1px solid #ddd; padding: 8px; font-size: 14px; }}
 th {{ background: #f5f5f5; text-align: left; }}
 small {{ color: #666; }}
 .muted {{ color: #777; }}
 .nowrap {{ white-space: nowrap; }}
</style>
</head>
<body>
<h1>TrueVision Database Report</h1>
<p class=\"muted\">Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z</p>

<h2>Faces</h2>
<table>
<thead>
<tr><th>ID</th><th>Name</th><th>Seen</th><th>Last Seen</th><th>Templates</th><th>Avg Quality</th></tr>
</thead>
<tbody>
{''.join(rows_faces)}
</tbody>
</table>

<h2>Recent Meetings</h2>
<table>
<thead>
<tr><th>ID</th><th>Person</th><th>Start</th><th>End</th><th>Transcript Chars</th><th>Audio</th><th>Summary</th><th>Transcript</th></tr>
</thead>
<tbody>
{''.join(rows_meet)}
</tbody>
</table>

<p><small>Report path: {esc(out_path)}</small></p>
</body>
</html>
"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)


def main():
    parser = argparse.ArgumentParser(description="Visualize TrueVision SQLite database")
    parser.add_argument('--db', default=DB_PATH_DEFAULT, help='Path to faces.db (default: %(default)s)')
    parser.add_argument('--limit', type=int, default=10, help='Number of recent meetings to show (default: %(default)s)')
    parser.add_argument('--html', action='store_true', help='Generate HTML report in docs/')
    parser.add_argument('--show-text', action='store_true', help='Print full transcript + summary in console output')
    parser.add_argument('--no-transcripts', action='store_true', help='In HTML mode, omit transcripts (smaller report)')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return

    conn = open_db(args.db)

    faces, emb_stats, meetings = summarize_db(conn, args.limit)

    if args.html:
        out_path = DOCS_REPORT_PATH
        write_html(faces, emb_stats, meetings, out_path, include_transcripts=(not bool(args.no_transcripts)))
        print(f"HTML report written to: {out_path}")
    else:
        print_console(faces, emb_stats, meetings, show_text=bool(args.show_text))

    conn.close()


if __name__ == '__main__':
    main()