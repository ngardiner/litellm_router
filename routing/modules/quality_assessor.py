"""
Quality Assessor — routes requests to the best model from a configured pool,
weighted by quality scores maintained by a background assessment daemon.

Each routing rule using this module specifies a pool of candidate models in
its instance_config. The daemon periodically assesses those models using a
pluggable judge engine and stores scores in quality_assessor_weights. The
route() function reads those weights and selects a model probabilistically.

Configuration (instance-level, per routing rule):
    models              list of candidate model names (required)
    judge_engine        response_metrics | self_consistency | llm_judge |
                        reward_model | hybrid  (default: response_metrics)
    scoring_metric      quality | quality_cost_ratio | latency_quality_ratio
                        (default: quality_cost_ratio)
    assessment_interval_minutes   how often to reassess (default: 60)
    selection_strategy  weighted_random | top_k | epsilon_greedy
                        (default: weighted_random)
    cold_start_strategy round_robin | cost_weighted  (default: cost_weighted)
    epsilon             exploration rate for epsilon_greedy (default: 0.1)
    top_k               number of top models to sample from for top_k (default: 1)
"""

import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

try:
    from ..database.connection import get_simple_connection
except ImportError:
    import os as _os
    import psycopg2 as _psycopg2

    def get_simple_connection():  # type: ignore[misc]
        import urllib.parse as _urlparse
        url = _os.environ["DATABASE_URL"]
        parsed = _urlparse.urlparse(url)
        clean_url = parsed._replace(query="").geturl()
        return _psycopg2.connect(clean_url)

logger = logging.getLogger(__name__)

MODULE_NAME = "Quality Assessor"
MODULE_DESCRIPTION = (
    "Routes requests to the best model from a configured pool, "
    "weighted by scores from a pluggable background assessment engine."
)
MODULE_VERSION = "0.1.0"
ROUTING_MODULE = True

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Candidate model names to route between (required per-instance)",
            "default": [],
        },
        "judge_engine": {
            "type": "string",
            "enum": ["response_metrics", "self_consistency", "llm_judge",
                     "reward_model", "hybrid"],
            "default": "response_metrics",
            "description": "Assessment engine to use for scoring models",
        },
        "scoring_metric": {
            "type": "string",
            "enum": ["quality", "quality_cost_ratio", "latency_quality_ratio"],
            "default": "quality_cost_ratio",
            "description": "Composite metric to optimise for",
        },
        "assessment_interval_minutes": {
            "type": "integer",
            "default": 60,
            "description": "How often the daemon reassesses models (minutes)",
        },
        "selection_strategy": {
            "type": "string",
            "enum": ["weighted_random", "top_k", "epsilon_greedy"],
            "default": "weighted_random",
            "description": "How to select a model from the scored pool",
        },
        "cold_start_strategy": {
            "type": "string",
            "enum": ["round_robin", "cost_weighted"],
            "default": "cost_weighted",
            "description": "Fallback strategy when no assessment scores exist yet",
        },
        "epsilon": {
            "type": "number",
            "default": 0.1,
            "description": "Exploration rate for epsilon_greedy strategy (0–1)",
        },
        "top_k": {
            "type": "integer",
            "default": 1,
            "description": "Number of top models to sample from for top_k strategy",
        },
    },
    "required": [],
}


# ---------------------------------------------------------------------------
# route() — the hot path; reads pre-computed weights from DB
# ---------------------------------------------------------------------------

def route(model: str, messages: list, config: dict, **kwargs) -> Optional[str]:
    """
    Select a candidate model from the pool based on stored quality weights.

    Falls back to cold_start_strategy if no weights exist yet or all weights
    are too stale (> 2× assessment_interval_minutes old).
    """
    models: List[str] = config.get("models", [])
    if not models:
        logger.warning("QualityAssessor: no candidate models configured for %s", model)
        return None

    weights = _load_weights(model, models)

    if not weights:
        return _cold_start(models, config)

    strategy = config.get("selection_strategy", "weighted_random")
    return _select(models, weights, strategy, config)


# ---------------------------------------------------------------------------
# Selection strategies
# ---------------------------------------------------------------------------

def _select(models: List[str], weights: Dict[str, float],
            strategy: str, config: dict) -> str:
    scores = [max(weights.get(m, 0.01), 0.01) for m in models]

    if strategy == "weighted_random":
        return random.choices(models, weights=scores, k=1)[0]

    elif strategy == "top_k":
        k = max(1, min(config.get("top_k", 1), len(models)))
        ranked = sorted(zip(models, scores), key=lambda x: x[1], reverse=True)
        top = [m for m, _ in ranked[:k]]
        return random.choice(top)

    elif strategy == "epsilon_greedy":
        epsilon = float(config.get("epsilon", 0.1))
        if random.random() < epsilon:
            return random.choice(models)
        best = models[scores.index(max(scores))]
        return best

    # fallback
    return random.choices(models, weights=scores, k=1)[0]


