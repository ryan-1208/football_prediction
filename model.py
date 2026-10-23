"""Pure probability calculations used by the local prediction service.

The module deliberately keeps the MVP explainable: team inputs produce expected
goals, independent Poisson distributions produce score probabilities, and all
other markets are aggregations of those probabilities.
"""

import math


GOAL_CAP = 12
TOTAL_GOAL_BUCKETS = tuple(str(value) for value in range(7)) + ("7+",)
OUTCOME_LABELS = ("主胜", "平局", "客胜")


def poisson_probability(goals, expected):
    return math.exp(-expected) * (expected**goals) / math.factorial(goals)


def _normalise(values):
    total = sum(values.values())
    if total <= 0:
        raise ValueError("概率分布总和必须大于 0")
    return {key: value / total for key, value in values.items()}


def _score_matrix(home_expected, away_expected, cap=GOAL_CAP):
    matrix = {
        (home_goals, away_goals): poisson_probability(home_goals, home_expected)
        * poisson_probability(away_goals, away_expected)
        for home_goals in range(cap + 1)
        for away_goals in range(cap + 1)
    }
    return _normalise(matrix)


def _result_label(home_goals, away_goals):
    if home_goals > away_goals:
        return "主胜"
    if home_goals == away_goals:
        return "平局"
    return "客胜"


def _half_result_label(home_goals, away_goals):
    if home_goals > away_goals:
        return "胜"
    if home_goals == away_goals:
        return "平"
    return "负"


def _round_distribution(values, digits=4):
    rounded = {key: round(value, digits) for key, value in values.items()}
    # Keep the public distribution summing to exactly one after rounding.
    residual = round(1.0 - sum(rounded.values()), digits)
    last_key = next(reversed(rounded))
    rounded[last_key] = round(rounded[last_key] + residual, digits)
    return rounded


def _calibrate(values, temperature):
    temperature = max(0.1, float(temperature))
    powered = {key: value ** (1.0 / temperature) for key, value in values.items()}
    return _normalise(powered)


def _total_goals(matrix):
    buckets = {key: 0.0 for key in TOTAL_GOAL_BUCKETS}
    for (home_goals, away_goals), probability in matrix.items():
        total = home_goals + away_goals
        buckets[str(total) if total <= 6 else "7+"] += probability
    return _round_distribution(buckets)


def _handicap_probabilities(matrix, line):
    buckets = {"让胜": 0.0, "让平": 0.0, "让负": 0.0}
    for (home_goals, away_goals), probability in matrix.items():
        result = home_goals + line - away_goals
        label = "让胜" if result > 0 else "让平" if result == 0 else "让负"
        buckets[label] += probability
    return _round_distribution(buckets)


def _half_full_probabilities(home_expected, away_expected):
    # A separate first-half/second-half split keeps half-time and full-time
    # outcomes internally consistent instead of multiplying half-time odds by
    # an unrelated full-time matrix.
    first_home = home_expected * 0.44
    first_away = away_expected * 0.44
    second_home = home_expected - first_home
    second_away = away_expected - first_away
    first_matrix = _score_matrix(first_home, first_away, cap=8)
    second_matrix = _score_matrix(second_home, second_away, cap=8)
    values = {}
    for (first_home_goals, first_away_goals), first_probability in first_matrix.items():
        first_label = _half_result_label(first_home_goals, first_away_goals)
        for (second_home_goals, second_away_goals), second_probability in second_matrix.items():
            full_label = _half_result_label(
                first_home_goals + second_home_goals,
                first_away_goals + second_away_goals,
            )
            key = f"{first_label}/{full_label}"
            values[key] = values.get(key, 0.0) + first_probability * second_probability
    return _round_distribution(values)


def _score_candidates(matrix, limit=5):
    ranked = sorted(matrix.items(), key=lambda item: item[1], reverse=True)
    candidates = [
        {"score": f"{home}:{away}", "probability": round(probability, 4)}
        for (home, away), probability in ranked[:limit]
    ]
    return candidates


