import json
import os
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv


load_dotenv()


class LLMError(RuntimeError):
	pass


_RETRY_DELAY_SECONDS = float(os.getenv("API_RETRY_DELAY_SECONDS", "2"))


def _is_retriable_status(status_code: int) -> bool:
	return status_code in {408, 409, 425, 429} or status_code >= 500


def _is_retriable_request_exception(exc: requests.RequestException) -> bool:
	response = getattr(exc, "response", None)
	if response is None:
		return True
	return _is_retriable_status(response.status_code)


def _env(name: str, default: Optional[str] = None) -> str:
	value = os.getenv(name, default)
	if value is None or value == "":
		raise LLMError(f"Missing {name} in environment")
	return value


def resolve_llm_for_role(role: str) -> Dict[str, Optional[str]]:
	"""
	Resolve provider/model for an agent role.

	Precedence:
	1) <ROLE>_LLM_PROVIDER / <ROLE>_LLM_MODEL
	2) For CEO only: CEO-specific values, then global LLM_PROVIDER
	3) For non-CEO roles: AGENT_LLM_PROVIDER / AGENT_LLM_MODEL
	4) Fallback to global defaults in call_llm.
	"""
	key = role.strip().upper()
	provider = os.getenv(f"{key}_LLM_PROVIDER")
	model = os.getenv(f"{key}_LLM_MODEL")

	if key != "CEO":
		provider = provider or os.getenv("AGENT_LLM_PROVIDER")
		model = model or os.getenv("AGENT_LLM_MODEL")
	else:
		provider = provider or os.getenv("LLM_PROVIDER")

	return {
		"provider": provider,
		"model": model,
	}


def _openai_chat_completion(system_prompt: str, user_prompt: str, model: str, max_tokens: int) -> str:
	api_key = _env("OPENAI_API_KEY")
	base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
	url = f"{base_url}/chat/completions"
	payload = {
		"model": model,
		"messages": [
			{"role": "system", "content": system_prompt},
			{"role": "user", "content": user_prompt},
		],
		"max_tokens": max_tokens,
	}
	attempt = 0
	while True:
		attempt += 1
		try:
			response = requests.post(
				url,
				headers={
					"Authorization": f"Bearer {api_key}",
					"Content-Type": "application/json",
				},
				json=payload,
				timeout=60,
			)
			response.raise_for_status()
			data = response.json()
			break
		except requests.RequestException as exc:
			if not _is_retriable_request_exception(exc):
				raise LLMError(f"OpenAI request failed: {exc}") from exc
			print(f"[llm] OpenAI call failed on attempt {attempt}; retrying...")
			time.sleep(_RETRY_DELAY_SECONDS)

	try:
		return data["choices"][0]["message"]["content"]
	except (KeyError, IndexError, TypeError) as exc:
		raise LLMError(f"Unexpected OpenAI response: {data}") from exc


def _gemini_generate_content(system_prompt: str, user_prompt: str, model: str, max_tokens: int) -> str:
	api_key = _env("GEMINI_API_KEY")
	url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
	payload = {
		"systemInstruction": {"parts": [{"text": system_prompt}]},
		"contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
		"generationConfig": {"maxOutputTokens": max_tokens},
	}
	attempt = 0
	while True:
		attempt += 1
		try:
			response = requests.post(
				url,
				params={"key": api_key},
				headers={"Content-Type": "application/json"},
				json=payload,
				timeout=60,
			)
			response.raise_for_status()
			data = response.json()
			break
		except requests.RequestException as exc:
			if not _is_retriable_request_exception(exc):
				raise LLMError(f"Gemini request failed: {exc}") from exc
			print(f"[llm] Gemini call failed on attempt {attempt}; retrying...")
			time.sleep(_RETRY_DELAY_SECONDS)

	try:
		return data["candidates"][0]["content"]["parts"][0]["text"]
	except (KeyError, IndexError, TypeError) as exc:
		raise LLMError(f"Unexpected Gemini response: {data}") from exc


def call_llm(
	system_prompt: str,
	user_prompt: str,
	provider: Optional[str] = None,
	model: Optional[str] = None,
	max_tokens: int = 1000,
) -> str:
	"""
	Call a configured LLM provider and return the raw text response.

	Supported providers: openai, gemini.
	The provider can be passed explicitly or set via LLM_PROVIDER.
	"""
	selected_provider = (provider or os.getenv("LLM_PROVIDER", "openai")).strip().lower()

	if selected_provider == "openai":
		selected_model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
		return _openai_chat_completion(system_prompt, user_prompt, selected_model, max_tokens)

	if selected_provider == "gemini":
		selected_model = model or os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
		return _gemini_generate_content(system_prompt, user_prompt, selected_model, max_tokens)

	raise LLMError(f"Unsupported provider: {selected_provider}")


def call_llm_json(
	system_prompt: str,
	user_prompt: str,
	provider: Optional[str] = None,
	model: Optional[str] = None,
	max_tokens: int = 1000,
) -> Dict[str, Any]:
	"""Call the LLM and parse the response as JSON, even if wrapped in text."""
	raw = call_llm(
		system_prompt=system_prompt,
		user_prompt=user_prompt,
		provider=provider,
		model=model,
		max_tokens=max_tokens,
	)

	text = raw.strip()
	if text.startswith("```"):
		text = text.strip("`")
		if text.startswith("json"):
			text = text[4:]
		text = text.strip()

	start = text.find("{")
	end = text.rfind("}")
	if start == -1 or end == -1 or end <= start:
		raise LLMError(f"LLM did not return JSON: {raw}")

	try:
		return json.loads(text[start : end + 1])
	except json.JSONDecodeError as exc:
		raise LLMError(f"Failed to parse JSON response: {raw}") from exc
