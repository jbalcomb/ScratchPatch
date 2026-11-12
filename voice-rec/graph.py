"""Send each archived note to the knowledge graph. ``GRAPH_KIND`` picks how:

* ``neo4j``: Cypher over Neo4j's HTTP API (``GRAPH_URL`` like http://localhost:7474, ``GRAPH_USER``,
  ``GRAPH_PASSWORD``, ``GRAPH_DATABASE`` default ``neo4j``). Works with Memgraph's Neo4j-compatible HTTP too.
  Builds::

      (:VoiceNote {id, title, summary, transcript, recorded, ...})
          -[:IN_CATEGORY]->(:Category {name})
          -[:TAGGED]->(:Tag {name})
          -[:RECORDED_ON]->(:Day {date})
          -[:MENTIONS]->(:Entity {name, type})      people, projects, places, organizations, topics
          -[:HAS_ACTION]->(:Action {text, done})

* ``webhook``: POST the note as JSON to ``GRAPH_URL`` (for Graphiti, LightRAG, n8n, your own ingester…),
  with ``GRAPH_PASSWORD`` sent as a Bearer token if set.
* empty / ``none``: skipped.

Re-sending a note replaces its edges, so archiving again never duplicates anything.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from datetime import datetime
from typing import Any

CYPHER = [
    """MERGE (n:VoiceNote {id: $id})
       SET n.title = $title, n.summary = $summary, n.transcript = $transcript, n.recorded = datetime($recorded),
           n.transcribed = CASE WHEN $transcribed IS NULL THEN null ELSE datetime($transcribed) END,
           n.duration = $duration, n.category = $category, n.tags = $tags, n.note_path = $note_path,
           n.source = 'voice-rec'""",
    """MATCH (n:VoiceNote {id: $id})-[r:IN_CATEGORY|TAGGED|RECORDED_ON|MENTIONS]->() DELETE r""",
    """MATCH (n:VoiceNote {id: $id})-[:HAS_ACTION]->(a:Action) DETACH DELETE a""",
    """MATCH (n:VoiceNote {id: $id}) MERGE (c:Category {name: $category}) MERGE (n)-[:IN_CATEGORY]->(c)""",
    """MATCH (n:VoiceNote {id: $id}) MERGE (d:Day {date: date($day)}) MERGE (n)-[:RECORDED_ON]->(d)""",
    """MATCH (n:VoiceNote {id: $id}) UNWIND $tags AS t MERGE (g:Tag {name: t}) MERGE (n)-[:TAGGED]->(g)""",
    """MATCH (n:VoiceNote {id: $id}) UNWIND $entities AS e
       MERGE (x:Entity {name: e.name}) ON CREATE SET x.type = e.type
       MERGE (n)-[:MENTIONS]->(x)""",
    """MATCH (n:VoiceNote {id: $id}) UNWIND range(0, size($actions) - 1) AS i
       CREATE (n)-[:HAS_ACTION]->(:Action {text: $actions[i], done: false, position: i})""",
]


def payload(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec["id"], "title": rec.get("title") or "", "summary": rec.get("summary") or "",
        "transcript": rec.get("transcript") or "", "recorded": rec["recorded_at"],
        "transcribed": rec.get("transcribed_at"), "duration": float(rec.get("duration_s") or 0),
        "category": rec.get("category") or "Inbox", "tags": list(rec.get("tags") or []),
        "entities": list(rec.get("entities") or []), "actions": list(rec.get("actions") or []),
        "note_path": rec.get("note_path") or "",
        "day": datetime.fromisoformat(rec["recorded_at"]).astimezone().date().isoformat(),
    }


def _post(url: str, body: dict, headers: dict[str, str], timeout: float = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}


def send(rec: dict[str, Any], env=os.environ) -> str:
    """Returns what was done ("" when not configured). Raises on failure."""
    kind = (env.get("GRAPH_KIND") or "").strip().lower()
    url = (env.get("GRAPH_URL") or "").strip().rstrip("/")
    if kind in ("", "none") or not url:
        return ""
    data = payload(rec)
    if kind == "webhook":
        token = env.get("GRAPH_PASSWORD") or ""
        _post(url, {"type": "voice-note", **data}, {"Authorization": f"Bearer {token}"} if token else {})
        return f"sent to {url}"
    if kind == "neo4j":
        user, password = env.get("GRAPH_USER") or "neo4j", env.get("GRAPH_PASSWORD") or ""
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        db = env.get("GRAPH_DATABASE") or "neo4j"
        statements = [{"statement": s, "parameters": data} for s in CYPHER]
        if not data["actions"]:
            statements = statements[:-1]  # range(0, -1) is empty in Neo4j, but skip it anyway
        reply = _post(f"{url}/db/{db}/tx/commit", {"statements": statements}, {"Authorization": f"Basic {auth}"})
        errors = reply.get("errors") or []
        if errors:
            raise RuntimeError("; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors)[:500])
        return f"Neo4j {url} ({db})"
    raise ValueError(f"GRAPH_KIND={kind!r} isn't one of neo4j, webhook, none")
