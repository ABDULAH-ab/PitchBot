import os
import re
from datetime import datetime, timezone
from typing import Any, Dict

from message_bus import read_message, send_message
from utils.github_api import (
	GitHubAPIError,
	commit_file,
	create_branch,
	create_issue,
	get_main_sha,
	open_pr,
)
from utils.llm import LLMError, call_llm, call_llm_json, resolve_llm_for_role


def _engineer_llm_config() -> Dict[str, str | None]:
	config = resolve_llm_for_role("engineer")
	return {
		"provider": config.get("provider") or os.getenv("LLM_PROVIDER", "openai"),
		"model": config.get("model"),
	}


def _extract_product_spec(payload: Dict[str, Any]) -> Dict[str, Any]:
	if isinstance(payload.get("product_spec"), dict):
		return payload["product_spec"]
	if isinstance(payload.get("spec"), dict):
		return payload["spec"]
	return payload


def _default_html(spec: Dict[str, Any]) -> str:
	value_prop = spec.get("value_proposition", "Build faster with an AI-powered startup workflow.")
	features = spec.get("features", [])
	feature_items = "\n".join(
		f"<li><strong>{f.get('name', 'Feature')}</strong>: {f.get('description', '')}</li>" for f in features[:5]
	)

	return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Startup Landing Page</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; margin: 0; background: #f7f7f5; color: #151515; }}
    .wrap {{ max-width: 860px; margin: 0 auto; padding: 48px 20px; }}
    h1 {{ font-size: 2.2rem; margin-bottom: 0.5rem; }}
    p {{ line-height: 1.6; }}
    .card {{ background: white; border-radius: 14px; padding: 24px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); }}
    .cta {{ display: inline-block; margin-top: 20px; background: #111; color: white; padding: 12px 18px; border-radius: 10px; text-decoration: none; }}
  </style>
</head>
<body>
  <main class=\"wrap\">
    <section class=\"card\">
      <h1>Ship your idea faster</h1>
      <p>{value_prop}</p>
      <h2>Top Features</h2>
      <ul>
        {feature_items}
      </ul>
      <a class=\"cta\" href=\"#\">Get Started</a>
    </section>
  </main>
</body>
</html>
"""


def generate_landing_page_html(spec: Dict[str, Any], focus: str = "") -> str:
	system_prompt = "You are an expert frontend engineer. Return only raw HTML, no markdown."
	user_prompt = (
		f"Product spec: {spec}\n"
		f"Engineering focus: {focus}\n"
		"Generate a complete single-file landing page with semantic HTML and embedded CSS. "
		"Include headline, subheadline, features section, and one CTA button."
	)

	try:
		raw = call_llm(system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=1800, **_engineer_llm_config())
		text = raw.strip()
		if text.startswith("```"):
			text = text.strip("`")
			if text.lower().startswith("html"):
				text = text[4:]
			text = text.strip()
		return text
	except LLMError as exc:
		print(f"[engineer] HTML generation failed, using fallback: {exc}")
		return _default_html(spec)


def generate_pr_package(spec: Dict[str, Any]) -> Dict[str, str]:
	system_prompt = (
		"You are an engineer preparing GitHub metadata. Return only JSON with keys: "
		"pr_title, pr_body, issue_title, issue_body."
	)
	user_prompt = (
		f"Product spec: {spec}\n"
		"Create concise but professional GitHub PR and issue text for the initial landing page implementation."
	)

	try:
		data = call_llm_json(system_prompt=system_prompt, user_prompt=user_prompt, **_engineer_llm_config())
		if not isinstance(data, dict):
			raise LLMError("PR metadata response is not a JSON object")
		return {
			"pr_title": str(data.get("pr_title", "feat: add initial landing page")).strip() or "feat: add initial landing page",
			"pr_body": str(data.get("pr_body", "Initial implementation of landing page from product spec.")).strip() or "Initial implementation of landing page from product spec.",
			"issue_title": str(data.get("issue_title", "Initial landing page implementation")).strip() or "Initial landing page implementation",
			"issue_body": str(data.get("issue_body", "Build and ship first version of landing page.")).strip() or "Build and ship first version of landing page.",
		}
	except LLMError as exc:
		print(f"[engineer] PR metadata generation failed, using fallback: {exc}")
		return {
			"pr_title": "feat: add initial landing page",
			"pr_body": "Initial implementation of the landing page based on product specification.",
			"issue_title": "Initial landing page implementation",
			"issue_body": "Build and ship the first version of the startup landing page.",
		}


def _safe_branch_name(base: str) -> str:
	clean = re.sub(r"[^a-z0-9-]", "-", base.lower())
	clean = re.sub(r"-+", "-", clean).strip("-")
	if not clean:
		clean = "startup-landing-page"
	timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
	return f"engineer/{clean[:40]}-{timestamp}"


def _handle_message(message: Dict[str, Any]) -> None:
	message_type = message.get("message_type")
	if message_type not in {"task", "result", "revision_request"}:
		return

	payload = message.get("payload", {})
	if not isinstance(payload, dict):
		payload = {}

	product_spec = _extract_product_spec(payload)
	focus = str(payload.get("focus", "")).strip()
	repo_full_name = os.getenv("TARGET_GITHUB_REPO") or os.getenv("GITHUB_REPO")

	html = generate_landing_page_html(product_spec, focus=focus)
	meta = generate_pr_package(product_spec)

	try:
		issue = create_issue(
			title=meta["issue_title"],
			body=meta["issue_body"],
			repo_full_name=repo_full_name,
		)

		base_sha = get_main_sha(repo_full_name=repo_full_name)
		branch_name = _safe_branch_name(product_spec.get("value_proposition", "landing-page"))
		create_branch(branch_name=branch_name, base_sha=base_sha, repo_full_name=repo_full_name)

		commit_file(
			path="index.html",
			content=html,
			branch_name=branch_name,
			commit_message="feat: add initial landing page",
			author_name="EngineerAgent",
			author_email="agent@pitchbot.ai",
			repo_full_name=repo_full_name,
		)

		pr = open_pr(
			branch_name=branch_name,
			title=meta["pr_title"],
			body=meta["pr_body"],
			repo_full_name=repo_full_name,
		)

		result_payload = {
			"status": "success",
			"repo": repo_full_name,
			"branch": branch_name,
			"pr_url": pr.get("html_url"),
			"issue_url": issue.get("html_url"),
			"html": html,
		}
	except GitHubAPIError as exc:
		print(f"[engineer] GitHub flow failed: {exc}")
		result_payload = {
			"status": "failed",
			"repo": repo_full_name,
			"error": str(exc),
			"html": html,
		}

	send_message(
		from_agent="engineer",
		to_agent="ceo",
		message_type="result",
		payload=result_payload,
		parent_message_id=message.get("message_id"),
	)


def run_loop() -> None:
	print("[engineer] listening on channel 'engineer'")
	while True:
		message = read_message("engineer")
		_handle_message(message)
