"""Read-only Jira transport and explicit pagination coverage (Python stdlib).

Cloud search uses the enhanced endpoint, not the retired /search endpoint:
https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/
https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/
"""

import base64
import datetime
import email.utils
import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


ISSUE_FIELDS = (
    "summary", "description", "assignee", "reporter", "creator", "status",
    "resolution", "created", "updated", "parent", "issuetype", "project",
    "issuelinks", "resolutiondate",
)
PAGE_SIZE = 100
MAX_PAGES = 10000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_RETRY_DELAY = 30.0


@dataclass
class PageResult:
    """Complete means all API-visible pages, not all Jira permissions/data."""

    items: List[Dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    gaps: List[str] = field(default_factory=list)


class JiraError(Exception):
    """Safe for display: never includes response bodies, credentials, or JQL."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject same-origin redirects too: correct the configured API URL instead.
        return None


def _validated_base(url: str) -> str:
    if not isinstance(url, str) or not url or any(c.isspace() for c in url):
        raise JiraError("Jira URL must be an HTTPS base URL without whitespace.")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise JiraError("Invalid Jira base URL.") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "\\" in url or (port is not None and not 1 <= port <= 65535)):
        raise JiraError("Jira URL must use HTTPS without credentials, query, or fragment.")
    decoded_path = urllib.parse.unquote(parsed.path)
    if any(part in (".", "..") for part in decoded_path.split("/")):
        raise JiraError("Jira base URL cannot contain relative path segments.")
    return url.rstrip("/")


def _retry_delay(value: Optional[str], attempt: int) -> float:
    delay = min(2.0 ** attempt, MAX_RETRY_DELAY)
    if value:
        try:
            candidate = float(value)
            if math.isfinite(candidate):
                delay = candidate
        except (ValueError, TypeError):
            try:
                retry_at = email.utils.parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                delay = (retry_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    return min(MAX_RETRY_DELAY, max(0.0, delay))


def _nonnegative_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _add_items(result: PageResult, rows: List[Dict[str, Any]], seen: set) -> int:
    added = 0
    for row in rows:
        identity = row.get("id") or row.get("key")
        if identity is None:
            # Preserve unusual records; do not silently discard them as duplicate null IDs.
            identity = json.dumps(row, sort_keys=True, ensure_ascii=False)
        marker = str(identity)
        if marker not in seen:
            seen.add(marker)
            result.items.append(row)
            added += 1
    return added


def _rows(page: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    values = page.get(key)
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        raise JiraError("Jira returned an invalid page structure.")
    return values


class JiraClient:
    """Cloud email/API-token Basic auth.

    api_url optionally supplies the API base before /rest/api/N (for example,
    https://api.atlassian.com/ex/jira/CLOUD_ID for scoped Cloud API tokens).
    Transport exceptions expose only a status/category; URLs and bodies are omitted.
    """

    def __init__(self, base_url: str, token: str, deployment: str = "cloud",
                 email: Optional[str] = None, api_url: Optional[str] = None,
                 timeout: float = 30, max_retries: int = 2):
        self.base_url = _validated_base(base_url)
        self.api_url = _validated_base(api_url or base_url)
        if deployment != "cloud":
            raise JiraError("This collector supports Jira Cloud only.")
        if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
            raise JiraError("A Jira API token is required.")
        if deployment == "cloud" and (not isinstance(email, str) or not email.strip()
                                      or any(c in email for c in ":\r\n")):
            raise JiraError("Cloud authentication requires an email address.")
        if not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
            raise JiraError("max_retries must be between 0 and 5.")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise JiraError("timeout must be between 0 and 300 seconds.")
        self.deployment = deployment
        self.timeout = timeout
        self.max_retries = max_retries
        self._prefix = "/rest/api/3"
        credential = base64.b64encode((email + ":" + token).encode("utf-8")).decode("ascii")
        self._authorization = "Basic " + credential
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None) -> Dict[str, Any]:
        if method not in ("GET", "POST") or not path.startswith("/") or path.startswith("//"):
            raise JiraError("Invalid internal Jira request.")
        url = self.api_url + self._prefix + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": self._authorization,
                   "User-Agent": "jira-achievements/1"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise JiraError("Jira response exceeded the size limit; data is incomplete.")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise JiraError("Jira returned invalid JSON.") from None
                if not isinstance(data, dict):
                    raise JiraError("Jira returned an invalid response structure.")
                return data
            except urllib.error.HTTPError as exc:
                code = exc.code
                delay = _retry_delay(exc.headers.get("Retry-After") if exc.headers else None, attempt)
                exc.close()
                if (code == 429 or 500 <= code <= 599) and attempt < self.max_retries:
                    time.sleep(delay)
                    continue
                if 300 <= code < 400:
                    raise JiraError("Jira redirected the request; verify the API base URL. Redirects are disabled.", code) from None
                raise JiraError("Jira request failed (HTTP %s)." % code, code) from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                raise JiraError("Jira connection failed; check the URL, TLS, network, and timeout.") from None
        raise JiraError("Jira retry limit reached.")

    def current_user(self) -> Dict[str, Any]:
        return self._request("GET", "/myself")

    def get_issue(self, key: str) -> Dict[str, Any]:
        return self._request("GET", "/issue/" + urllib.parse.quote(str(key), safe=""),
                             params={"fields": ",".join(ISSUE_FIELDS)})

    def search_issues(self, jql: str, fields: Optional[List[str]] = None) -> PageResult:
        body = {"jql": jql, "fields": list(ISSUE_FIELDS if fields is None else fields),
                "maxResults": PAGE_SIZE}
        result = PageResult()
        seen_items, seen_tokens = set(), set()
        token = None
        for _ in range(MAX_PAGES):
            request_body = dict(body)
            if token is not None:
                request_body["nextPageToken"] = token
            try:
                page = self._request("POST", "/search/jql", body=request_body)
                rows = _rows(page, "issues")
            except JiraError as exc:
                return self._incomplete(result, "search: " + str(exc))
            added = _add_items(result, rows, seen_items)
            next_token = page.get("nextPageToken")
            if page.get("isLast") is True:
                return result
            if not isinstance(next_token, str) or not next_token:
                return self._incomplete(result, "search: missing final-page signal or nextPageToken.")
            if next_token in seen_tokens or (rows and added == 0):
                return self._incomplete(result, "search: pagination repeated a cursor or made no progress.")
            seen_tokens.add(next_token)
            token = next_token
        return self._incomplete(result, "search: pagination safety limit reached.")

    def _offset_pages(self, method: str, path: str, items_key: str,
                      body: Optional[dict] = None) -> PageResult:
        result = PageResult()
        seen_items = set()
        offset = 0
        for _ in range(MAX_PAGES):
            pagination = {"startAt": offset, "maxResults": PAGE_SIZE}
            try:
                if body is None:
                    page = self._request(method, path, params=pagination)
                else:
                    page = self._request(method, path, body=dict(body, **pagination))
                rows = _rows(page, items_key)
            except JiraError as exc:
                return self._incomplete(result, items_key + ": " + str(exc))
            actual_offset = _nonnegative_int(page.get("startAt"))
            if actual_offset is None or actual_offset != offset:
                _add_items(result, rows, seen_items)
                return self._incomplete(result, items_key + ": missing or unexpected page offset.")
            added = _add_items(result, rows, seen_items)
            total = _nonnegative_int(page.get("total"))
            next_offset = offset + len(rows)
            if page.get("isLast") is True or (total is not None and next_offset >= total):
                return result
            if not rows or added == 0:
                return self._incomplete(result, items_key + ": pagination made no progress before completion.")
            # An explicit total/isLast is required to prove completion. A short page
            # is not proof: Jira may cap results below the requested maxResults.
            if total is None and page.get("isLast") is not False:
                return self._incomplete(result, items_key + ": no completion metadata was returned.")
            offset = next_offset
        return self._incomplete(result, items_key + ": pagination safety limit reached.")

    def get_comments(self, key: str) -> PageResult:
        return self._offset_pages("GET", "/issue/" + urllib.parse.quote(str(key), safe="") + "/comment", "comments")

    def get_worklogs(self, key: str) -> PageResult:
        return self._offset_pages("GET", "/issue/" + urllib.parse.quote(str(key), safe="") + "/worklog", "worklogs")

    def get_changelog(self, key: str) -> PageResult:
        path = "/issue/" + urllib.parse.quote(str(key), safe="")
        return self._offset_pages("GET", path + "/changelog", "values")

    @staticmethod
    def _incomplete(result: PageResult, gap: str) -> PageResult:
        result.complete = False
        result.gaps.append(gap)
        return result
