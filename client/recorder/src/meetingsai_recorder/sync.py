from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type,
)


log = logging.getLogger("meetingsai.sync")


class SyncError(RuntimeError):
    pass


class AuthError(SyncError):
    pass


@dataclass(frozen=True)
class PushResult:
    accepted: int
    duplicates: int


_RETRYABLE = (httpx.TimeoutException, httpx.TransportError, httpx.RemoteProtocolError)


class VpsClient:
    def __init__(self, base_url: str, password: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._password = password
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "meetingsai-recorder/0.1"},
            follow_redirects=False,
        )
        self._logged_in = False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "VpsClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def login(self) -> None:
        try:
            r = self._client.post("/api/auth/login", json={"password": self._password})
        except _RETRYABLE as e:
            raise SyncError(f"login network error: {e}") from e
        if r.status_code == 200:
            self._logged_in = True
            log.info("vps login ok")
            return
        if r.status_code in (401, 429):
            raise AuthError(f"login rejected: {r.status_code}")
        raise SyncError(f"login unexpected: {r.status_code} {r.text[:200]}")

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=1.0, max=30.0),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    def _post_batch(self, meeting_id: str, payload: dict) -> httpx.Response:
        return self._client.post(
            f"/api/meetings/{meeting_id}/transcript",
            json=payload,
        )

    def push_batch(self, meeting_id: str, segments: list[dict]) -> PushResult:
        if not segments:
            return PushResult(accepted=0, duplicates=0)
        self._ensure_login()
        r = self._post_batch(meeting_id, {"segments": segments})

        if r.status_code == 401:
            log.info("session expired; re-logging in")
            self._logged_in = False
            self.login()
            r = self._post_batch(meeting_id, {"segments": segments})

        if r.status_code == 200:
            body = r.json()
            return PushResult(
                accepted=int(body.get("accepted", 0)),
                duplicates=int(body.get("duplicates", 0)),
            )
        if r.status_code == 404:
            raise SyncError(f"meeting {meeting_id} not found on VPS")
        if r.status_code in (401, 403):
            raise AuthError(f"push rejected: {r.status_code}")
        raise SyncError(f"push unexpected: {r.status_code} {r.text[:200]}")

    # ---- 9.5: meeting selection helpers ----

    def create_meeting(
        self,
        title: str,
        objective: str | None = None,
        organization: str | None = None,
        role: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_login()
        body: dict[str, Any] = {"title": title}
        if objective is not None: body["objective"] = objective
        if organization is not None: body["organization"] = organization
        if role is not None: body["role"] = role
        if notes is not None: body["notes"] = notes
        r = self._client.post("/api/meetings", json=body)
        if r.status_code == 201:
            return r.json()
        if r.status_code in (401, 403):
            raise AuthError(f"create meeting rejected: {r.status_code}")
        raise SyncError(f"create meeting unexpected: {r.status_code} {r.text[:200]}")

    def list_meetings(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        self._ensure_login()
        r = self._client.get("/api/meetings", params={"limit": limit, "offset": offset})
        if r.status_code == 200:
            return list(r.json().get("items", []))
        if r.status_code in (401, 403):
            raise AuthError(f"list meetings rejected: {r.status_code}")
        raise SyncError(f"list meetings unexpected: {r.status_code} {r.text[:200]}")
