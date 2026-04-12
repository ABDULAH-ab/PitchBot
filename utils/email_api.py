import os
import time
from typing import Optional

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail


class EmailAPIError(RuntimeError):
	pass


_RETRY_DELAY_SECONDS = float(os.getenv("API_RETRY_DELAY_SECONDS", "2"))


def _is_retriable_status(status_code: int) -> bool:
	return status_code in {408, 409, 425, 429} or status_code >= 500


def _api_key() -> str:
	key = os.getenv("SENDGRID_API_KEY")
	if not key:
		raise EmailAPIError("Missing SENDGRID_API_KEY in environment")
	return key


def _default_from_email() -> str:
	from_email = os.getenv("SENDGRID_FROM_EMAIL")
	if not from_email:
		raise EmailAPIError("Missing SENDGRID_FROM_EMAIL in environment")
	return from_email


def send_cold_email(
	to_email: str,
	subject: str,
	body: str,
	from_email: Optional[str] = None,
	use_html: bool = False,
) -> int:
	"""Send a cold outreach email via SendGrid and return HTTP status code."""
	selected_from = from_email or _default_from_email()

	message = Mail(
		from_email=selected_from,
		to_emails=to_email,
		subject=subject,
		plain_text_content=(None if use_html else body),
		html_content=(body if use_html else None),
	)

	client = SendGridAPIClient(_api_key())
	attempt = 0
	while True:
		attempt += 1
		try:
			response = client.send(message)
			status_code = int(response.status_code)
			if status_code < 400:
				return status_code
			if _is_retriable_status(status_code):
				print(f"[email] SendGrid returned {status_code} on attempt {attempt}; retrying...")
				time.sleep(_RETRY_DELAY_SECONDS)
				continue
			raise EmailAPIError(f"SendGrid send failed with non-retriable status {status_code}: {response.body}")
		except EmailAPIError:
			raise
		except Exception as exc:
			# Network or transport errors are retried indefinitely.
			print(f"[email] SendGrid request failed on attempt {attempt}: {exc}; retrying...")
			time.sleep(_RETRY_DELAY_SECONDS)
