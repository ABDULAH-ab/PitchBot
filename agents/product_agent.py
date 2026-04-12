import os
from typing import Any, Dict

from message_bus import read_message, send_message
from utils.llm import LLMError, call_llm_json, resolve_llm_for_role


def _product_llm_config() -> Dict[str, str | None]:
	config = resolve_llm_for_role("product")
	return {
		"provider": config.get("provider") or os.getenv("LLM_PROVIDER", "openai"),
		"model": config.get("model"),
	}


def _default_spec(idea: str) -> Dict[str, Any]:
	return {
		"value_proposition": f"A focused startup product based on: {idea}",
		"personas": [
			{
				"name": "Student Seller",
				"role": "Primary user",
				"pain_point": "Needs a fast way to publish and manage listings",
			},
			{
				"name": "Student Buyer",
				"role": "Customer",
				"pain_point": "Needs trustworthy and affordable options quickly",
			},
		],
		"features": [
			{"name": "Onboarding", "description": "Quick signup and profile setup", "priority": 1},
			{"name": "Listing flow", "description": "Create and edit listings with images", "priority": 2},
			{"name": "Search", "description": "Filter and sort by relevance and price", "priority": 3},
			{"name": "Messaging", "description": "In-app buyer and seller chat", "priority": 4},
			{"name": "Trust signals", "description": "Ratings and verification badges", "priority": 5},
		],
		"user_stories": [
			"As a seller, I want to create a listing quickly so that I can start getting offers immediately.",
			"As a buyer, I want to compare relevant listings so that I can choose the best option confidently.",
			"As a user, I want to message the other party safely so that I can finalize details without leaving the platform.",
		],
	}


def _normalize_spec(spec: Dict[str, Any], idea: str) -> Dict[str, Any]:
	"""Keep minimum structure stable even if LLM output is partially malformed."""
	default = _default_spec(idea)

	value_proposition = spec.get("value_proposition")
	if not isinstance(value_proposition, str) or not value_proposition.strip():
		value_proposition = default["value_proposition"]

	personas = spec.get("personas")
	if not isinstance(personas, list) or len(personas) < 2:
		personas = default["personas"]

	features = spec.get("features")
	if not isinstance(features, list) or len(features) < 5:
		features = default["features"]

	user_stories = spec.get("user_stories")
	if not isinstance(user_stories, list) or len(user_stories) < 3:
		user_stories = default["user_stories"]

	return {
		"value_proposition": value_proposition,
		"personas": personas,
		"features": features,
		"user_stories": user_stories,
	}


def generate_product_spec(payload: Dict[str, Any]) -> Dict[str, Any]:
	idea = str(payload.get("idea", "")).strip()
	focus = str(payload.get("focus", "")).strip()

	system_prompt = (
		"You are a product manager agent. Return only valid JSON with keys "
		"value_proposition, personas, features, user_stories."
	)
	user_prompt = (
		f"Startup idea: {idea}\n"
		f"Focus: {focus}\n"
		"Requirements:\n"
		"- value_proposition: one clear sentence\n"
		"- personas: array of 2-3 objects with name, role, pain_point\n"
		"- features: array of exactly 5 objects with name, description, priority\n"
		"- user_stories: array of exactly 3 strings in As a / I want / So that format\n"
		"- no markdown and no extra text"
	)

	try:
		spec = call_llm_json(system_prompt=system_prompt, user_prompt=user_prompt, **_product_llm_config())
		if not isinstance(spec, dict):
			return _default_spec(idea)
		return _normalize_spec(spec, idea)
	except LLMError as exc:
		print(f"[product] LLM error, using fallback spec: {exc}")
		return _default_spec(idea)


def _handle_message(message: Dict[str, Any]) -> None:
	message_type = message.get("message_type")
	if message_type not in {"task", "revision_request"}:
		return

	payload = message.get("payload", {})
	if not isinstance(payload, dict):
		payload = {}

	spec = generate_product_spec(payload)
	result_payload = {"product_spec": spec}

	send_message(
		from_agent="product",
		to_agent="engineer",
		message_type="result",
		payload=result_payload,
		parent_message_id=message.get("message_id"),
	)

	send_message(
		from_agent="product",
		to_agent="marketing",
		message_type="result",
		payload=result_payload,
		parent_message_id=message.get("message_id"),
	)

	send_message(
		from_agent="product",
		to_agent="ceo",
		message_type="result",
		payload=result_payload,
		parent_message_id=message.get("message_id"),
	)

	send_message(
		from_agent="product",
		to_agent="ceo",
		message_type="confirmation",
		payload={"status": "product_spec_ready"},
		parent_message_id=message.get("message_id"),
	)


def run_loop() -> None:
	print("[product] listening on channel 'product'")
	while True:
		message = read_message("product")
		_handle_message(message)
