from __future__ import annotations

from cloud_agent_service.models import (
    DeploymentPolicy,
    JobStatus,
    PromotionStatus,
    TaskCase,
    TaskSuite,
)


def default_replayable_corpus() -> TaskSuite:
    return TaskSuite(
        suite_id="repo_edit_replay_corpus_v1",
        cases=[
            TaskCase(
                task_id="shopping_buy_button_local",
                prompt="For my shopping website, create a buy button.",
                deploy_policy=DeploymentPolicy.LOCAL,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.PROMOTE,
                expected_changed_files=["index.html"],
            ),
            TaskCase(
                task_id="shopping_buy_button_manual",
                prompt="For my shopping website, create a buy button.",
                deploy_policy=DeploymentPolicy.MANUAL,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.NEEDS_REVIEW,
                expected_changed_files=["index.html"],
            ),
            TaskCase(
                task_id="shopping_buy_button_preview",
                prompt="For my shopping website, create a buy button.",
                deploy_policy=DeploymentPolicy.PREVIEW_ONLY,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.NEEDS_REVIEW,
                expected_changed_files=["index.html"],
            ),
            TaskCase(
                task_id="shopping_budget_guard",
                prompt="For my shopping website, create a buy button.",
                deploy_policy=DeploymentPolicy.LOCAL,
                expected_job_status=JobStatus.FAILED,
                expected_promotion_status=PromotionStatus.REJECT,
                token_budget=10,
            ),
            TaskCase(
                task_id="docs_checkout_flow",
                prompt="Document the checkout flow for reviewers.",
                deploy_policy=DeploymentPolicy.LOCAL,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.PROMOTE,
                expected_changed_files=["agent_output/implementation_plan.md"],
            ),
            TaskCase(
                task_id="api_endpoint_plan",
                prompt="Develop a small API endpoint for order status.",
                deploy_policy=DeploymentPolicy.MANUAL,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.NEEDS_REVIEW,
                expected_changed_files=["agent_output/implementation_plan.md"],
            ),
            TaskCase(
                task_id="dependency_upgrade_plan",
                prompt="Plan a dependency upgrade for the frontend package.",
                deploy_policy=DeploymentPolicy.PR_ONLY,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.NEEDS_REVIEW,
                expected_changed_files=["agent_output/implementation_plan.md"],
            ),
            TaskCase(
                task_id="test_repair_plan",
                prompt="Repair the failing checkout test.",
                deploy_policy=DeploymentPolicy.LOCAL,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.PROMOTE,
                expected_changed_files=["agent_output/implementation_plan.md"],
            ),
            TaskCase(
                task_id="refactor_cart_plan",
                prompt="Refactor cart calculation code with tests.",
                deploy_policy=DeploymentPolicy.MANUAL,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.NEEDS_REVIEW,
                expected_changed_files=["agent_output/implementation_plan.md"],
            ),
            TaskCase(
                task_id="staging_release_note",
                prompt="Prepare a staging release note for the shopping site.",
                deploy_policy=DeploymentPolicy.STAGING_AUTO,
                expected_job_status=JobStatus.SUCCEEDED,
                expected_promotion_status=PromotionStatus.PROMOTE,
                expected_changed_files=["agent_output/implementation_plan.md"],
            ),
        ],
    )
