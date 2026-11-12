"""Offline transport/coverage tests; no Jira instance or network is used."""

import base64
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "jira-achievements" / "scripts"))
import jira_client
from jira_client import JiraClient, JiraError, PageResult


def response(data):
    return io.BytesIO(json.dumps(data).encode("utf-8"))


def http_error(status, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://secret.example/?token=DO_NOT_LEAK", status,
                                  "PRIVATE ERROR", headers, io.BytesIO(b"PRIVATE RESPONSE"))


class JiraClientTests(unittest.TestCase):
    def client(self, responses=(), **kwargs):
        client = JiraClient("https://team.atlassian.net", "PRIVATE_TOKEN",
                            email="user@example.com", **kwargs)
        client._opener = Mock()
        client._opener.open.side_effect = list(responses)
        return client

    def test_enhanced_search_uses_cursor_and_deduplicates_overlapping_pages(self):
        client = self.client([
            response({"issues": [{"id": "1"}, {"id": "2"}], "nextPageToken": "cursor-a", "isLast": False}),
            response({"issues": [{"id": "2"}, {"id": "3"}], "isLast": True}),
        ])
        result = client.search_issues('assignee = "user-id"', fields=["summary"])
        self.assertTrue(result.complete)
        self.assertEqual([item["id"] for item in result.items], ["1", "2", "3"])
        first, second = [call.args[0] for call in client._opener.open.call_args_list]
        self.assertEqual(first.full_url, "https://team.atlassian.net/rest/api/3/search/jql")
        self.assertEqual(first.method, "POST")
        self.assertNotIn("nextPageToken", json.loads(first.data))
        self.assertEqual(json.loads(second.data)["nextPageToken"], "cursor-a")
        self.assertEqual(json.loads(first.data)["fields"], ["summary"])

    def test_search_returns_partial_records_when_later_page_fails(self):
        client = self.client([
            response({"issues": [{"id": "1"}], "nextPageToken": "next", "isLast": False}),
            http_error(403),
        ])
        result = client.search_issues("reporter = currentUser()")
        self.assertEqual(result.items, [{"id": "1"}])
        self.assertFalse(result.complete)
        self.assertIn("HTTP 403", result.gaps[0])
        self.assertNotIn("PRIVATE", str(result))
        self.assertNotIn("secret.example", str(result))

    def test_search_repeating_cursor_is_incomplete_and_finite(self):
        client = self.client([
            response({"issues": [{"id": "1"}], "nextPageToken": "same", "isLast": False}),
            response({"issues": [{"id": "2"}], "nextPageToken": "same", "isLast": False}),
        ])
        result = client.search_issues("project = A")
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(client._opener.open.call_count, 2)

    def test_search_duplicate_only_page_does_not_loop_on_new_tokens(self):
        client = self.client([
            response({"issues": [{"id": "1"}], "nextPageToken": "a", "isLast": False}),
            response({"issues": [{"id": "1"}], "nextPageToken": "b", "isLast": False}),
        ])
        result = client.search_issues("project = A")
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 1)

    def test_search_requires_final_page_signal(self):
        client = self.client([response({"issues": []})])
        result = client.search_issues("project = A")
        self.assertFalse(result.complete)
        self.assertIn("signal", result.gaps[0])

    def test_search_empty_final_page_is_valid(self):
        client = self.client([response({"issues": [], "isLast": True})])
        self.assertEqual(client.search_issues("project = A"), PageResult())

    def test_comments_follow_actual_page_length_even_when_server_caps_page_size(self):
        client = self.client([
            response({"comments": [{"id": "1"}, {"id": "2"}], "startAt": 0, "maxResults": 2, "total": 3}),
            response({"comments": [{"id": "3"}], "startAt": 2, "maxResults": 2, "total": 3}),
        ])
        result = client.get_comments("APP-1")
        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 3)
        second = client._opener.open.call_args_list[1].args[0]
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(second.full_url).query)["startAt"], ["2"])

    def test_changelog_uses_dedicated_paginated_endpoint_not_issue_expansion(self):
        client = self.client([
            response({"values": [{"id": "h1"}], "startAt": 0, "total": 2, "isLast": False}),
            response({"values": [{"id": "h2"}], "startAt": 1, "total": 2, "isLast": True}),
        ])
        result = client.get_changelog("APP-2")
        self.assertTrue(result.complete)
        self.assertIn("/rest/api/3/issue/APP-2/changelog?", client._opener.open.call_args_list[0].args[0].full_url)

    def test_worklog_failure_is_missing_data_not_an_empty_complete_collection(self):
        client = self.client([http_error(404)])
        result = client.get_worklogs("APP-1")
        self.assertFalse(result.complete)
        self.assertEqual(result.items, [])
        self.assertIn("HTTP 404", result.gaps[0])

    def test_offset_page_without_completion_metadata_is_incomplete(self):
        client = self.client([response({"worklogs": [{"id": "1"}], "startAt": 0, "maxResults": 100})])
        result = client.get_worklogs("APP-1")
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 1)

    def test_empty_offset_page_before_total_does_not_claim_success(self):
        client = self.client([
            response({"comments": [{"id": "1"}], "startAt": 0, "total": 3}),
            response({"comments": [], "startAt": 1, "total": 3}),
        ])
        result = client.get_comments("APP-1")
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 1)

    def test_unexpected_offset_preserves_rows_but_marks_incomplete(self):
        client = self.client([
            response({"comments": [{"id": "1"}], "startAt": 0, "total": 3}),
            response({"comments": [{"id": "2"}], "startAt": 0, "total": 3}),
        ])
        result = client.get_comments("APP-1")
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 2)

    def test_invalid_page_structure_is_not_treated_as_no_results(self):
        client = self.client([response({"issues": ["unexpected string"], "isLast": True})])
        self.assertFalse(client.search_issues("project = A").complete)

    @patch("jira_client.time.sleep")
    def test_retry_after_is_capped_and_rate_limit_retry_is_finite(self, sleep):
        client = self.client([http_error(429, "999999"), http_error(503, "2"), response({"accountId": "me"})])
        self.assertEqual(client.current_user()["accountId"], "me")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30.0, 2.0])
        self.assertEqual(client._opener.open.call_count, 3)

    @patch("jira_client.time.sleep")
    def test_retry_exhaustion_does_not_leak_remote_error_content(self, sleep):
        client = self.client([http_error(500), http_error(500), http_error(500)])
        with self.assertRaises(JiraError) as error:
            client.current_user()
        self.assertEqual(error.exception.status, 500)
        self.assertEqual(client._opener.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("secret.example", str(error.exception))

    @patch("jira_client.time.sleep")
    def test_authentication_failure_is_not_retried(self, sleep):
        client = self.client([http_error(401)])
        with self.assertRaises(JiraError):
            client.current_user()
        self.assertEqual(client._opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_redirect_is_rejected_without_a_second_request(self):
        client = self.client([http_error(302)])
        with self.assertRaises(JiraError) as error:
            client.current_user()
        self.assertIn("Redirects are disabled", str(error.exception))
        self.assertEqual(client._opener.open.call_count, 1)
        self.assertIsNone(jira_client._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))

    def test_network_error_is_sanitized(self):
        client = self.client([urllib.error.URLError("PRIVATE_TOKEN https://secret.example")])
        with self.assertRaises(JiraError) as error:
            client.current_user()
        self.assertNotIn("PRIVATE_TOKEN", str(error.exception))
        self.assertNotIn("secret.example", str(error.exception))

    def test_scoped_cloud_api_base_and_basic_auth_are_used(self):
        client = self.client([response({"accountId": "me"})], api_url="https://api.atlassian.com/ex/jira/site-id")
        client.current_user()
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.atlassian.com/ex/jira/site-id/rest/api/3/myself")
        expected = "Basic " + base64.b64encode(b"user@example.com:PRIVATE_TOKEN").decode("ascii")
        self.assertEqual(request.get_header("Authorization"), expected)

    def test_issue_identifier_is_encoded_as_one_path_segment(self):
        client = self.client([response({"key": "APP-1"})])
        client.get_issue("../APP-1?secret=x")
        request = client._opener.open.call_args.args[0]
        self.assertIn("/issue/..%2FAPP-1%3Fsecret%3Dx?fields=", request.full_url)

    def test_unsafe_base_urls_and_unsupported_deployment_are_rejected(self):
        for url in ("http://team.test", "https://user:secret@team.test", "https://@team.test",
                    "https://team.test?secret=x", "https://team.test/#fragment",
                    "https://team.test/%2e%2e/path", "https://team.test/\npath"):
            with self.subTest(url=url), self.assertRaises(JiraError):
                JiraClient(url, "token", email="user@example.com")
        with self.assertRaises(JiraError):
            JiraClient("https://team.test", "token", email="user@example.com", deployment="data-center")

    def test_invalid_json_and_oversized_responses_fail_safely(self):
        client = self.client([io.BytesIO(b"PRIVATE invalid JSON")])
        with self.assertRaises(JiraError) as error:
            client.current_user()
        self.assertNotIn("PRIVATE", str(error.exception))
        client = self.client([response({"accountId": "very-long-id"})])
        with patch("jira_client.MAX_RESPONSE_BYTES", 4), self.assertRaises(JiraError):
            client.current_user()

    def test_pagination_safety_cap_is_reported(self):
        client = self.client([response({"issues": [{"id": "1"}], "nextPageToken": "next", "isLast": False})])
        with patch("jira_client.MAX_PAGES", 1):
            result = client.search_issues("project = A")
        self.assertFalse(result.complete)
        self.assertIn("safety limit", result.gaps[0])


if __name__ == "__main__":
    unittest.main()
