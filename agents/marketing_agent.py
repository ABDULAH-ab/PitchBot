import os
import re
from typing import Any, Dict

from message_bus import read_message, send_message
from utils.email_api import EmailAPIError, send_cold_email
from utils.llm import LLMError, call_llm_json, resolve_llm_for_role
from utils.slack_api import SlackAPIError, post_launch_message


def _marketing_llm_config() -> Dict[str, str | None]:
	config = resolve_llm_for_role("marketing")
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


def _default_marketing_copy(spec: Dict[str, Any]) -> Dict[str, Any]:
	value_prop = str(spec.get("value_proposition", "Launch smarter with AI assistance.")).strip()
	return {
		"startup_name": "LaunchPilot",
		"tagline": "Plan smarter. Launch faster.",
		"description": f"{value_prop} Built to help users move from idea to launch with less friction.",
		"cold_email_subject": "Quick launch idea for your students",
		"cold_email_body": (
			"Hi,\n\n"
			"We just launched an AI-powered solution that helps students make better course decisions faster. "
			"Would you be open to a quick look and feedback this week?\n\n"
			"Best regards,\nLaunchPilot Team"
		),
		"social_posts": {
			"twitter": "New launch: AI-powered course planning to help students choose smarter and faster.",
			"linkedin": "We launched a new AI assistant for course planning focused on speed, clarity, and student outcomes.",
			"instagram": "Course planning, simplified. Our new AI launch helps students choose with confidence.",
		},
	}


def _sanitize_startup_name(name: str) -> str:
	cleaned = re.sub(r"[^A-Za-z0-9\s-]", "", name).strip()
	if not cleaned:
		return "LaunchPilot"
	words = [w for w in cleaned.split() if w]
	if not words:
		return "LaunchPilot"
	return " ".join(words[:3])


def _normalize_email_body(email_body: str, startup_name: str) -> str:
	text = str(email_body or "").strip()

	# Remove unresolved placeholders from model output.
	text = re.sub(r"\{\{\s*first\s*name\s*\}\}", "", text, flags=re.IGNORECASE)
	text = re.sub(r"\{\{\s*signup\s*link\s*\}\}", "https://example.com", text, flags=re.IGNORECASE)
	text = re.sub(r"\{\{[^{}]+\}\}", "", text)

	# Replace bracket placeholders with concrete startup identity.
	text = re.sub(r"\[\s*startup\s*name\s*\]", startup_name, text, flags=re.IGNORECASE)
	text = re.sub(r"\[\s*your\s*name\s*\]", f"{startup_name} Team", text, flags=re.IGNORECASE)

	if not text:
		text = (
			"Hi,\n\n"
			"We are launching a new product and would love to share a quick demo. "
			"If this sounds useful, reply to this email and we will send access details.\n\n"
			f"Best regards,\n{startup_name} Team"
		)

	lines = [line.rstrip() for line in text.splitlines()]
	while lines and not lines[0].strip():
		lines.pop(0)

	if not lines:
		return f"Hi,\n\nBest regards,\n{startup_name} Team"

	# Enforce non-personalized greeting.
	if lines[0].lower().startswith("hi"):
		lines[0] = "Hi,"
	else:
		lines.insert(0, "Hi,")
		lines.insert(1, "")

	joined = "\n".join(lines).strip()

	# Enforce stable signature with startup name by replacing any existing sign-off tail.
	body_without_signature = re.split(
		r"\n(?:best regards,?|best,?|regards,?|sincerely,?|thanks,?)[\s\S]*$",
		joined,
		maxsplit=1,
		flags=re.IGNORECASE,
	)[0].rstrip()

	return f"{body_without_signature}\n\nBest regards,\n{startup_name} Team"


def _normalize_email_subject(subject: str, startup_name: str) -> str:
	clean_subject = str(subject or "").strip()
	if not clean_subject:
		clean_subject = "Launch update"

	if startup_name.lower() in clean_subject.lower():
		return clean_subject

	return f"[{startup_name}] {clean_subject}"


