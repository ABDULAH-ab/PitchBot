import base64
import os
import time
from typing import Any, Dict, Optional

import requests


class GitHubAPIError(RuntimeError):
	pass


_RETRY_DELAY_SECONDS = float(os.getenv("API_RETRY_DELAY_SECONDS", "2"))


def _is_retriable_status(status_code: int) -> bool:
	return status_code in {408, 409, 425, 429} or status_code >= 500


def _repo_full_name(repo_full_name: Optional[str] = None) -> str:
	repo = repo_full_name or os.getenv("TARGET_GITHUB_REPO") or os.getenv("GITHUB_REPO")
	if not repo:
		raise GitHubAPIError("Missing TARGET_GITHUB_REPO or GITHUB_REPO in environment")
	return repo


def _headers() -> Dict[str, str]:
	token = os.getenv("GITHUB_TOKEN")
	if not token:
		raise GitHubAPIError("Missing GITHUB_TOKEN in environment")
	return {
		"Authorization": f"token {token}",
		"Accept": "application/vnd.github+json",
		"X-GitHub-Api-Version": "2022-11-28",
	}


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
	attempt = 0
	while True:
		attempt += 1
		try:
			response = requests.request(method, url, headers=_headers(), timeout=60, **kwargs)
			response.raise_for_status()
			return response
		except requests.RequestException as exc:
			response = getattr(exc, "response", None)
			status_code = response.status_code if response is not None else None
			if status_code is not None and not _is_retriable_status(status_code):
				raise GitHubAPIError(f"GitHub request failed for {method} {url}: {exc}") from exc
			status_text = status_code if status_code is not None else "network-error"
			print(f"[github] {method} {url} failed with {status_text} on attempt {attempt}; retrying...")
			time.sleep(_RETRY_DELAY_SECONDS)


def _api_url(path: str, repo_full_name: Optional[str] = None) -> str:
	return f"https://api.github.com/repos/{_repo_full_name(repo_full_name)}/{path.lstrip('/')}"


def get_main_sha(branch: str = "main", repo_full_name: Optional[str] = None) -> str:
	"""Return the SHA for the named branch ref."""
	url = _api_url(f"git/ref/heads/{branch}", repo_full_name=repo_full_name)
	data = _request("GET", url).json()
	try:
		return data["object"]["sha"]
	except (KeyError, TypeError) as exc:
		raise GitHubAPIError(f"Unexpected GitHub ref response: {data}") from exc


def create_branch(branch_name: str, base_sha: str, repo_full_name: Optional[str] = None) -> Dict[str, Any]:
	"""Create a new branch ref from the given base SHA."""
	url = _api_url("git/refs", repo_full_name=repo_full_name)
	payload = {"ref": f"refs/heads/{branch_name}", "sha": base_sha}
	return _request("POST", url, json=payload).json()


def commit_file(
	path: str,
	content: str,
	branch_name: str,
	commit_message: str,
	author_name: str = "EngineerAgent",
	author_email: str = "agent@pitchbot.ai",
	sha: Optional[str] = None,
	repo_full_name: Optional[str] = None,
) -> Dict[str, Any]:
	"""Create or update a file in the repo on the specified branch."""
	url = _api_url(f"contents/{path.lstrip('/')}", repo_full_name=repo_full_name)
	payload: Dict[str, Any] = {
		"message": commit_message,
		"content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
		"branch": branch_name,
		"committer": {"name": author_name, "email": author_email},
		"author": {"name": author_name, "email": author_email},
	}
	if sha:
		payload["sha"] = sha
	return _request("PUT", url, json=payload).json()


def open_pr(
	branch_name: str,
	title: str,
	body: str,
	base: str = "main",
	repo_full_name: Optional[str] = None,
) -> Dict[str, Any]:
	"""Open a pull request from a feature branch into base."""
	url = _api_url("pulls", repo_full_name=repo_full_name)
	payload = {"title": title, "body": body, "head": branch_name, "base": base}
	return _request("POST", url, json=payload).json()


def create_issue(title: str, body: str, repo_full_name: Optional[str] = None) -> Dict[str, Any]:
	"""Create a GitHub issue and return the API response."""
	url = _api_url("issues", repo_full_name=repo_full_name)
	payload = {"title": title, "body": body}
	return _request("POST", url, json=payload).json()


def post_pr_review_comments(
	pr_number: int,
	body: str,
	repo_full_name: Optional[str] = None,
) -> Dict[str, Any]:
	"""Post a QA feedback comment on the PR conversation thread."""
	url = _api_url(f"issues/{pr_number}/comments", repo_full_name=repo_full_name)
	payload = {"body": body}
	return _request("POST", url, json=payload).json()
