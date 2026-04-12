import os
import re
from typing import Any, Dict, List

from message_bus import read_message, send_message
from utils.github_api import GitHubAPIError, post_pr_review_comments
from utils.llm import LLMError, call_llm_json, resolve_llm_for_role


def _qa_llm_config() -> Dict[str, str | None]:
	config = resolve_llm_for_role("qa")
	return {
		"provider": config.get("provider") or os.getenv("LLM_PROVIDER", "openai"),
		"model": config.get("model"),
	}


def _extract_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
	return payload if isinstance(payload, dict) else {}


def _extract_pr_number(pr_url: str) -> int | None:
	match = re.search(r"/pull/(\d+)", pr_url)
	if not match:
		return None
	return int(match.group(1))


def _default_review_report() -> Dict[str, Any]:
	issues = [
		"Ensure landing page headline matches value proposition exactly.",
		"Confirm all top product features are represented on the page.",
	]
	return {
		"verdict": "fail",
		"summary": "Fallback QA review used due to LLM/service issue.",
		"issues": issues,
	}


def review_quality(html: str, marketing_copy: Dict[str, Any], product_spec: Dict[str, Any]) -> Dict[str, Any]:
	system_prompt = (
		"You are a strict QA reviewer. Return only valid JSON with keys: "
		"verdict, summary, issues. verdict must be pass or fail. "
		"issues must be an array of specific actionable strings."
	)
	user_prompt = (
		f"Product spec: {product_spec}\n"
		f"Landing page HTML: {html[:8000]}\n"
		f"Marketing copy: {marketing_copy}\n"
		"Review criteria:\n"
		"1) Headline and messaging align with value proposition\n"
		"2) Key features are represented\n"
		"3) CTA exists and is clear\n"
		"4) Tagline is concise and compelling\n"
		"5) Cold email includes clear call to action"
	)

	try:
		data = call_llm_json(system_prompt=system_prompt, user_prompt=user_prompt, **_qa_llm_config())
		if not isinstance(data, dict):
			return _default_review_report()

		verdict = str(data.get("verdict", "fail")).strip().lower()
		if verdict not in {"pass", "fail"}:
			verdict = "fail"

		summary = str(data.get("summary", "QA review complete")).strip() or "QA review complete"
		issues = data.get("issues", [])
		if not isinstance(issues, list):
			issues = []
		issues = [str(item).strip() for item in issues if str(item).strip()]
		if not issues and verdict == "fail":
			issues = _default_review_report()["issues"]

		return {"verdict": verdict, "summary": summary, "issues": issues}
	except LLMError as exc:
		print(f"[qa] review generation failed, using fallback: {exc}")
		return _default_review_report()


def _post_review_comments(pr_url: str, issues: List[str], repo_full_name: str | None) -> List[Dict[str, Any]]:
	pr_number = _extract_pr_number(pr_url)
	if pr_number is None:
		return [{"status": "skipped", "reason": "No PR number found in pr_url"}]

	comments = issues[:2] if len(issues) >= 2 else issues + ["Please review overall consistency before merge."]
	results: List[Dict[str, Any]] = []
	for comment in comments[:2]:
		try:
			resp = post_pr_review_comments(
				pr_number=pr_number,
				body=f"QA Review: {comment}",
				repo_full_name=repo_full_name,
			)
			results.append({"status": "posted", "url": resp.get("html_url")})
		except GitHubAPIError as exc:
			results.append({"status": "failed", "error": str(exc)})
	return results


def _handle_message(message: Dict[str, Any]) -> None:
	if message.get("message_type") not in {"task", "revision_request", "result"}:
		return

	payload = _extract_payload(message.get("payload", {}))
	html = str(payload.get("html", ""))
	marketing_copy = payload.get("marketing_copy", {})
	if not isinstance(marketing_copy, dict):
		marketing_copy = {}
	product_spec = payload.get("product_spec", {})
	if not isinstance(product_spec, dict):
		product_spec = {}
	pr_url = str(payload.get("pr_url", "")).strip()
	repo_full_name = os.getenv("TARGET_GITHUB_REPO") or os.getenv("GITHUB_REPO")

	report = review_quality(html=html, marketing_copy=marketing_copy, product_spec=product_spec)
	comment_results = _post_review_comments(pr_url=pr_url, issues=report.get("issues", []), repo_full_name=repo_full_name)

	result_payload = {
		"status": "qa_complete",
		"qa_report": report,
		"comment_results": comment_results,
		"pr_url": pr_url,
	}

	send_message(
		from_agent="qa",
		to_agent="ceo",
		message_type="result",
		payload=result_payload,
		parent_message_id=message.get("message_id"),
	)


def run_loop() -> None:
	print("[qa] listening on channel 'qa'")
	while True:
		message = read_message("qa")
		_handle_message(message)
