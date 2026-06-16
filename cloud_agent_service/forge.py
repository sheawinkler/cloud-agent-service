from __future__ import annotations

import os
import urllib.parse
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ForgeProviderStatus:
    provider: str
    configured: bool
    mode: str
    missing: list[str]
    capabilities: list[str]
    notes: list[str]


class ForgeRegistry:
    def statuses(self) -> dict[str, dict[str, object]]:
        return {
            "generic_git": asdict(
                ForgeProviderStatus(
                    provider="generic_git",
                    configured=True,
                    mode="branch-ref",
                    missing=[],
                    capabilities=["clone", "push_review_branch"],
                    notes=[
                        "Provider-agnostic Git pushes a review branch and does not create "
                        "provider-native review objects."
                    ],
                )
            ),
            "github": asdict(
                self._status(
                    provider="github",
                    required=[
                        "GITHUB_APP_ID",
                        "GITHUB_APP_INSTALLATION_ID",
                        "GITHUB_APP_PRIVATE_KEY",
                    ],
                    capabilities=["clone", "push_review_branch", "pull_request"],
                )
            ),
            "gitlab": asdict(
                self._status(
                    provider="gitlab",
                    required=["GITLAB_TOKEN"],
                    capabilities=["clone", "push_review_branch", "merge_request_contract"],
                )
            ),
            "bitbucket": asdict(
                self._status(
                    provider="bitbucket",
                    required=["BITBUCKET_TOKEN"],
                    capabilities=["clone", "push_review_branch", "pull_request_contract"],
                )
            ),
            "gitea": asdict(
                self._status(
                    provider="gitea",
                    required=["GITEA_TOKEN", "GITEA_API_URL"],
                    capabilities=["clone", "push_review_branch", "pull_request_contract"],
                )
            ),
        }

    def review_target(
        self,
        *,
        repo_provider: str,
        git_url: str | None = None,
        github_repo: str | None = None,
    ) -> dict[str, object]:
        if repo_provider == "github":
            return {
                "provider": "github",
                "mode": "provider-native-pr",
                "target": github_repo,
                "configured": self.statuses()["github"]["configured"],
            }
        if repo_provider == "git":
            inferred = self.infer_provider(git_url)
            status = self.statuses().get(inferred, self.statuses()["generic_git"])
            return {
                "provider": inferred,
                "mode": status["mode"] if status["configured"] else "branch-ref",
                "target": self.safe_git_target(git_url),
                "configured": status["configured"],
            }
        return {
            "provider": "local_mock",
            "mode": "mock-pr-artifact",
            "target": None,
            "configured": True,
        }

    @staticmethod
    def infer_provider(git_url: str | None) -> str:
        if not git_url:
            return "generic_git"
        parsed = urllib.parse.urlparse(git_url)
        host = (parsed.hostname or "").lower()
        if not host and git_url.startswith("git@") and ":" in git_url:
            host = git_url.split("@", 1)[1].split(":", 1)[0].lower()
        if "github.com" in host:
            return "github"
        if "gitlab" in host:
            return "gitlab"
        if "bitbucket" in host:
            return "bitbucket"
        if "gitea" in host:
            return "gitea"
        return "generic_git"

    @staticmethod
    def safe_git_target(git_url: str | None) -> str | None:
        if not git_url:
            return None
        parsed = urllib.parse.urlparse(git_url)
        if parsed.scheme and parsed.netloc:
            return urllib.parse.urlunparse(
                (parsed.scheme, parsed.hostname or parsed.netloc, parsed.path, "", "", "")
            )
        if git_url.startswith("git@") and ":" in git_url:
            host, path = git_url.split(":", 1)
            return f"{host}:{path}"
        if parsed.scheme == "file":
            return "file://local-git-remote"
        return "git-remote"

    @staticmethod
    def _status(
        *,
        provider: str,
        required: list[str],
        capabilities: list[str],
    ) -> ForgeProviderStatus:
        missing = [name for name in required if not os.environ.get(name)]
        return ForgeProviderStatus(
            provider=provider,
            configured=not missing,
            mode="provider-native-review" if not missing else "contract-only",
            missing=missing,
            capabilities=capabilities,
            notes=[
                "Native review-object creation is available only when provider "
                "credentials are configured and the adapter is implemented for that provider."
            ],
        )
