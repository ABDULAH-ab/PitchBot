import os
import time
from typing import Any, Dict, List, Optional

import requests


class SlackAPIError(RuntimeError):
	pass


_RETRY_DELAY_SECONDS = float(os.getenv("API_RETRY_DELAY_SECONDS", "2"))


def _is_retriable_status(status_code: int) -> bool:
	return status_code in {408, 409, 425, 429} or status_code >= 500


def _is_retriable_slack_error(data: Dict[str, Any]) -> bool:
	err = data.get("error")
	return err in {"ratelimited", "internal_error", "fatal_error", "request_timeout", "service_unavailable"}


def _token() -> str:
	token = os.getenv("SLACK_BOT_TOKEN")
	if not token:
		raise SlackAPIError("Missing SLACK_BOT_TOKEN in environment")
	return token


def _default_channel() -> str:
	channel = os.getenv("SLACK_CHANNEL")
	if not channel:
		raise SlackAPIError("Missing SLACK_CHANNEL in environment")
	return channel


def build_launch_blocks(tagline: str, description: str, pr_url: str) -> List[Dict[str, Any]]:
	"""Return a Block Kit payload for a launch announcement."""
	return [
		{
			"type": "header",
			"text": {"type": "plain_text", "text": tagline[:150]},
		},
		{
			"type": "section",
			"text": {"type": "mrkdwn", "text": description},
		},
		{
			"type": "section",
			"fields": [
				{"type": "mrkdwn", "text": f"*GitHub PR:* <{pr_url}|View PR>"},
				{"type": "mrkdwn", "text": "*Status:* Launch in progress"},
			],
		},
	]


def post_message(channel: str, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
	"""Post a Block Kit message to Slack and return the API response."""
	attempt = 0
	while True:
		attempt += 1
		try:
			response = requests.post(
				"https://slack.com/api/chat.postMessage",
				headers={
					"Authorization": f"Bearer {_token()}",
					"Content-Type": "application/json",
				},
				json={"channel": channel, "blocks": blocks},
				timeout=30,
			)
			response.raise_for_status()
			data = response.json()
		except requests.RequestException as exc:
			response = getattr(exc, "response", None)
			status_code = response.status_code if response is not None else None
			if status_code is not None and not _is_retriable_status(status_code):
				raise SlackAPIError(f"Slack request failed: {exc}") from exc
			status_text = status_code if status_code is not None else "network-error"
			print(f"[slack] post_message failed with {status_text} on attempt {attempt}; retrying...")
			time.sleep(_RETRY_DELAY_SECONDS)
			continue

		if data.get("ok"):
			return data

		if _is_retriable_slack_error(data):
			print(f"[slack] Slack API returned retriable error on attempt {attempt}: {data.get('error')}; retrying...")
			time.sleep(_RETRY_DELAY_SECONDS)
			continue

		raise SlackAPIError(f"Slack API error: {data}")


def post_launch_message(
	tagline: str,
	description: str,
	pr_url: str,
	channel: Optional[str] = None,
) -> Dict[str, Any]:
	"""Build and post a standard launch message to Slack."""
	selected_channel = channel or _default_channel()
	blocks = build_launch_blocks(tagline=tagline, description=description, pr_url=pr_url)
	return post_message(channel=selected_channel, blocks=blocks)