def _cold_start(models: List[str], config: dict) -> str:
    strategy = config.get("cold_start_strategy", "cost_weighted")

    if strategy == "cost_weighted":
        cost_weights = [1.0 / max(_estimate_cost_per_1k(m), 1e-6) for m in models]
        return random.choices(models, weights=cost_weights, k=1)[0]

    return random.choice(models)


def _estimate_cost_per_1k(model_name: str) -> float:
    """
    Estimate cost per 1k output tokens. Uses LiteLLM's built-in cost table if
    available; falls back to a rough heuristic based on model name keywords.
    """
    try:
        import litellm
        cost = litellm.completion_cost(
            completion_response=None,
            model=model_name,
            prompt="x",
            completion="x",
        )
        return float(cost) * 1000
    except Exception:
        pass

    name = model_name.lower()
    if "opus" in name or "gpt-4o" in name:
        return 15.0
    if "sonnet" in name or "gpt-4" in name:
        return 3.0
    if "haiku" in name or "gpt-3.5" in name or "mini" in name:
        return 0.25
    return 1.0


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_weights(rule_model_name: str, models: List[str]) -> Dict[str, float]:
    """Return {model: score} for all candidate models that have a weight row."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT candidate_model, score
                    FROM quality_assessor_weights
                    WHERE rule_model_name = %s
                      AND candidate_model = ANY(%s)
                    """,
                    (rule_model_name, models),
                )
                return {row[0]: float(row[1]) for row in cursor.fetchall()}
    except Exception as exc:
        logger.error("QualityAssessor: weight lookup failed: %s", exc)
        return {}


def _upsert_weight(rule_model_name: str, candidate_model: str,
                   score: float) -> None:
    """Store or update an assessment score."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO quality_assessor_weights
                        (rule_model_name, candidate_model, score,
                         last_assessed_at, assessment_count, updated_at)
                    VALUES (%s, %s, %s, NOW(), 1, NOW())
                    ON CONFLICT (rule_model_name, candidate_model) DO UPDATE SET
                        score = EXCLUDED.score,
                        last_assessed_at = NOW(),
                        assessment_count = quality_assessor_weights.assessment_count + 1,
                        updated_at = NOW()
                    """,
                    (rule_model_name, candidate_model, score),
                )
                conn.commit()
    except Exception as exc:
        logger.error("QualityAssessor: weight upsert failed: %s", exc)


