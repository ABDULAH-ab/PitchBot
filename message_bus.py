import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import redis


LOG_PATH = Path("logs/message_log.json")
_LOG_LOCK = threading.Lock()
_ALLOWED_MESSAGE_TYPES = {"task", "result", "revision_request", "confirmation"}


def _redis_client(redis_url: Optional[str] = None) -> redis.Redis:
	"""Create a Redis client using explicit URL or REDIS_URL env var."""
	url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
	return redis.Redis.from_url(url, decode_responses=True)


def _ensure_log_file() -> None:
	"""Ensure the logs directory and JSON log file exist."""
	LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
	if not LOG_PATH.exists():
		LOG_PATH.write_text("[]\n", encoding="utf-8")


def _append_log(message: Dict[str, Any]) -> None:
	"""Append one message to logs/message_log.json in a thread-safe way."""
	_ensure_log_file()
	with _LOG_LOCK:
		raw = LOG_PATH.read_text(encoding="utf-8").strip() or "[]"
		data = json.loads(raw)
		if not isinstance(data, list):
			raise ValueError("message_log.json must contain a JSON array")
		data.append(message)
		LOG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def send_message(
	from_agent: str,
	to_agent: str,
	message_type: str,
	payload: Dict[str, Any],
	parent_message_id: Optional[str] = None,
	redis_url: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Build a structured message, publish it to Redis, and persist it to the JSON log.
	"""
	if message_type not in _ALLOWED_MESSAGE_TYPES:
		raise ValueError(
			"Invalid message_type. Expected one of: "
			f"{', '.join(sorted(_ALLOWED_MESSAGE_TYPES))}"
		)

	message: Dict[str, Any] = {
		"message_id": str(uuid.uuid4()),
		"from_agent": from_agent,
		"to_agent": to_agent,
		"message_type": message_type,
		"payload": payload,
		"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
	}
	if parent_message_id:
		message["parent_message_id"] = parent_message_id

	client = _redis_client(redis_url)
	client.publish(to_agent, json.dumps(message))
	_append_log(message)

	print(f"[{from_agent} -> {to_agent}] {message_type}")
	return message


def read_message(agent_name: str, redis_url: Optional[str] = None) -> Dict[str, Any]:
	"""
	Subscribe to one agent channel and block until a single message arrives.
	"""
	client = _redis_client(redis_url)
	pubsub = client.pubsub(ignore_subscribe_messages=True)
	pubsub.subscribe(agent_name)

	try:
		for item in pubsub.listen():
			if item.get("type") != "message":
				continue
			data = item.get("data")
			if not data:
				continue
			return json.loads(data)
	finally:
		pubsub.close()

	raise RuntimeError(f"Message stream for agent '{agent_name}' ended unexpectedly")


def get_full_history() -> list[Dict[str, Any]]:
	"""Return all logged messages from logs/message_log.json."""
	_ensure_log_file()
	raw = LOG_PATH.read_text(encoding="utf-8").strip() or "[]"
	history = json.loads(raw)
	if not isinstance(history, list):
		raise ValueError("message_log.json must contain a JSON array")
	return history
