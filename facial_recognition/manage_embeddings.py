import sqlite3
import argparse
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database', 'faces.db')


def get_stats(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM faces ORDER BY id")
    people = cur.fetchall()
    print("People and template counts:\n")
    for pid, name in people:
        cur.execute("SELECT COUNT(*), COALESCE(ROUND(AVG(quality),2), 'n/a') FROM face_embeddings WHERE face_id = ?", (pid,))
        count, avgq = cur.fetchone()
        print(f"- [{pid}] {name}: {count} templates (avg quality: {avgq})")


def prune_person(conn, face_id: int, keep: int):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, quality, created_at FROM face_embeddings WHERE face_id = ?",
        (face_id,),
    )
    rows = cur.fetchall()
    if len(rows) <= keep:
        print(f"Person {face_id}: nothing to prune (have {len(rows)} <= keep {keep})")
        return

    def sort_key(r):
        rid, q, ts = r
        qv = -1.0 if q is None else float(q)
        return (qv, ts or '')

    rows_sorted = sorted(rows, key=sort_key)
    to_remove = rows_sorted[: max(0, len(rows_sorted) - keep)]
    ids = [r[0] for r in to_remove]
    cur.executemany("DELETE FROM face_embeddings WHERE id = ?", [(i,) for i in ids])
    conn.commit()
    print(f"Pruned {len(ids)} templates for person {face_id}; kept {keep}")


def delete_person_templates(conn, face_id: int):
    cur = conn.cursor()
    cur.execute("DELETE FROM face_embeddings WHERE face_id = ?", (face_id,))
    conn.commit()
    print(f"Deleted all templates for person {face_id}")


def main():
    parser = argparse.ArgumentParser(description="Manage face embeddings/templates")
    parser.add_argument('--stats', action='store_true', help='Show template counts and avg quality per person')
    parser.add_argument('--prune', type=int, metavar='PERSON_ID', help='Prune templates for a person to --keep')
    parser.add_argument('--keep', type=int, default=30, help='How many templates to keep when pruning (default: 30)')
    parser.add_argument('--delete', type=int, metavar='PERSON_ID', help='Delete all templates for a person')

    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)

    # Ensure the table exists (in case user runs this before main.py created it)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS face_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_id INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL,
            quality REAL,
            FOREIGN KEY(face_id) REFERENCES faces(id)
        )
    """)
    conn.commit()

    if args.stats:
        get_stats(conn)
    elif args.prune is not None:
        prune_person(conn, args.prune, args.keep)
    elif args.delete is not None:
        delete_person_templates(conn, args.delete)
    else:
        parser.print_help()

    conn.close()


if __name__ == '__main__':
    main()