def get_weights_for_rule(rule_model_name: str) -> List[Dict]:
    """Return all weight rows for a routing rule (used by UI)."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT candidate_model, score, last_assessed_at, assessment_count
                    FROM quality_assessor_weights
                    WHERE rule_model_name = %s
                    ORDER BY score DESC
                    """,
                    (rule_model_name,),
                )
                return [
                    {
                        "candidate_model": row[0],
                        "score": row[1],
                        "last_assessed_at": row[2].isoformat() if row[2] else None,
                        "assessment_count": row[3],
                    }
                    for row in cursor.fetchall()
                ]
    except Exception as exc:
        logger.error("QualityAssessor: get_weights_for_rule failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Judge engine abstraction
# ---------------------------------------------------------------------------

class JudgeEngine(ABC):
    """Base class for model quality assessment engines."""

    @abstractmethod
    def assess(self, rule_model_name: str, candidate_model: str,
               config: dict) -> float:
        """
        Return a normalised quality score in [0, 1] for candidate_model
        in the context of rule_model_name's workload.
        """


class ResponseMetricsJudge(JudgeEngine):
    """
    Derives quality scores from existing routing_metrics data.

    Uses: success_rate, mean response time, and (optionally) cost weighting.
    Requires no live inference — works entirely from historical data.
    """

    def assess(self, rule_model_name: str, candidate_model: str,
               config: dict) -> float:
        scoring_metric = config.get("scoring_metric", "quality_cost_ratio")
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            COUNT(*) AS total,
                            SUM(CASE WHEN success THEN 1 ELSE 0 END) AS successes,
                            AVG(response_time_ms) AS avg_latency
                        FROM routing_metrics
                        WHERE target_model = %s
                          AND timestamp > NOW() - INTERVAL '24 hours'
                        """,
                        (candidate_model,),
                    )
                    row = cursor.fetchone()

            total, successes, avg_latency = row
            if not total or total == 0:
                return 0.5  # no data — neutral score

            success_rate = float(successes) / float(total)
            latency_s = float(avg_latency or 1000) / 1000.0
            latency_score = max(0.0, 1.0 - latency_s / 30.0)  # normalise to 30s ceiling

            if scoring_metric == "quality":
                return success_rate

            elif scoring_metric == "latency_quality_ratio":
                return (success_rate + latency_score) / 2.0

            elif scoring_metric == "quality_cost_ratio":
                cost = _estimate_cost_per_1k(candidate_model)
                cost_score = max(0.0, 1.0 - cost / 20.0)  # normalise to $20/1k ceiling
                return (success_rate * 0.6 + latency_score * 0.2 + cost_score * 0.2)

            return success_rate

        except Exception as exc:
            logger.error("ResponseMetricsJudge.assess failed for %s: %s",
                         candidate_model, exc)
            return 0.5


class SelfConsistencyJudge(JudgeEngine):
    """
    Sends probe prompts N times and measures semantic consistency of responses.
    High variance = low confidence for this task type.

    TODO: requires live inference against each candidate model.
          Implement once LiteLLM client is accessible from this context.
    """

    def assess(self, rule_model_name: str, candidate_model: str,
               config: dict) -> float:
        raise NotImplementedError(
            "SelfConsistencyJudge requires live inference — not yet implemented"
        )


class LLMJudge(JudgeEngine):
    """
    Uses a designated judge LLM to score responses on probe prompts.

    TODO: requires live inference. Implement using LiteLLM's completion()
          with a judge prompt template. Cost: ~1 judge call per probe per model.
    """

    def assess(self, rule_model_name: str, candidate_model: str,
               config: dict) -> float:
        raise NotImplementedError(
            "LLMJudge requires live inference — not yet implemented"
        )


class RewardModelJudge(JudgeEngine):
    """
    Uses a reward model (e.g. ArmoRM, Skywork-Reward) to score responses.
    Much cheaper than LLMJudge once the reward model is deployed.

    TODO: requires reward model endpoint. Implement once endpoint is configured.
    """

    def assess(self, rule_model_name: str, candidate_model: str,
               config: dict) -> float:
        raise NotImplementedError(
            "RewardModelJudge requires a reward model endpoint — not yet implemented"
        )


class HybridJudge(JudgeEngine):
    """
    Combines ResponseMetricsJudge (always available) with optional higher-quality
    judges when they're configured. Weights are configurable.

    TODO: wire in SelfConsistencyJudge / LLMJudge once implemented.
    """

    def assess(self, rule_model_name: str, candidate_model: str,
               config: dict) -> float:
        # For now, fall back to ResponseMetricsJudge — the only implemented engine
        return ResponseMetricsJudge().assess(rule_model_name, candidate_model, config)


_JUDGE_ENGINES: Dict[str, JudgeEngine] = {
    "response_metrics": ResponseMetricsJudge(),
    "self_consistency": SelfConsistencyJudge(),
    "llm_judge": LLMJudge(),
    "reward_model": RewardModelJudge(),
    "hybrid": HybridJudge(),
}


def get_judge_engine(name: str) -> JudgeEngine:
    engine = _JUDGE_ENGINES.get(name)
    if engine is None:
        logger.warning("Unknown judge engine %r; falling back to response_metrics", name)
        return _JUDGE_ENGINES["response_metrics"]
    return engine


# ---------------------------------------------------------------------------
# Background assessment daemon
# ---------------------------------------------------------------------------

class QualityAssessorDaemon:
    """
    Periodically assesses all candidate models for all quality_assessor rules
    and writes scores to quality_assessor_weights.

    TODO: integrate with application startup.
          Options:
            a) FastAPI lifespan event (asyncio background task)
            b) Separate process / cron job
            c) APScheduler registered at startup

    Usage (once integrated):
        daemon = QualityAssessorDaemon()
        daemon.start()          # starts background thread
        daemon.stop()           # graceful shutdown
    """

    def __init__(self):
        self._running = False
        self._thread = None

    def start(self) -> None:
        import threading
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="quality-assessor-daemon")
        self._thread.start()
        logger.info("QualityAssessorDaemon started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("QualityAssessorDaemon stopped")

    def _loop(self) -> None:
        """Main daemon loop — run assessment cycles on a schedule."""
        import time
        while self._running:
            try:
                self._run_all_rules()
            except Exception as exc:
                logger.error("QualityAssessorDaemon cycle error: %s", exc)
            time.sleep(60)

    def _run_all_rules(self) -> None:
        """Find all routing rules that use quality_assessor and run assessments."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT model_name,
                               COALESCE(instance_config, '{}')::jsonb
                        FROM routing_rules
                        WHERE module_name = 'quality_assessor'
                          AND enabled = true
                        """
                    )
                    rules = cursor.fetchall()

            for model_name, instance_config in rules:
                self.run_assessment_cycle(model_name, instance_config or {})

        except Exception as exc:
            logger.error("QualityAssessorDaemon._run_all_rules failed: %s", exc)

    def run_assessment_cycle(self, rule_model_name: str, config: dict) -> None:
        """
        Assess all candidate models for a single routing rule and update weights.
        """
        models: List[str] = config.get("models", [])
        if not models:
            return

        judge_name = config.get("judge_engine", "response_metrics")
        judge = get_judge_engine(judge_name)

        for candidate in models:
            try:
                score = judge.assess(rule_model_name, candidate, config)
                _upsert_weight(rule_model_name, candidate, score)
                logger.debug(
                    "QualityAssessor: %s → %s score=%.3f (judge=%s)",
                    rule_model_name, candidate, score, judge_name,
                )
            except NotImplementedError:
                logger.debug(
                    "QualityAssessor: judge %s not implemented, skipping %s",
                    judge_name, candidate,
                )
            except Exception as exc:
                logger.error(
                    "QualityAssessor: assessment failed for %s → %s: %s",
                    rule_model_name, candidate, exc,
                )
