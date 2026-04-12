import os
from typing import Any, Dict

from message_bus import read_message, send_message
from utils.llm import LLMError, call_llm_json, resolve_llm_for_role


def _ceo_llm_config() -> Dict[str, str | None]:
	config = resolve_llm_for_role("ceo")
	return {
		"provider": config.get("provider") or os.getenv("LLM_PROVIDER", "openai"),
		"model": config.get("model") or os.getenv("CEO_LLM_MODEL") or os.getenv("GEMINI_MODEL"),
	}


def _default_decomposition(idea: str) -> Dict[str, Any]:
	return {
		"product_task": {
			"idea": idea,
			"focus": "Define value proposition, personas, top 5 features, and 3 user stories.",
		},
		"engineer_task": {
			"idea": idea,
			"focus": "Build an MVP landing page from product specs and open a PR.",
		},
		"marketing_task": {
			"idea": idea,
			"focus": "Create launch copy, outreach email, and Slack launch post.",
		},
	}


def decompose_startup_idea(idea: str) -> Dict[str, Any]:
	"""LLM call #1: break one startup idea into role-specific tasks."""
	system_prompt = (
		"You are a CEO orchestrator. Return only valid JSON with keys: "
		"product_task, engineer_task, marketing_task. "
		"Each key must map to an object with keys idea and focus."
	)
	user_prompt = (
		f"Startup idea: {idea}\n"
		"Create concise, actionable tasks for product, engineer, and marketing agents."
	)

	try:
		data = call_llm_json(system_prompt=system_prompt, user_prompt=user_prompt, **_ceo_llm_config())
		if not isinstance(data, dict):
			return _default_decomposition(idea)

		fallback = _default_decomposition(idea)
		for key in ("product_task", "engineer_task", "marketing_task"):
			if not isinstance(data.get(key), dict):
				data[key] = fallback[key]
			if not isinstance(data[key].get("idea"), str) or not data[key]["idea"].strip():
				data[key]["idea"] = idea
			if not isinstance(data[key].get("focus"), str) or not data[key]["focus"].strip():
				data[key]["focus"] = fallback[key]["focus"]

		return data
	except LLMError as exc:
		print(f"[ceo] decomposition failed, using fallback tasks: {exc}")
		return _default_decomposition(idea)


def dispatch_tasks(idea: str) -> Dict[str, Dict[str, Any]]:
	"""Send CEO-generated tasks to product, engineer, and marketing channels."""
	decomposition = decompose_startup_idea(idea)

	product_msg = send_message(
		from_agent="ceo",
		to_agent="product",
		message_type="task",
		payload=decomposition["product_task"],
	)
	engineer_msg = send_message(
		from_agent="ceo",
		to_agent="engineer",
		message_type="task",
		payload=decomposition["engineer_task"],
	)
	marketing_msg = send_message(
		from_agent="ceo",
		to_agent="marketing",
		message_type="task",
		payload=decomposition["marketing_task"],
	)

	return {
		"product": product_msg,
		"engineer": engineer_msg,
		"marketing": marketing_msg,
	}


def review_agent_output(message: Dict[str, Any]) -> Dict[str, str]:
	"""LLM call #2: assess output quality and return pass/fail + feedback."""
	system_prompt = (
		"You are a strict CEO reviewer. Return only valid JSON with keys: "
		"verdict, reason, feedback. Verdict must be pass or fail."
	)
	user_prompt = (
		f"Agent: {message.get('from_agent')}\n"
		f"Message type: {message.get('message_type')}\n"
		f"Payload: {message.get('payload')}\n"
		"Evaluate whether this output is specific, complete, and startup-ready. "
		"If insufficient, set verdict=fail and provide concrete feedback."
	)

	try:
		result = call_llm_json(system_prompt=system_prompt, user_prompt=user_prompt, **_ceo_llm_config())
		verdict = str(result.get("verdict", "")).strip().lower()
		if verdict not in {"pass", "fail"}:
			verdict = "fail"
		reason = str(result.get("reason", "CEO review completed")).strip() or "CEO review completed"
		feedback = str(result.get("feedback", "")).strip()
		return {"verdict": verdict, "reason": reason, "feedback": feedback}
	except LLMError as exc:
		print(f"[ceo] review failed, defaulting to fail-safe revision request: {exc}")
		return {
			"verdict": "fail",
			"reason": "Review service unavailable",
			"feedback": "Please refine output with more specificity and completeness.",
		}


def handle_incoming_for_review(message: Dict[str, Any]) -> None:
	"""Review incoming result messages and request revision when needed."""
	if message.get("to_agent") != "ceo":
		return
	if message.get("message_type") != "result":
		return

	review = review_agent_output(message)
	from_agent = str(message.get("from_agent", "")).strip()
	parent_id = message.get("message_id")

	if review["verdict"] == "pass":
		send_message(
			from_agent="ceo",
			to_agent=from_agent,
			message_type="confirmation",
			payload={"status": "accepted", "reason": review["reason"]},
			parent_message_id=parent_id,
		)
		return

	send_message(
		from_agent="ceo",
		to_agent=from_agent,
		message_type="revision_request",
		payload={
			"reason": review["reason"],
			"feedback": review["feedback"],
		},
		parent_message_id=parent_id,
	)


def run_review_loop() -> None:
	"""Continuously listen on ceo channel and review agent result messages."""
	print("[ceo] listening on channel 'ceo' for result reviews")
	while True:
		message = read_message("ceo")
		handle_incoming_for_review(message)
