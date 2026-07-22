import json
import math
import sqlite3
import subprocess
import re
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from data_sources import DataSourceError, fetch_upcoming_matches

ROOT = Path(__file__).parent
DB_PATH = ROOT / "football_predict.db"
UPLOAD_DIR = ROOT / "uploads"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    UPLOAD_DIR.mkdir(exist_ok=True)
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            aliases TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            handicap REAL DEFAULT 0,
            source TEXT DEFAULT 'manual/demo',
            source_id TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS team_stats (
            team TEXT PRIMARY KEY,
            home_attack REAL NOT NULL,
            away_attack REAL NOT NULL,
            home_defense REAL NOT NULL,
            away_defense REAL NOT NULL,
            form REAL NOT NULL DEFAULT 1.0
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        CREATE TABLE IF NOT EXISTS results (
            event_id INTEGER PRIMARY KEY,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        """
    )
    # Existing local databases created before source_id was introduced remain usable.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "source_id" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN source_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_source_id_unique ON events(source, source_id) WHERE source_id IS NOT NULL")
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count == 0:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            ("英超", "阿森纳", "切尔西", "2026-08-15 22:00", 0.5),
            ("西甲", "皇家马德里", "塞维利亚", "2026-08-16 03:00", -1.0),
            ("德甲", "拜仁慕尼黑", "多特蒙德", "2026-08-16 21:30", -0.5),
        ]
        conn.executemany(
            "INSERT INTO events(league,home_team,away_team,kickoff_at,handicap,updated_at) VALUES(?,?,?,?,?,?)",
            [r + (now,) for r in rows],
        )
        stats = [
            ("阿森纳", 1.45, 1.15, 0.82, 1.02, 1.08),
            ("切尔西", 1.18, 1.02, 1.04, 1.16, 0.98),
            ("皇家马德里", 1.62, 1.40, 0.72, 0.90, 1.12),
            ("塞维利亚", 0.94, 0.86, 1.18, 1.24, 0.92),
            ("拜仁慕尼黑", 1.72, 1.45, 0.78, 0.98, 1.10),
            ("多特蒙德", 1.34, 1.20, 1.06, 1.18, 1.00),
        ]
        conn.executemany("INSERT INTO team_stats VALUES(?,?,?,?,?,?)", stats)
    conn.commit()
    conn.close()


def poisson(k, lam):
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def predict(event):
    conn = db()
    hs = conn.execute("SELECT * FROM team_stats WHERE team=?", (event["home_team"],)).fetchone()
    aw = conn.execute("SELECT * FROM team_stats WHERE team=?", (event["away_team"],)).fetchone()
    conn.close()
    # Fallback values keep new or incomplete teams usable, with low confidence.
    hs = hs or {"home_attack": 1.15, "home_defense": 1.05, "form": 1.0}
    aw = aw or {"away_attack": 1.05, "away_defense": 1.10, "form": 1.0}
    home_xg = max(0.15, hs["home_attack"] * aw["away_defense"] * hs["form"] * 1.08)
    away_xg = max(0.15, aw["away_attack"] * hs["home_defense"] * aw["form"] * 0.94)
    matrix = {(h, a): poisson(h, home_xg) * poisson(a, away_xg) for h in range(7) for a in range(7)}
    home = sum(p for (h, a), p in matrix.items() if h > a)
    draw = sum(p for (h, a), p in matrix.items() if h == a)
    away = sum(p for (h, a), p in matrix.items() if h < a)
    total = {str(n): sum(p for (h, a), p in matrix.items() if h + a == n) for n in range(6)}
    total["5+"] = sum(p for (h, a), p in matrix.items() if h + a >= 5)
    half_home, half_away = home_xg * 0.44, away_xg * 0.44
    half_matrix = {(h, a): poisson(h, half_home) * poisson(a, half_away) for h in range(5) for a in range(5)}
    htft = {}
    for (hh, ha), hp in half_matrix.items():
        for (fh, fa), fp in matrix.items():
            ht = "胜" if hh > ha else "平" if hh == ha else "负"
            ft = "胜" if fh > fa else "平" if fh == fa else "负"
            htft[f"{ht}/{ft}"] = htft.get(f"{ht}/{ft}", 0) + hp * fp
    best_score = max(matrix, key=matrix.get)
    probs = {"主胜": round(home, 4), "平局": round(draw, 4), "客胜": round(away, 4)}
    best_1x2 = max(probs, key=probs.get)
    quality = 0.92 if hs and aw else 0.62
    return {
        "model_version": "poisson-mvp-0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_goals": {"home": round(home_xg, 2), "away": round(away_xg, 2)},
        "win_draw_loss": probs,
        "recommendation": best_1x2,
        "handicap": {"line": event["handicap"], "reference": "请结合让球方向确认胜平负含义"},
        "total_goals": {k: round(v, 4) for k, v in total.items()},
        "half_full": {k: round(v, 4) for k, v in sorted(htft.items(), key=lambda x: x[1], reverse=True)},
        "score": {"home": best_score[0], "away": best_score[1]},
        "confidence": round(min(0.9, max(0.45, quality * (0.72 + abs(home - away) * 0.5))), 2),
        "factors": [
            f"模型估计进球：主队 {home_xg:.2f}，客队 {away_xg:.2f}",
            "已使用主客场攻防与近期状态特征",
            "MVP 尚未接入实时首发、伤停和多源赔率交叉验证",
        ],
    }


def normalize_text(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.lower())


def match_events(ocr_text):
    """Find scheduled events whose two team names appear in the OCR text."""
    normalized = normalize_text(ocr_text)
    conn = db()
    events = conn.execute("SELECT * FROM events WHERE status='scheduled' ORDER BY kickoff_at").fetchall()
    conn.close()
    candidates = []
    for event in events:
        home, away = normalize_text(event["home_team"]), normalize_text(event["away_team"])
        home_hit, away_hit = home in normalized, away in normalized
        if home_hit or away_hit:
            score = (0.5 if home_hit else 0) + (0.5 if away_hit else 0)
            candidates.append({"event": dict(event), "match_score": score})
    return sorted(candidates, key=lambda item: item["match_score"], reverse=True)[:5]


def local_ocr(image_path):
    script = ROOT / "scripts" / "ocr.swift"
    completed = subprocess.run(
        ["/usr/bin/swift", str(script), str(image_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "本地 OCR 引擎运行失败")
    return json.loads(completed.stdout)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/events":
            conn = db()
            events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY kickoff_at")]
            conn.close()
            return self.send_json(events)
        if path.startswith("/api/events/"):
            event_id = path.rsplit("/", 1)[-1]
            conn = db()
            event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            conn.close()
            return self.send_json(dict(event) if event else {"error": "赛事不存在"}, 200 if event else 404)
        if path == "/api/predictions":
            conn = db()
            rows = conn.execute("""SELECT p.id, p.event_id, p.model_version, p.generated_at,
                e.league, e.home_team, e.away_team, p.payload,
                r.home_score, r.away_score
                FROM predictions p JOIN events e ON e.id=p.event_id
                LEFT JOIN results r ON r.event_id=p.event_id
                ORDER BY p.generated_at DESC LIMIT 100""").fetchall()
            conn.close()
            out = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item["payload"])
                item["result"] = None if item["home_score"] is None else f"{item['home_score']}:{item['away_score']}"
                out.append(item)
            return self.send_json(out)
        if path == "/api/health":
            return self.send_json({"ok": True, "database": str(DB_PATH.name)})
        if path == "/" or path == "/index.html":
            return self.serve_file(ROOT / "static" / "index.html", "text/html; charset=utf-8")
        return self.serve_file(ROOT / "static" / path.lstrip("/"), None)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/upload":
            return self.upload_image()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "请求格式错误"}, 400)
        if path == "/api/predict":
            conn = db()
            event = conn.execute("SELECT * FROM events WHERE id=?", (body.get("event_id"),)).fetchone()
            if not event:
                conn.close()
                return self.send_json({"error": "赛事不存在"}, 404)
            result = predict(event)
            conn.execute("INSERT INTO predictions(event_id,model_version,generated_at,payload) VALUES(?,?,?,?)", (event["id"], result["model_version"], result["generated_at"], json.dumps(result, ensure_ascii=False)))
            conn.commit()
            conn.close()
            return self.send_json({"event": dict(event), "prediction": result})
        if path == "/api/results":
            event_id = body.get("event_id")
            home, away = body.get("home_score"), body.get("away_score")
            if not isinstance(home, int) or not isinstance(away, int) or home < 0 or away < 0:
                return self.send_json({"error": "比分必须是非负整数"}, 400)
            conn = db()
            conn.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?)", (event_id, home, away, datetime.now(timezone.utc).isoformat()))
            conn.commit()
            conn.close()
            return self.send_json({"ok": True})
        if path == "/api/import-events":
            rows = body.get("events")
            if not isinstance(rows, list) or not rows:
                return self.send_json({"error": "events 必须是非空数组"}, 400)
            conn = db()
            now = datetime.now(timezone.utc).isoformat()
            inserted = 0
            for row in rows:
                required = [row.get(k) for k in ("league", "home_team", "away_team", "kickoff_at")]
                if any(not isinstance(v, str) or not v.strip() for v in required):
                    continue
                conn.execute("""INSERT INTO events(league,home_team,away_team,kickoff_at,handicap,source,updated_at)
                    VALUES(?,?,?,?,?,?,?)""", (row["league"], row["home_team"], row["away_team"], row["kickoff_at"], float(row.get("handicap", 0)), row.get("source", "import"), now))
                inserted += 1
            conn.commit(); conn.close()
            return self.send_json({"ok": True, "inserted": inserted})
        if path == "/api/sync/football-data":
            try:
                imported = fetch_upcoming_matches(days=min(max(int(body.get("days", 14)), 1), 30))
            except DataSourceError as exc:
                return self.send_json({"error": str(exc)}, 400)
            conn = db()
            now = datetime.now(timezone.utc).isoformat()
            inserted = 0
            for row in imported:
                existing = conn.execute("SELECT id FROM events WHERE source=? AND source_id=?", (row["source"], row["source_id"])).fetchone()
                if existing:
                    conn.execute("""UPDATE events SET league=?, home_team=?, away_team=?, kickoff_at=?, updated_at=? WHERE id=?""", (row["league"], row["home_team"], row["away_team"], row["kickoff_at"], now, existing["id"]))
                else:
                    conn.execute("""INSERT INTO events(league,home_team,away_team,kickoff_at,source,source_id,updated_at)
                        VALUES(?,?,?,?,?,?,?)""", (row["league"], row["home_team"], row["away_team"], row["kickoff_at"], row["source"], row["source_id"], now))
                    inserted += 1
            conn.commit(); conn.close()
            return self.send_json({"ok": True, "imported": inserted, "total": len(imported)})
        return self.send_json({"error": "接口不存在"}, 404)

    def upload_image(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self.send_json({"error": "请使用 multipart/form-data 上传图片"}, 400)
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
            )
            part = next((p for p in message.walk() if p.get_filename()), None)
            if not part:
                return self.send_json({"error": "没有找到图片文件"}, 400)
            mime = part.get_content_type()
            data = part.get_payload(decode=True) or b""
            if mime not in {"image/png", "image/jpeg", "image/webp"} or len(data) > 10 * 1024 * 1024:
                return self.send_json({"error": "仅支持不超过 10MB 的 PNG/JPG/WEBP 图片"}, 400)
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            image_id = str(uuid.uuid4())
            output_path = UPLOAD_DIR / f"{image_id}{suffix}"
            output_path.write_bytes(data)
            try:
                ocr = local_ocr(output_path)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                return self.send_json({"image_id": image_id, "status": "uploaded", "ocr_status": "unavailable", "message": f"图片已保存，但本地 OCR 暂不可用：{exc}"})
            text = "\n".join(item["text"] for item in ocr.get("lines", []))
            return self.send_json({
                "image_id": image_id,
                "status": "uploaded",
                "ocr_status": "completed",
                "text": text,
                "lines": ocr.get("lines", []),
                "candidates": match_events(text),
                "message": "图片已识别，请确认候选赛事。",
            })
        except Exception as exc:
            return self.send_json({"error": f"图片处理失败：{exc}"}, 400)

    def serve_file(self, path, content_type):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return self.send_json({"error": "资源不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("球赛智析运行于 http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
