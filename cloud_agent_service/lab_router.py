from __future__ import annotations

from dataclasses import asdict
from typing import Any

from cloud_agent_service.analysis_lab import default_analysis_cases
from cloud_agent_service.models import JobRequest, RoutingDecision, RoutingPolicy


class LabRouter:
    def recommend(
        self,
        request: JobRequest,
        leaderboard: list[dict[str, Any]],
    ) -> RoutingDecision:
        nearest_cases = self._nearest_analysis_cases(request.prompt)
        if request.routing_policy == RoutingPolicy.FIXED:
            return RoutingDecision(
                routing_policy=request.routing_policy,
                selected_model_id=request.model_id,
                selected_agent_id=request.agent_id,
                selected_harness_id=request.harness_id,
                confidence=1.0,
                reason="Fixed routing policy preserves the caller-selected lab tuple.",
                nearest_analysis_cases=nearest_cases,
                fallback=False,
            )

        best = self._best_leaderboard_row(leaderboard)
        if not best:
            return RoutingDecision(
                routing_policy=request.routing_policy,
                selected_model_id=request.model_id,
                selected_agent_id=request.agent_id,
                selected_harness_id=request.harness_id,
                confidence=0.35,
                reason="No leaderboard history is available; using caller-selected fallback.",
                nearest_analysis_cases=nearest_cases,
                fallback=True,
            )

        confidence = min(
            0.95,
            max(0.45, float(best["promotion_rate"]) + min(best["total_runs"], 20) / 100),
        )
        return RoutingDecision(
            routing_policy=request.routing_policy,
            selected_model_id=best["model_id"],
            selected_agent_id=best["agent_id"],
            selected_harness_id=best["harness_id"],
            confidence=confidence,
            reason=(
                "Selected the highest-evidence model/agent/harness tuple from "
                "the lab leaderboard."
            ),
            nearest_analysis_cases=nearest_cases,
            fallback=False,
        )

    @staticmethod
    def asdict(decision: RoutingDecision) -> dict[str, Any]:
        return asdict(decision)

    @staticmethod
    def _best_leaderboard_row(leaderboard: list[dict[str, Any]]) -> dict[str, Any] | None:
        eligible = [
            row
            for row in leaderboard
            if row.get("total_runs", 0) > 0 and row.get("promote_count", 0) > 0
        ]
        if not eligible:
            return None
        return sorted(
            eligible,
            key=lambda row: (
                -float(row.get("promotion_rate", 0.0)),
                -int(row.get("promote_count", 0)),
                float(row.get("avg_tokens_used", 0.0)),
                row["model_id"],
                row["agent_id"],
                row["harness_id"],
            ),
        )[0]

    @staticmethod
    def _nearest_analysis_cases(prompt: str) -> list[str]:
        lower = prompt.lower()
        matches: list[str] = []
        if "secret" in lower or "protected" in lower or "token" in lower:
            matches.append("adversarial_safety_boundary")
        if "test" in lower or "failing" in lower or "repair" in lower:
            matches.append("failure_forensics_repair_loop")
        if "prompt" in lower or "context" in lower or "document" in lower:
            matches.append("prompt_ablation_context_quality")
        if "buy" in lower or "button" in lower or "website" in lower:
            matches.append("model_bakeoff_repo_edit")
        known = {case.case_id for case in default_analysis_cases()}
        matches = [case_id for case_id in matches if case_id in known]
        return matches or ["model_bakeoff_repo_edit"]
