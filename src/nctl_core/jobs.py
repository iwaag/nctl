"""Nautobot Job lookup, launch, terminal polling, and exact artifact download."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from nctl_core.artifacts import OperationArtifacts
from nctl_core.events import OperationLog
from nctl_core.nautobot import NautobotClient, NautobotError

SUCCESS_STATUSES = frozenset({"completed", "success", "successful"})
FAILURE_STATUSES = frozenset(
    {"failed", "failure", "errored", "error", "revoked", "canceled", "cancelled"}
)


class NautobotJobError(NautobotError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


class NautobotJobResult(BaseModel):
    job_name: str
    job_id: str
    job_result_id: str
    job_result_url: str
    status: str
    poll_count: int
    final_result: dict[str, Any] = Field(default_factory=dict)
    result_path: str | None = None
    artifact_name: str | None = None
    artifact_path: str | None = None


class NautobotJobRunner:
    def __init__(
        self,
        client: NautobotClient,
        *,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
        artifacts: OperationArtifacts | None = None,
        operation_log: OperationLog | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.artifacts = artifacts
        self.operation_log = operation_log
        self.sleep = sleep
        self.monotonic = monotonic

    def run(
        self,
        job_name: str,
        data: dict[str, Any],
        *,
        commit: bool = True,
        artifact_name: str | None = None,
        artifact_relative_path: str | Path | None = None,
    ) -> NautobotJobResult:
        job = self._lookup_exact(job_name)
        job_id = str(job.get("id") or job.get("pk") or "")
        if not job_id:
            raise NautobotJobError("job_id_missing", f"Nautobot Job {job_name!r} has no id")

        response = self.client.rest_post(
            f"/api/extras/jobs/{job_id}/run/",
            {"data": data, "commit": commit},
        )
        self._raise_response_error(response, "job_run_failed", f"cannot start Nautobot Job {job_name!r}")
        body = _response_json(response)
        job_result_id, job_result_url = extract_job_result_reference(body, response.headers.get("Location"))
        if not job_result_id:
            raise NautobotJobError(
                "job_result_id_missing",
                f"could not determine JobResult id after starting {job_name!r}",
            )
        self._emit("job_started", f"Nautobot Job {job_name} started", job_name=job_name, job_id=job_id,
                   job_result_id=job_result_id)

        final_result, status, poll_count = self._poll(job_name, job_result_id)
        sanitized_result = sanitize_job_result(final_result)
        result = NautobotJobResult(
            job_name=job_name,
            job_id=job_id,
            job_result_id=job_result_id,
            job_result_url=job_result_url or f"/api/extras/job-results/{job_result_id}/",
            status=status,
            poll_count=poll_count,
            final_result=sanitized_result,
        )
        if self.artifacts is not None:
            result.result_path = str(
                self.artifacts.write_json(f"jobs/{job_result_id}.json", sanitized_result)
            )
        if artifact_name is not None:
            if self.artifacts is None or artifact_relative_path is None:
                raise NautobotJobError(
                    "job_artifact_destination_missing",
                    "artifact download requires operation artifacts and a relative destination",
                )
            artifact = self._download_exact_artifact(job_result_id, artifact_name, artifact_relative_path)
            result.artifact_name = artifact_name
            result.artifact_path = str(artifact)

        self._emit(
            "job_completed",
            f"Nautobot Job {job_name} completed",
            job_name=job_name,
            job_result_id=job_result_id,
            status=status,
            artifact_path=result.artifact_path,
        )
        return result

    def _lookup_exact(self, job_name: str) -> dict[str, Any]:
        response = self.client.rest_get("/api/extras/jobs/", params={"q": job_name})
        self._raise_response_error(response, "job_lookup_failed", f"cannot look up Nautobot Job {job_name!r}")
        results = _response_json(response).get("results", [])
        exact = [row for row in results if isinstance(row, dict) and row.get("name") == job_name]
        if len(exact) != 1:
            raise NautobotJobError(
                "job_lookup_ambiguous" if exact else "job_not_found",
                f"expected exactly one Nautobot Job named {job_name!r}, found {len(exact)}",
                {"match_count": len(exact)},
            )
        return exact[0]

    def _poll(self, job_name: str, job_result_id: str) -> tuple[dict[str, Any], str, int]:
        started = self.monotonic()
        poll_count = 0
        while True:
            response = self.client.rest_get(f"/api/extras/job-results/{job_result_id}/")
            self._raise_response_error(
                response,
                "job_result_poll_failed",
                f"cannot poll Nautobot JobResult {job_result_id}",
            )
            payload = _response_json(response)
            poll_count += 1
            status = normalize_job_status(payload.get("status"))
            self._emit(
                "job_poll",
                f"Nautobot Job {job_name} status: {status or 'unknown'}",
                job_name=job_name,
                job_result_id=job_result_id,
                status=status,
                poll_count=poll_count,
            )
            if status in SUCCESS_STATUSES:
                return payload, status, poll_count
            if status in FAILURE_STATUSES:
                self._emit(
                    "job_failed",
                    f"Nautobot Job {job_name} failed with status {status}",
                    level="error",
                    job_name=job_name,
                    job_result_id=job_result_id,
                    status=status,
                )
                raise NautobotJobError(
                    "job_failed",
                    f"Nautobot Job {job_name!r} finished with status {status!r}",
                    {"job_result_id": job_result_id, "status": status},
                )
            elapsed = self.monotonic() - started
            if elapsed >= self.timeout_seconds:
                self._emit(
                    "job_failed",
                    f"Nautobot Job {job_name} timed out",
                    level="error",
                    job_name=job_name,
                    job_result_id=job_result_id,
                    status=status,
                )
                raise NautobotJobError(
                    "job_timeout",
                    f"Nautobot Job {job_name!r} did not finish within {self.timeout_seconds} seconds",
                    {"job_result_id": job_result_id, "last_status": status},
                )
            self.sleep(min(self.poll_interval_seconds, max(0.0, self.timeout_seconds - elapsed)))

    def _download_exact_artifact(
        self,
        job_result_id: str,
        artifact_name: str,
        artifact_relative_path: str | Path,
    ) -> Path:
        response = self.client.rest_get(
            "/api/extras/file-proxies/",
            params={"job_result_id": job_result_id, "name": artifact_name},
        )
        self._raise_response_error(response, "job_artifact_lookup_failed", f"cannot look up {artifact_name!r}")
        rows = _response_json(response).get("results", [])
        matches = [row for row in rows if _file_proxy_matches(row, job_result_id, artifact_name)]
        if len(matches) != 1:
            raise NautobotJobError(
                "job_artifact_ambiguous" if matches else "job_artifact_not_found",
                f"expected exactly one {artifact_name!r} artifact for JobResult {job_result_id}, found {len(matches)}",
                {"match_count": len(matches)},
            )
        proxy_id = matches[0].get("id") or matches[0].get("pk")
        if not proxy_id:
            raise NautobotJobError("job_artifact_id_missing", f"FileProxy {artifact_name!r} has no id")
        download = self.client.rest_download(f"/api/extras/file-proxies/{proxy_id}/download/")
        self._raise_response_error(download, "job_artifact_download_failed", f"cannot download {artifact_name!r}")
        assert self.artifacts is not None
        destination = self.artifacts.path(artifact_relative_path)
        try:
            return self.artifacts.write_text(artifact_relative_path, download.content.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise NautobotJobError(
                "job_artifact_invalid_text",
                f"Nautobot Job artifact {artifact_name!r} is not UTF-8 text: {exc}",
                {"destination": str(destination)},
            ) from exc

    @staticmethod
    def _raise_response_error(response: Any, code: str, message: str) -> None:
        if response.status_code in (401, 403):
            raise NautobotJobError(code, f"{message}: authentication failed ({response.status_code})")
        if not response.is_success:
            raise NautobotJobError(
                code,
                f"{message}: HTTP {response.status_code}",
                {"status_code": response.status_code},
            )

    def _emit(self, event: str, message: str, level: str = "info", **data: Any) -> None:
        if self.operation_log is not None:
            self.operation_log.emit(event, message, level=level, **data)


def normalize_job_status(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("name") or value.get("label")
    return str(value or "").strip().lower()


_SENSITIVE_RESULT_KEY = re.compile(
    r"(?i)^(data|args|kwargs|task_kwargs|job_kwargs|variables|request|token|password|passwd|secret|"
    r"api[_-]?key|credential)$"
)


def sanitize_job_result(value: Any) -> Any:
    """Remove submitted variables and credential-shaped fields from a JobResult payload."""
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _SENSITIVE_RESULT_KEY.search(str(key)) else sanitize_job_result(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_job_result(item) for item in value]
    return value


def extract_job_result_reference(body: dict[str, Any], location: str | None) -> tuple[str, str]:
    candidates: list[Any] = [body.get("job_result"), body.get("result"), body]
    urls: list[str] = [location] if location else []
    ids: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("id", "pk"):
                value = candidate.get(key)
                if value:
                    ids.append(str(value))
            for key in ("url", "absolute_url"):
                value = candidate.get(key)
                if value:
                    urls.append(str(value))
        elif isinstance(candidate, str):
            if "/api/extras/job-results/" in candidate:
                urls.append(candidate)
            elif candidate:
                ids.append(candidate)
    for url in urls:
        result_id = _job_result_id_from_url(url)
        if result_id:
            return result_id, url
    return (ids[0], f"/api/extras/job-results/{ids[0]}/") if ids else ("", "")


def _job_result_id_from_url(url: str) -> str:
    marker = "/api/extras/job-results/"
    if marker not in url:
        return ""
    return url.split(marker, 1)[1].split("/", 1)[0].split("?", 1)[0]


def _file_proxy_matches(row: Any, job_result_id: str, artifact_name: str) -> bool:
    if not isinstance(row, dict) or row.get("name") != artifact_name:
        return False
    related = row.get("job_result") or row.get("job_result_id")
    if isinstance(related, dict):
        related = related.get("id") or related.get("pk") or related.get("url")
    if related is None:
        return job_result_id in json.dumps(row, sort_keys=True, default=str)
    return str(related) == job_result_id or _job_result_id_from_url(str(related)) == job_result_id


def _response_json(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise NautobotJobError("invalid_json_response", "Nautobot returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise NautobotJobError("invalid_json_response", "Nautobot JSON response root is not an object")
    return payload