def build_prediction(
    home_stats,
    away_stats,
    temperature=1.0,
    model_version="poisson-mvp-0.2",
    lottery_handicap=None,
    context=None,
    goal_scales=None,
):
    """Build one coherent prediction payload from team strength inputs."""

    home_complete = home_stats is not None
    away_complete = away_stats is not None
    context = context or {}
    availability = context.get("availability") or {}
    try:
        home_availability = min(1.0, max(0.9, float(availability.get("home_factor", 1.0))))
    except (TypeError, ValueError):
        home_availability = 1.0
    try:
        away_availability = min(1.0, max(0.9, float(availability.get("away_factor", 1.0))))
    except (TypeError, ValueError):
        away_availability = 1.0
    lineup_available = bool((context.get("lineups") or {}).get("available"))
    injury_available = bool((context.get("injuries") or {}).get("available"))
    context_complete = lineup_available and injury_available
    stats_fallback = bool(context.get("stats_fallback"))
    stats_seasons = context.get("stats_seasons") or {}
    quality_parts = (home_stats is not None, away_stats is not None, lineup_available, injury_available)
    data_quality_score = round(sum(quality_parts) / len(quality_parts), 2)
    home_stats = home_stats or {"home_attack": 1.15, "home_defense": 1.05, "form": 1.0}
    away_stats = away_stats or {"away_attack": 1.05, "away_defense": 1.10, "form": 1.0}
    home_expected = max(
        0.15,
        float(home_stats["home_attack"])
        * float(away_stats["away_defense"])
        * float(home_stats["form"])
        * 1.08,
    )
    away_expected = max(
        0.15,
        float(away_stats["away_attack"])
        * float(home_stats["home_defense"])
        * float(away_stats["form"])
        * 0.94,
    )
    home_expected = max(0.15, home_expected * home_availability)
    away_expected = max(0.15, away_expected * away_availability)
    base_home_expected = home_expected
    base_away_expected = away_expected
    goal_scales = goal_scales or {}
    home_goal_scale = max(0.75, min(1.25, float(goal_scales.get("home", 1.0))))
    away_goal_scale = max(0.75, min(1.25, float(goal_scales.get("away", 1.0))))
    home_expected = max(0.15, home_expected * home_goal_scale)
    away_expected = max(0.15, away_expected * away_goal_scale)

    matrix = _score_matrix(home_expected, away_expected)
    raw_outcomes = {label: 0.0 for label in OUTCOME_LABELS}
    for (home_goals, away_goals), probability in matrix.items():
        raw_outcomes[_result_label(home_goals, away_goals)] += probability
    outcomes = _calibrate(raw_outcomes, temperature)
    rounded_outcomes = _round_distribution(outcomes)

    ranked_outcomes = sorted(rounded_outcomes.items(), key=lambda item: item[1], reverse=True)
    top_value = ranked_outcomes[0][1]
    second_value = ranked_outcomes[1][1]
    gap = max(0.0, top_value - second_value)
    data_factor = 1.0 if home_complete and away_complete and context_complete else 0.72
    confidence = min(0.95, (top_value * 0.7 + gap * 0.3) * data_factor)
    if confidence >= 0.56 and gap >= 0.08:
        confidence_level = "方向相对清晰"
    elif confidence >= 0.46:
        confidence_level = "方向有倾向"
    else:
        confidence_level = "分歧偏大"

    score_candidates = _score_candidates(matrix)
    top_score = score_candidates[0]["score"].split(":")
    result = {
        "model_version": model_version,
        "expected_goals": {"home": round(home_expected, 2), "away": round(away_expected, 2)},
        "win_draw_loss": rounded_outcomes,
        "raw_win_draw_loss": _round_distribution(raw_outcomes),
        "recommendation": ranked_outcomes[0][0],
        "confidence": round(confidence, 4),
        "confidence_level": confidence_level,
        "probability_gap": round(gap, 4),
        "handicap": {
            "line": lottery_handicap,
            "probabilities": _handicap_probabilities(matrix, lottery_handicap)
            if lottery_handicap is not None
            else None,
            "reference": "已按体彩整数让球从比分分布聚合"
            if lottery_handicap is not None
            else "未提供体彩让球，未计算让球概率",
        },
        "total_goals": _total_goals(matrix),
        "half_full": _half_full_probabilities(home_expected, away_expected),
        "score": {"home": int(top_score[0]), "away": int(top_score[1])},
        "score_distribution": score_candidates,
        "score_other_probability": round(
            max(0.0, 1.0 - sum(item["probability"] for item in score_candidates)), 4
        ),
        "data_quality": {
            "complete": all(quality_parts),
            "score": data_quality_score,
            "missing": (["主队历史攻防参数"] if not home_complete else [])
            + (["客队历史攻防参数"] if not away_complete else [])
            + (["实时首发"] if not lineup_available else [])
            + (["实时伤停"] if not injury_available else []),
        },
        "features": {
            "home_xg": round(home_expected, 4),
            "away_xg": round(away_expected, 4),
            "home_xg_base": round(base_home_expected, 4),
            "away_xg_base": round(base_away_expected, 4),
            "home_goal_scale": home_goal_scale,
            "away_goal_scale": away_goal_scale,
            "temperature": float(temperature),
            "goal_cap": GOAL_CAP,
            "home_availability": home_availability,
            "away_availability": away_availability,
            "home_injury_count": availability.get("home_injury_count", 0),
            "away_injury_count": availability.get("away_injury_count", 0),
            "lineup_available": lineup_available,
            "injury_available": injury_available,
            "stats_fallback": stats_fallback,
            "stats_seasons": stats_seasons,
        },
    }
    result["factors"] = [
        f"模型估计进球：主队 {home_expected:.2f}，客队 {away_expected:.2f}",
        (f"已使用 {stats_seasons.get('home')} / {stats_seasons.get('away')} 赛季历史攻防与近期状态特征"
         if home_complete and away_complete and stats_fallback
         else "已使用主客场攻防与近期状态特征"
         if home_complete and away_complete
         else "至少一方缺少历史参数，使用默认基线"),
        "胜平负、总进球、比分和半全场均由同一比分分布推导",
        "已使用 API-Football 赛前首发与伤停快照" if context_complete else "；".join(
            part for part, available in (
                ("实时首发已返回", lineup_available),
                ("实时伤停已返回", injury_available),
            ) if available
        ) or "实时首发和伤停尚未完整返回",
    ]
    return result
