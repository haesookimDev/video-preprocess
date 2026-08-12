"""Small asynchronous HTTP transport backed by the Python standard library."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True, slots=True)
class HTTPTransportResponse:
    """Raw bounded HTTP response returned to the inference provider."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        return None


class UrllibHTTPTransport:
    """Run blocking urllib calls in worker threads without following redirects."""

    def __init__(self, *, max_response_bytes: int = 4 * 1024 * 1024) -> None:
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        self.max_response_bytes = max_response_bytes
        self._opener = build_opener(_NoRedirectHandler())

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_sec: float,
    ) -> HTTPTransportResponse:
        return await asyncio.to_thread(
            self._request_sync,
            method,
            url,
            headers,
            body,
            timeout_sec,
        )

    def _request_sync(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_sec: float,
    ) -> HTTPTransportResponse:
        request = Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout_sec)
        except HTTPError as exc:
            response = exc
        with response:
            payload = response.read(self.max_response_bytes + 1)
            if len(payload) > self.max_response_bytes:
                raise ValueError("HTTP response exceeded the configured limit")
            return HTTPTransportResponse(
                status=int(response.status),
                headers={
                    name.lower(): value
                    for name, value in response.headers.items()
                },
                body=payload,
            )
