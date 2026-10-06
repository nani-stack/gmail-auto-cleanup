"""Gmail access: OAuth, paced requests, fetching metadata, applying labels.

Gmail's published quota is 15,000 units per minute per user, but real accounts
are often held well below that. Every request therefore goes through one paced
queue that backs off when Gmail pushes back and speeds up again when it stops.
"""

from __future__ import annotations

import html
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # labels and archive; cannot delete forever
RETRYABLE = (403, 429, 500, 502, 503, 504)


def credentials(directory: Path) -> Credentials:
    """Load the saved sign-in, refreshing or prompting for one as needed."""
    token_path = directory / "token.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            secrets = directory / "credentials.json"
            if not secrets.exists():
                raise SystemExit(f"Missing {secrets}. See the README's Google Cloud setup.")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            print("Open the link below in a browser and sign in:")
            creds = flow.run_local_server(port=0, open_browser=False)
        token_path.write_text(creds.to_json())
        token_path.chmod(0o600)
    return creds


class Mailbox:
    """A paced Gmail client."""

    def __init__(self, creds: Credentials, requests_per_second: float = 4.5):
        self.creds = creds
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._interval = 1 / max(requests_per_second, 0.2)
        self._floor = self._interval
        self._ceiling = max(self._interval * 4, 0.5)
        self._next_slot = 0.0
        self._ok_streak = 0
        self._lock = threading.Lock()
        self._local = threading.local()

    def request(self, req, tries: int = 8):
        """Run one Gmail request inside the pacing queue, retrying rate limits."""
        for attempt in range(tries):
            with self._lock:
                now = time.monotonic()
                wait = max(0.0, self._next_slot - now)
                self._next_slot = max(now, self._next_slot) + self._interval
            time.sleep(wait)
            try:
                result = req.execute()
            except HttpError as err:
                if err.resp.status not in RETRYABLE or attempt == tries - 1:
                    raise
                with self._lock:
                    self._interval = min(self._ceiling, self._interval * 1.2)
                    self._ok_streak = 0
                delay = min(75.0, 10 * 2 ** attempt) + random.random() * 3
                print(f"  rate limited: waiting {delay:.0f}s, now {1 / self._interval:.1f} req/s",
                      flush=True)
                time.sleep(delay)
                continue
            with self._lock:
                self._ok_streak += 1
                if self._ok_streak >= 100:
                    self._interval = max(self._floor, self._interval * 0.9)
                    self._ok_streak = 0
            return result

    def message_ids(self, query: str, limit: int) -> list[str]:
        """Collect ids up front, so later label changes cannot disturb paging."""
        ids: list[str] = []
        page = None
        while len(ids) < limit:
            resp = self.request(self.svc.users().messages().list(
                userId="me", q=query, maxResults=min(500, limit - len(ids)), pageToken=page))
            ids += [m["id"] for m in resp.get("messages", [])]
            page = resp.get("nextPageToken")
            print(f"  found {len(ids)} emails", end="\r", flush=True)
            if not page:
                break
        print()
        return ids

    def fetch(self, ids: list[str], workers: int = 4) -> list[dict]:
        """Fetch sender, subject, preview and age. Never message bodies."""
        def one(mid: str) -> dict | None:
            if not hasattr(self._local, "svc"):
                self._local.svc = build("gmail", "v1", credentials=self.creds,
                                        cache_discovery=False)
            try:
                m = self.request(self._local.svc.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject"]))
            except HttpError as err:
                print(f"  skipping {mid}: {err.resp.status}", flush=True)
                return None
            headers = {h["name"]: h["value"] for h in m["payload"].get("headers", [])}
            return {
                "id": mid,
                "labels": m.get("labelIds", []),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": html.unescape(m.get("snippet", "")),
                "age_days": (time.time() - int(m.get("internalDate", 0)) / 1000) / 86400,
            }

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return [e for e in pool.map(one, ids) if e]

    def label_ids(self, names: dict[str, str]) -> dict[str, str]:
        """Map action -> label id, creating any label that does not exist yet."""
        existing = {l["name"]: l["id"] for l in
                    self.request(self.svc.users().labels().list(userId="me"))["labels"]}
        out = {}
        for action, name in names.items():
            if name not in existing:
                existing[name] = self.request(self.svc.users().labels().create(
                    userId="me", body={"name": name}))["id"]
            out[action] = existing[name]
        return out

    def apply(self, emails: list[dict], labels: dict[str, str], clear_prior: bool = False) -> None:
        """Label each group, archiving the cleanup group. Nothing is deleted."""
        for action, label in labels.items():
            ids = [e["id"] for e in emails if e["action"] == action]
            if not ids:
                continue
            remove = ["INBOX"] if action == "CLEANUP" else []
            if clear_prior:  # re-judged mail drops the label it carried before
                remove += [lid for a, lid in labels.items() if a != action]
            for i in range(0, len(ids), 1000):
                self.request(self.svc.users().messages().batchModify(
                    userId="me",
                    body={"ids": ids[i:i + 1000], "addLabelIds": [label],
                          "removeLabelIds": remove}))

    def trash(self, mid: str) -> None:
        """Move one message to Trash, where Gmail keeps it for 30 days."""
        self.request(self.svc.users().messages().trash(userId="me", id=mid))
