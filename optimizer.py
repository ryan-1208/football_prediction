"""Safe, data-driven model evaluation and calibration for the MVP."""

import json
import math
from datetime import datetime, timezone


def init_optimizer_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS feature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            features TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_versions (
            version TEXT PRIMARY KEY,
            model_type TEXT NOT NULL,
            parameters TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version TEXT NOT NULL,
            market TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            accuracy REAL,
            log_loss REAL,
            brier_score REAL,
            evaluated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prediction_evaluations (
            prediction_id INTEGER PRIMARY KEY,
            market TEXT NOT NULL,
            actual TEXT NOT NULL,
            predicted TEXT NOT NULL,
            hit INTEGER NOT NULL,
            evaluated_at TEXT NOT NULL
        );
        """
    )
    active = conn.execute("SELECT version FROM model_versions WHERE status='active' LIMIT 1").fetchone()
    if not active:
        conn.execute(
            "INSERT OR IGNORE INTO model_versions VALUES(?,?,?,?,?)",
            ("poisson-mvp-0.2", "poisson", json.dumps({"temperature": 1.0}), "active", now()),
        )
    elif active["version"] == "poisson-mvp-0.1":
        # Keep existing databases aligned with the corrected, coherent
        # probability engine without rewriting historical predictions.
        conn.execute(
            "UPDATE model_versions SET version=?, model_type=? WHERE version=?",
            ("poisson-mvp-0.2", "poisson", "poisson-mvp-0.1"),
        )


def now():
    return datetime.now(timezone.utc).isoformat()


def outcome(home, away):
    return "主胜" if home > away else "平局" if home == away else "客胜"


def _result_probabilities(home_expected, away_expected, cap=12):
    matrix = {}
    for home_goals in range(cap + 1):
        home_probability = math.exp(-home_expected) * (home_expected ** home_goals) / math.factorial(home_goals)
        for away_goals in range(cap + 1):
            away_probability = math.exp(-away_expected) * (away_expected ** away_goals) / math.factorial(away_goals)
            matrix[(home_goals, away_goals)] = home_probability * away_probability
    total = sum(matrix.values()) or 1.0
    outcomes = [0.0, 0.0, 0.0]
    for (home_goals, away_goals), probability in matrix.items():
        normalized = probability / total
        outcomes[0 if home_goals > away_goals else 1 if home_goals == away_goals else 2] += normalized
    return outcomes


def probabilities(payload, goal_scales=None, features=None):
    if goal_scales:
        expected = payload.get("expected_goals") or {}
        features = features or {}
        home_expected = features.get("home_xg_base", expected.get("home", 1.2))
        away_expected = features.get("away_xg_base", expected.get("away", 1.1))
        home_scale = max(0.75, min(1.25, float(goal_scales.get("home", 1.0))))
        away_scale = max(0.75, min(1.25, float(goal_scales.get("away", 1.0))))
        return _result_probabilities(
            max(0.15, float(home_expected) * home_scale),
            max(0.15, float(away_expected) * away_scale),
        )
    values = payload["win_draw_loss"]
    return [max(1e-7, float(values["主胜"])), max(1e-7, float(values["平局"])), max(1e-7, float(values["客胜"]))]


def calibrate(values, temperature):
    # Temperature scaling preserves the ranking while making probabilities
    # less overconfident or more decisive based on historical evidence.
    powered = [value ** (1.0 / max(0.1, temperature)) for value in values]
    total = sum(powered)
    return [value / total for value in powered]


def _row_value(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def metrics(rows, temperature=1.0, goal_scales=None):
    labels = {"主胜": 0, "平局": 1, "客胜": 2}
    correct = 0
    log_loss = 0.0
    brier = 0.0
    for row in rows:
        payload = json.loads(row["payload"])
        features_raw = _row_value(row, "features")
        features = json.loads(features_raw) if features_raw else None
        probs = calibrate(probabilities(payload, goal_scales=goal_scales, features=features), temperature)
        actual = labels[row["actual"]]
        predicted = max(range(3), key=lambda index: probs[index])
        correct += int(predicted == actual)
        log_loss -= math.log(max(probs[actual], 1e-7))
        brier += sum((probs[index] - (1.0 if index == actual else 0.0)) ** 2 for index in range(3))
    count = len(rows)
    return {
        "sample_count": count,
        "accuracy": round(correct / count, 4) if count else None,
        "log_loss": round(log_loss / count, 4) if count else None,
        "brier_score": round(brier / count, 4) if count else None,
    }


def record_evaluation(conn, prediction_id, home_score, away_score):
    row = conn.execute("SELECT payload FROM predictions WHERE id=?", (prediction_id,)).fetchone()
    if not row:
        return False
    payload = json.loads(row["payload"])
    actual = outcome(home_score, away_score)
    predicted = payload["recommendation"]
    conn.execute(
        "INSERT OR REPLACE INTO prediction_evaluations VALUES(?,?,?,?,?,?)",
        (prediction_id, "win_draw_loss", actual, predicted, int(actual == predicted), now()),
    )
    return True


def evaluated_rows(conn):
    return conn.execute(
        """SELECT p.payload, e.actual, f.features, r.home_score, r.away_score FROM predictions p
           LEFT JOIN feature_snapshots f ON f.prediction_id=p.id
           JOIN prediction_evaluations e ON e.prediction_id=p.id
           JOIN results r ON r.event_id=p.event_id
           WHERE e.market='win_draw_loss' ORDER BY p.generated_at"""
    ).fetchall()


def fit_goal_scales(rows):
    """Estimate conservative global goal corrections from completed results."""
    expected_home = expected_away = actual_home = actual_away = 0.0
    for row in rows:
        payload = json.loads(row["payload"])
        features_raw = _row_value(row, "features")
        features = json.loads(features_raw) if features_raw else {}
        expected = payload.get("expected_goals") or {}
        expected_home += float(features.get("home_xg_base", expected.get("home", 1.2)))
        expected_away += float(features.get("away_xg_base", expected.get("away", 1.1)))
        actual_home += float(row["home_score"] if row["home_score"] is not None else 0.0)
        actual_away += float(row["away_score"] if row["away_score"] is not None else 0.0)
    # A small prior prevents a short run of unusually high or low scores from
    # dominating the next model version.
    prior_weight = 5.0
    home_scale = (actual_home + prior_weight * 1.35) / max(expected_home + prior_weight * 1.35, 1e-7)
    away_scale = (actual_away + prior_weight * 1.10) / max(expected_away + prior_weight * 1.10, 1e-7)
    return {
        "home": round(max(0.85, min(1.15, home_scale)), 4),
        "away": round(max(0.85, min(1.15, away_scale)), 4),
    }


def optimize_calibration(conn, minimum_samples=20):
    rows = evaluated_rows(conn)
    if len(rows) < minimum_samples:
        return {"status": "insufficient_data", "sample_count": len(rows), "minimum_samples": minimum_samples}
    baseline = metrics(rows, 1.0)
    goal_scales = fit_goal_scales(rows)
    candidates = [round(0.70 + i * 0.05, 2) for i in range(17)]
    best_temperature = min(candidates, key=lambda value: metrics(rows, value, goal_scales)["log_loss"])
    best = metrics(rows, best_temperature, goal_scales)
    # Require a meaningful improvement before activating a new calibration.
    if best["log_loss"] >= baseline["log_loss"] * 0.98:
        return {
            "status": "unchanged",
            "sample_count": len(rows),
            "baseline": baseline,
            "candidate": best,
            "temperature": best_temperature,
            "goal_scales": goal_scales,
        }
    version = f"calibrated-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    conn.execute("UPDATE model_versions SET status='retired' WHERE status='active'")
    parameters = {
        "temperature": best_temperature,
        "goal_scales": goal_scales,
        "trained_on": len(rows),
    }
    conn.execute("INSERT INTO model_versions VALUES(?,?,?,?,?)", (version, "poisson-calibrated", json.dumps(parameters), "active", now()))
    conn.execute("INSERT INTO model_metrics(model_version,market,sample_count,accuracy,log_loss,brier_score,evaluated_at) VALUES(?,?,?,?,?,?,?)", (version, "win_draw_loss", best["sample_count"], best["accuracy"], best["log_loss"], best["brier_score"], now()))
    return {
        "status": "activated",
        "version": version,
        "sample_count": len(rows),
        "baseline": baseline,
        "candidate": best,
        "temperature": best_temperature,
        "goal_scales": goal_scales,
    }


def active_model_parameters(conn):
    row = conn.execute("SELECT parameters FROM model_versions WHERE status='active' LIMIT 1").fetchone()
    if not row:
        return {"temperature": 1.0, "goal_scales": {"home": 1.0, "away": 1.0}}
    try:
        parameters = json.loads(row["parameters"])
    except (TypeError, json.JSONDecodeError):
        parameters = {}
    if not isinstance(parameters, dict):
        parameters = {}
    parameters.setdefault("temperature", 1.0)
    parameters.setdefault("goal_scales", {"home": 1.0, "away": 1.0})
    return parameters


def active_temperature(conn):
    return float(active_model_parameters(conn).get("temperature", 1.0))
