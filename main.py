import threading
import os
from typing import Any, Dict

from dotenv import load_dotenv

from agents.ceo_agent import dispatch_tasks, review_agent_output
from agents.engineer_agent import run_loop as run_engineer_loop
from agents.marketing_agent import run_loop as run_marketing_loop
from agents.product_agent import run_loop as run_product_loop
from agents.qa_agent import run_loop as run_qa_loop
from message_bus import get_full_history, read_message, send_message
from utils.slack_api import SlackAPIError, post_message


MAX_CEO_MESSAGES = 40


def _get_startup_idea() -> str:
	while True:
		try:
			user_idea = input("Enter startup idea: ").strip()
		except EOFError as exc:
			raise RuntimeError("Startup idea input is required") from exc

		if user_idea:
			return user_idea

		print("Startup idea cannot be empty. Please enter a valid idea.")


def _start_agent_threads() -> list[threading.Thread]:
	threads: list[threading.Thread] = []
	for target in (run_product_loop, run_engineer_loop, run_marketing_loop, run_qa_loop):
		thread = threading.Thread(target=target, daemon=True)
		thread.start()
		threads.append(thread)
	return threads


def _find_latest_product_spec() -> Dict[str, Any]:
	history = get_full_history()
	for item in reversed(history):
		if item.get("from_agent") == "product" and item.get("message_type") == "result":
			payload = item.get("payload", {})
			if isinstance(payload, dict) and isinstance(payload.get("product_spec"), dict):
				return payload["product_spec"]
	return {}


def _build_summary_blocks(status: str, qa_verdict: str, pr_url: str) -> list[Dict[str, Any]]:
	return [
		{
			"type": "header",
			"text": {"type": "plain_text", "text": "PitchBot Run Summary"},
		},
		{
			"type": "section",
			"text": {
				"type": "mrkdwn",
				"text": (
					f"*Overall Status:* {status}\n"
					f"*QA Verdict:* {qa_verdict}\n"
					f"*PR:* <{pr_url}|View Pull Request>"
				),
			},
		},
	]


def run() -> None:
	load_dotenv()
	_start_agent_threads()
	startup_idea = _get_startup_idea()

	print("[main] dispatching CEO startup tasks")
	dispatch_tasks(startup_idea)

	latest_results: Dict[str, Dict[str, Any]] = {}
	qa_dispatched = False
	engineer_ready_for_qa = False
	qa_verdict = "unknown"
	final_status = "in_progress"

	for _ in range(MAX_CEO_MESSAGES):
		message = read_message("ceo")
		from_agent = str(message.get("from_agent", "")).strip()
		message_type = message.get("message_type")

		if message_type != "result":
			continue

		payload = message.get("payload", {})
		if not isinstance(payload, dict):
			payload = {}
		latest_results[from_agent] = payload
		if from_agent == "engineer":
			engineer_ready_for_qa = True

		review = review_agent_output(message)
		if review["verdict"] == "pass":
			send_message(
				from_agent="ceo",
				to_agent=from_agent,
				message_type="confirmation",
				payload={"status": "accepted", "reason": review["reason"]},
				parent_message_id=message.get("message_id"),
			)
		else:
			if from_agent == "qa":
				send_message(
					from_agent="ceo",
					to_agent="engineer",
					message_type="revision_request",
					payload={
						"reason": "QA reported issues",
						"feedback": review["feedback"],
						"product_spec": _find_latest_product_spec(),
					},
					parent_message_id=message.get("message_id"),
				)
			else:
				send_message(
					from_agent="ceo",
					to_agent=from_agent,
					message_type="revision_request",
					payload={"reason": review["reason"], "feedback": review["feedback"]},
					parent_message_id=message.get("message_id"),
				)

		if (
			not qa_dispatched
			and engineer_ready_for_qa
			and "engineer" in latest_results
			and "marketing" in latest_results
		):
			engineer_payload = latest_results.get("engineer", {})
			marketing_payload = latest_results.get("marketing", {})
			qa_payload = {
				"product_spec": _find_latest_product_spec(),
				"html": engineer_payload.get("html", ""),
				"pr_url": engineer_payload.get("pr_url", ""),
				"marketing_copy": marketing_payload.get("marketing_copy", {}),
			}
			send_message(from_agent="ceo", to_agent="qa", message_type="task", payload=qa_payload)
			qa_dispatched = True
			print("[main] QA task dispatched")

		if from_agent == "qa":
			qa_report = payload.get("qa_report", {})
			if isinstance(qa_report, dict):
				qa_verdict = str(qa_report.get("verdict", "unknown"))
			if qa_verdict.lower() == "pass":
				final_status = "completed"
				break

			# Keep loop alive: request engineer revision and wait for fresh engineer output,
			# then dispatch QA again.
			qa_dispatched = False
			engineer_ready_for_qa = False

	pr_url = ""
	if isinstance(latest_results.get("engineer"), dict):
		pr_url = str(latest_results["engineer"].get("pr_url", "")).strip()

	try:
		summary_channel = os.getenv("SLACK_CHANNEL", "#launches")
		post_message(
			channel=summary_channel,
			blocks=_build_summary_blocks(
				status=final_status,
				qa_verdict=qa_verdict,
				pr_url=pr_url or "https://github.com",
			),
		)
		print("[main] final summary posted to Slack")
	except SlackAPIError as exc:
		print(f"[main] final summary Slack post failed: {exc}")

	history = get_full_history()
	print(f"\n[main] message history count: {len(history)}")
	for item in history:
		print(
			f"{item['timestamp']} | {item['from_agent']} -> {item['to_agent']} "
			f"| {item['message_type']}"
		)


if __name__ == "__main__":
	run()