def generate_marketing_copy(spec: Dict[str, Any], focus: str = "") -> Dict[str, Any]:
	system_prompt = (
		"You are a startup marketing agent. Return only valid JSON with keys: "
		"startup_name, tagline, description, cold_email_subject, cold_email_body, social_posts. "
		"social_posts must be an object with keys twitter, linkedin, instagram."
	)
	user_prompt = (
		f"Product spec: {spec}\n"
		f"Marketing focus: {focus}\n"
		"Rules:\n"
		"- suggest a startup_name (2-3 words max) that fits the idea\n"
		"- tagline under 10 words\n"
		"- description 2-3 sentences\n"
		"- cold email should have clear CTA\n"
		"- email greeting must be generic: 'Hi,' (no first-name placeholders)\n"
		"- signature must end with: 'Best regards,' and '<startup_name> Team'\n"
		"- no markdown and no extra text"
	)

	try:
		data = call_llm_json(system_prompt=system_prompt, user_prompt=user_prompt, **_marketing_llm_config())
		if not isinstance(data, dict):
			return _default_marketing_copy(spec)

		fallback = _default_marketing_copy(spec)
		startup_name = _sanitize_startup_name(str(data.get("startup_name", fallback["startup_name"])))
		tagline = str(data.get("tagline", fallback["tagline"]))
		tagline_words = [w for w in tagline.strip().split() if w]
		if len(tagline_words) > 10 or not tagline_words:
			tagline = fallback["tagline"]

		description = str(data.get("description", fallback["description"]))
		subject = _normalize_email_subject(
			subject=str(data.get("cold_email_subject", fallback["cold_email_subject"])),
			startup_name=startup_name,
		)
		email_body = _normalize_email_body(
			email_body=str(data.get("cold_email_body", fallback["cold_email_body"])),
			startup_name=startup_name,
		)

		social = data.get("social_posts")
		if not isinstance(social, dict):
			social = fallback["social_posts"]
		else:
			social = {
				"twitter": str(social.get("twitter", fallback["social_posts"]["twitter"])),
				"linkedin": str(social.get("linkedin", fallback["social_posts"]["linkedin"])),
				"instagram": str(social.get("instagram", fallback["social_posts"]["instagram"])),
			}

		return {
			"startup_name": startup_name,
			"tagline": tagline,
			"description": description,
			"cold_email_subject": subject,
			"cold_email_body": email_body,
			"social_posts": social,
		}
	except LLMError as exc:
		print(f"[marketing] copy generation failed, using fallback: {exc}")
		fallback = _default_marketing_copy(spec)
		fallback["cold_email_body"] = _normalize_email_body(
			email_body=fallback["cold_email_body"],
			startup_name=_sanitize_startup_name(fallback.get("startup_name", "LaunchPilot")),
		)
		fallback["cold_email_subject"] = _normalize_email_subject(
			subject=fallback.get("cold_email_subject", "Launch update"),
			startup_name=_sanitize_startup_name(fallback.get("startup_name", "LaunchPilot")),
		)
		return fallback


def _handle_message(message: Dict[str, Any]) -> None:
	message_type = message.get("message_type")
	if message_type not in {"task", "result", "revision_request"}:
		return

	payload = message.get("payload", {})
	if not isinstance(payload, dict):
		payload = {}

	product_spec = _extract_product_spec(payload)
	focus = str(payload.get("focus", "")).strip()
	pr_url = str(payload.get("pr_url", "")).strip() or "https://github.com"
	recipient = str(os.getenv("SENDGRID_TO_EMAIL", "")).strip()

	copy = generate_marketing_copy(product_spec, focus=focus)

	email_status: Any = None
	email_error: str | None = None
	try:
		if recipient:
			email_status = send_cold_email(
				to_email=recipient,
				subject=copy["cold_email_subject"],
				body=copy["cold_email_body"],
			)
		else:
			email_error = "SENDGRID_TO_EMAIL is not set"
	except EmailAPIError as exc:
		email_error = str(exc)

	slack_status: Any = None
	slack_error: str | None = None
	try:
		slack_result = post_launch_message(
			tagline=copy["tagline"],
			description=copy["description"],
			pr_url=pr_url,
		)
		slack_status = {"ok": slack_result.get("ok"), "ts": slack_result.get("ts")}
	except SlackAPIError as exc:
		slack_error = str(exc)

	status = "success" if (email_error is None and slack_error is None) else "partial_failure"

	result_payload = {
		"status": status,
		"marketing_copy": copy,
		"email_status": email_status,
		"email_error": email_error,
		"slack_status": slack_status,
		"slack_error": slack_error,
		"pr_url": pr_url,
	}

	send_message(
		from_agent="marketing",
		to_agent="ceo",
		message_type="result",
		payload=result_payload,
		parent_message_id=message.get("message_id"),
	)


def run_loop() -> None:
	print("[marketing] listening on channel 'marketing'")
	while True:
		message = read_message("marketing")
		_handle_message(message)
