#!/usr/bin/env python3
"""Extrae conversaciones que tocan ecografía a /tmp/eco_conversations.json (prod)."""
import sys, json
sys.path.insert(0, ".")
from app.session import _conn

Q = ("eco* OR ecograf* OR ecotom* OR doppler OR transvaginal OR transvajinal "
     "OR mamaria OR pardo OR ecotomograf*")

c = _conn()
phones = [r[0] for r in c.execute(
    "SELECT DISTINCT m.phone FROM messages m JOIN messages_fts f ON f.rowid=m.id "
    "WHERE f.messages_fts MATCH ?", (Q,)).fetchall()]

out = []
for p in phones:
    rows = c.execute(
        "SELECT direction, text, state, ts FROM messages WHERE phone=? ORDER BY id",
        (p,)).fetchall()
    msgs = [{"d": r[0], "t": (r[1] or ""), "s": r[2], "ts": r[3]} for r in rows]
    out.append({"phone": p[-4:], "n": len(msgs), "msgs": msgs})

out.sort(key=lambda x: -x["n"])
json.dump(out, open("/tmp/eco_conversations.json", "w"), ensure_ascii=False)
print("CONVS", len(out), "TOTAL_MSGS", sum(x["n"] for x in out))
