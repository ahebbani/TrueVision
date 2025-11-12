#!/usr/bin/env python3
import argparse
import os
import sqlite3
from datetime import datetime
from typing import Any, List, Tuple, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH_DEFAULT = os.path.join(BASE_DIR, 'database', 'faces.db')
REPORTS_DIR = os.path.join(BASE_DIR, 'reports')


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
        emb_stats = {row[0]: (row[1], row[2]) for row in emb_counts}  # face_id -> (count, avgq)

    if table_exists(conn, 'meetings'):
        meetings = _fetchall(
            conn,
            """
            SELECT m.id, m.person_id, f.name, m.started_at, m.ended_at,
                   COALESCE(LENGTH(m.transcript), 0) as tlen,
                   COALESCE(m.summary, '') as summary,
                   m.audio_path
            FROM meetings m
            LEFT JOIN faces f ON f.id = m.person_id
            ORDER BY COALESCE(m.started_at, m.id) DESC
            LIMIT ?
            """,
            (limit_meetings,),
        )

    return faces, emb_stats, meetings


def print_console(faces, emb_stats, meetings):
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
        for mid, pid, name, started_at, ended_at, tlen, summary, audio_path in meetings:
            name = name or f"person {pid}"
            start = started_at or '—'
            end = ended_at or '—'
            audio = (audio_path or '—')
            if len(audio) > 28:
                audio = '…' + audio[-27:]
            print(f"{mid:>3}  {name:<20}  {start:<19}  {end:<19}  {tlen:>16}  {audio:<30}")

    print("\nTip: run with --html to generate an HTML report under facial_recognition/reports/\n")


def write_html(faces, emb_stats, meetings, out_path: str):
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
    for mid, pid, name, started_at, ended_at, tlen, summary, audio_path in meetings:
        rows_meet.append(
            f"<tr><td>{mid}</td><td>{esc(name) or f'person {pid}'}</td><td>{esc(started_at) or '—'}</td><td>{esc(ended_at) or '—'}</td><td>{tlen}</td><td>{esc(audio_path) or '—'}</td><td>{esc(summary)[:160]}</td></tr>"
        )

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
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
<p class="muted">Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z</p>

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
<tr><th>ID</th><th>Person</th><th>Start</th><th>End</th><th>Transcript Chars</th><th>Audio</th><th>Summary (preview)</th></tr>
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
    parser.add_argument('--html', action='store_true', help='Generate HTML report in facial_recognition/reports')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return

    conn = sqlite3.connect(args.db)

    faces, emb_stats, meetings = summarize_db(conn, args.limit)

    if args.html:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        out_path = os.path.join(REPORTS_DIR, 'db_report.html')
        write_html(faces, emb_stats, meetings, out_path)
        print(f"HTML report written to: {out_path}")
    else:
        print_console(faces, emb_stats, meetings)

    conn.close()


if __name__ == '__main__':
    main()
