"""Offline workflow tests with isolated state directories and fake Jira records."""

import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "jira-achievements" / "scripts"))
import jira_achievements as cli
from jira_client import JiraError, PageResult


ME = {"accountId": "me-id", "displayName": "Example User", "timeZone": "Asia/Seoul",
      "emailAddress": "private-subject@example.com"}
OTHER = {"accountId": "other-id", "displayName": "Colleague"}


def issue(key="APP-1"):
    return {"id": key.split("-")[-1], "key": key, "fields": {
        "summary": "Review integration", "description": "Documented review work",
        "assignee": copy.deepcopy(ME), "reporter": copy.deepcopy(OTHER),
        "creator": copy.deepcopy(OTHER),
        "created": "2026-06-01T00:00:00+09:00", "updated": "2026-10-20T00:00:00+09:00",
        "status": {"id": "3", "name": "In Progress"}, "resolution": None,
        "project": {"id": "10", "key": "APP", "name": "Application"},
        "issuetype": {"id": "100", "name": "Task"},
    }}


def comment(identifier="c1"):
    return {"id": identifier, "created": "2026-08-01T10:00:00+09:00",
            "updated": "2026-08-01T10:00:00+09:00", "author": copy.deepcopy(ME),
            "body": "Completed the integration review and documented findings."}


class FakeJira:
    """The client contract without HTTP or external state."""

    def __init__(self, searches=None, issues=None, comments=None, changelogs=None,
                 worklogs=None, failed_keys=()):
        self.searches = list(searches if searches is not None else [
            PageResult([{"id": "1", "key": "APP-1"}]), PageResult(), PageResult()])
        self.issues = issues or {"APP-1": issue()}
        self.comments = comments or {}
        self.changelogs = changelogs or {}
        self.worklogs = worklogs or {}
        self.failed_keys = set(failed_keys)
        self.search_calls = []
        self.issue_calls = []

    def current_user(self):
        return copy.deepcopy(ME)

    def search_issues(self, jql, fields=None):
        self.search_calls.append({"jql": jql, "fields": fields})
        return copy.deepcopy(self.searches[len(self.search_calls) - 1])

    def get_issue(self, key):
        self.issue_calls.append(key)
        if key in self.failed_keys:
            raise JiraError("Jira request failed (HTTP 403).", 403)
        return copy.deepcopy(self.issues[key])

    def get_comments(self, key):
        return copy.deepcopy(self.comments.get(key, PageResult()))

    def get_changelog(self, key):
        return copy.deepcopy(self.changelogs.get(key, PageResult()))

    def get_worklogs(self, key):
        return copy.deepcopy(self.worklogs.get(key, PageResult()))


class JiraCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {"JIRA_ACHIEVEMENTS_HOME": str(self.root / "state")}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {"site_url": "https://team.atlassian.net", "email": "config-secret@example.com",
                       "api_token": "DO_NOT_EXPORT_THIS_TOKEN", "timezone": "Asia/Seoul", "projects": []}
        self.start, self.end = cli.period_bounds("2026-07-01", "2026-09-30", "Asia/Seoul")

    def collect(self, client=None, **kwargs):
        output = kwargs.pop("output", self.root / "run")
        with contextlib.redirect_stderr(io.StringIO()):
            manifest = cli.collect_run(client or FakeJira(), self.config, self.start, self.end,
                                       output, **kwargs)
        return output, manifest

    def records(self, output):
        return [json.loads(line) for line in (output / "issues.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_collect_counts_and_query_union_do_not_fetch_duplicates(self):
        fake = FakeJira(searches=[
            PageResult([{"id": "1", "key": "APP-1"}, {"id": "2", "key": "APP-2"}]),
            PageResult([{"id": "1", "key": "APP-1"}]),
            PageResult([{"id": "2", "key": "APP-2"}]),
        ], issues={"APP-1": issue("APP-1"), "APP-2": issue("APP-2")},
            comments={"APP-1": PageResult([comment()])})
        output, manifest = self.collect(fake)
        self.assertEqual(fake.issue_calls, ["APP-1", "APP-2"])
        self.assertEqual(len(fake.search_calls), 3)
        self.assertEqual(manifest["counts"]["candidate_issues"], 2)
        self.assertEqual(manifest["counts"]["normalized_issues"], 2)
        self.assertEqual(manifest["counts"]["failed_issues"], 0)
        self.assertEqual(manifest["counts"]["unprocessed_issues"], 0)
        self.assertEqual(manifest["coverage"]["status"], "complete")
        self.assertEqual({record["key"] for record in self.records(output)}, {"APP-1", "APP-2"})
        self.assertEqual(json.loads((output / "manifest.json").read_text(encoding="utf-8")), manifest)
        raw = [json.loads(line) for line in (output / "raw/issues.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(raw), 2)

    def test_partial_history_query_preserves_candidates_and_records_gap(self):
        fake = FakeJira(searches=[PageResult(),
            PageResult([{"key": "APP-1", "id": "1"}], False, ["pagination interrupted"]), PageResult()])
        output, manifest = self.collect(fake)
        self.assertEqual(manifest["coverage"]["status"], "partial")
        self.assertFalse(manifest["queries"][1]["complete"])
        self.assertTrue(manifest["coverage"]["gaps"])
        self.assertEqual(manifest["counts"]["normalized_issues"], 1)
        self.assertEqual(self.records(output)[0]["key"], "APP-1")

    def test_partial_stream_preserves_available_evidence(self):
        fake = FakeJira(comments={"APP-1": PageResult([comment()], False, ["later page unavailable"])})
        output, manifest = self.collect(fake)
        self.assertEqual(manifest["coverage"]["status"], "partial")
        record = self.records(output)[0]
        self.assertFalse(record["coverage"]["comments"]["complete"])
        self.assertTrue(any(e["type"] == "comment_created" for e in record["evidence"]))

    def test_fetch_failure_is_counted_and_does_not_claim_complete(self):
        fake = FakeJira(searches=[PageResult([{"key": "APP-1"}, {"key": "APP-2"}]), PageResult(), PageResult()],
                        failed_keys={"APP-2"})
        output, manifest = self.collect(fake)
        self.assertEqual(manifest["coverage"]["status"], "partial")
        self.assertEqual(manifest["counts"]["candidate_issues"], 2)
        self.assertEqual(manifest["counts"]["normalized_issues"], 1)
        self.assertEqual(manifest["counts"]["failed_issues"], 1)
        self.assertEqual(manifest["failures"][0]["key"], "APP-2")
        self.assertEqual(len(self.records(output)), 1)

    def test_zero_results_is_a_complete_empty_package_not_fabricated_work(self):
        output, manifest = self.collect(FakeJira(searches=[PageResult(), PageResult(), PageResult()]))
        self.assertEqual(manifest["coverage"]["status"], "complete")
        self.assertEqual(manifest["counts"]["candidate_issues"], 0)
        self.assertEqual(self.records(output), [])
        self.assertEqual(json.loads((output / "analysis-index.json").read_text())["batches"], [])

    def test_search_row_without_key_marks_coverage_incomplete(self):
        _, manifest = self.collect(FakeJira(searches=[PageResult([{"id": "1"}]), PageResult(), PageResult()]))
        self.assertEqual(manifest["coverage"]["status"], "partial")
        self.assertEqual(manifest["counts"]["normalized_issues"], 0)

    def test_period_is_inclusive_dates_with_exclusive_next_midnight(self):
        self.assertEqual(self.start.isoformat(), "2026-07-01T00:00:00+09:00")
        self.assertEqual(self.end.isoformat(), "2026-10-01T00:00:00+09:00")
        start, end = cli.period_bounds("2026-11-11", "2026-11-11", "Asia/Seoul")
        self.assertEqual(end - start, dt.timedelta(days=1))
        with self.assertRaises(ValueError):
            cli.period_bounds("2026-09-30", "2026-07-01", "Asia/Seoul")

    def test_period_handles_dst_as_local_calendar_days(self):
        start, end = cli.period_bounds("2026-03-08", "2026-03-08", "America/New_York")
        self.assertEqual((end.astimezone(dt.timezone.utc) - start.astimezone(dt.timezone.utc)).total_seconds(), 23 * 3600)

    def test_queries_preserve_old_and_retrospectively_recorded_work(self):
        queries = cli.candidate_queries(ME, self.start, self.end, ["APP", "OPS"])
        self.assertEqual({q["purpose"] for q in queries}, {"current_roles", "historical_assignee", "historical_reporter"})
        for query in queries:
            jql = query["jql"].lower()
            self.assertNotIn("updated", jql)
            self.assertNotIn("created", jql)
            self.assertIn("project in", jql)
            self.assertIn('"me-id"', jql)
            self.assertNotIn("currentuser()", jql)
        self.assertIn("assignee was", queries[1]["jql"].lower())
        self.assertIn("reporter was", queries[2]["jql"].lower())

    def test_jql_values_are_escaped_and_control_characters_rejected(self):
        escaped = cli.jql_string('value" OR key = "x\\y')
        self.assertEqual(escaped, '"value\\" OR key = \\"x\\\\y"')
        with self.assertRaises(ValueError):
            cli.jql_string("value\nOR project = OTHER")

    def test_environment_token_is_read_without_creating_a_config_file(self):
        with patch.dict(os.environ, {"JIRA_URL": "https://team.atlassian.net", "JIRA_EMAIL": "user@example.com",
                                     "JIRA_API_TOKEN": "ENV_PRIVATE_TOKEN"}):
            config = cli.load_config()
        self.assertEqual(config["api_token"], "ENV_PRIVATE_TOKEN")
        self.assertEqual(config["timezone"], "Asia/Seoul")
        self.assertFalse(cli.paths()["config"].exists())

    def test_environment_token_overrides_saved_token_without_rewriting_it(self):
        cli.dump_json(cli.paths()["config"], self.config)
        with patch.dict(os.environ, {"JIRA_API_TOKEN": "NEW_ENV_TOKEN"}):
            self.assertEqual(cli.load_config()["api_token"], "NEW_ENV_TOKEN")
        self.assertEqual(json.loads(cli.paths()["config"].read_text())["api_token"], self.config["api_token"])

    def test_credentials_and_email_are_not_exported_to_manifest_or_normalized_data(self):
        output, manifest = self.collect()
        exported = (output / "manifest.json").read_text(encoding="utf-8") + (output / "issues.jsonl").read_text(encoding="utf-8")
        for secret in (self.config["api_token"], self.config["email"], ME["emailAddress"]):
            self.assertNotIn(secret, exported)
        self.assertNotIn("api_token", manifest)
        self.assertNotIn("emailAddress", manifest["subject"])
        self.assertEqual(manifest["subject"]["accountId"], ME["accountId"])

    def test_ensure_style_preserves_user_changes_across_calls(self):
        style = cli.ensure_style()
        self.assertTrue(style.is_file())
        style.write_text("사용자가 수정한 성과 표현 기준\n", encoding="utf-8")
        self.assertEqual(cli.ensure_style().read_text(encoding="utf-8"), "사용자가 수정한 성과 표현 기준\n")

    def test_run_uses_an_immutable_style_snapshot(self):
        style = self.root / "custom-style.md"
        style.write_text("Report as short factual bullets.\n", encoding="utf-8")
        output, manifest = self.collect(style_path=style)
        original_hash = manifest["style"]["sha256"]
        style.write_text("Changed later.\n", encoding="utf-8")
        self.assertEqual((output / "achievement-style.md").read_text(), "Report as short factual bullets.\n")
        self.assertEqual(len(original_hash), 64)

    def test_install_is_idempotent_but_preserves_existing_directories(self):
        destination = self.root / "installed" / "jira-achievements"
        self.assertEqual(cli.install(destination), destination)
        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.resolve(), cli.SKILL_ROOT)
        self.assertEqual(cli.install(destination), destination)
        occupied = self.root / "existing"
        occupied.mkdir()
        marker = occupied / "user-file"
        marker.write_text("preserve me")
        with self.assertRaises(ValueError):
            cli.install(occupied)
        self.assertEqual(marker.read_text(), "preserve me")

    def test_default_install_targets_claude_code_under_user_home(self):
        isolated_home = self.root / "fake-home"
        with patch.object(Path, "home", return_value=isolated_home):
            destination = cli.install()
            repeated = cli.install()
        expected = isolated_home / ".claude" / "skills" / "jira-achievements"
        self.assertEqual(destination, expected)
        self.assertEqual(repeated, expected)
        self.assertTrue(expected.is_symlink())
        self.assertEqual(expected.resolve(), cli.SKILL_ROOT)
        self.assertFalse((isolated_home / ".codex").exists())

    def test_claude_config_dir_controls_default_install_destination(self):
        isolated_home = self.root / "fake-home"
        claude_config = self.root / "custom-claude-config"
        with patch.object(Path, "home", return_value=isolated_home), \
                patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_config)}):
            destination = cli.install()
        expected = claude_config / "skills" / "jira-achievements"
        self.assertEqual(destination, expected)
        self.assertTrue(expected.is_symlink())
        self.assertEqual(expected.resolve(), cli.SKILL_ROOT)
        self.assertFalse((isolated_home / ".claude").exists())

    def test_codex_home_does_not_redirect_claude_code_install(self):
        isolated_home = self.root / "fake-home"
        codex_config = self.root / "unrelated-codex-config"
        with patch.object(Path, "home", return_value=isolated_home), \
                patch.dict(os.environ, {"CODEX_HOME": str(codex_config)}):
            destination = cli.install()
        self.assertEqual(destination, isolated_home / ".claude" / "skills" / "jira-achievements")
        self.assertTrue(destination.is_symlink())
        self.assertFalse(codex_config.exists())

    def test_collect_does_not_overwrite_existing_output(self):
        output = self.root / "existing-run"
        output.mkdir()
        marker = output / "original"
        marker.write_text("keep")
        with self.assertRaises(ValueError):
            self.collect(output=output)
        self.assertEqual(marker.read_text(), "keep")

    def test_fragmentation_preserves_every_evidence_record_and_flags_oversized_piece(self):
        records = [{"key": "APP-1", "summary": "A", "evidence": [
            {"evidence_id": "e1", "text": "small"},
            {"evidence_id": "e2", "text": "한글" * 1000},
            {"evidence_id": "e3", "text": "small end"},
        ]}, {"key": "APP-2", "summary": "B", "evidence": [{"evidence_id": "e4", "text": "other issue"}]}]
        output = self.root / "batches"
        index = cli.batch_analysis(records, output, max_bytes=600)
        recovered = []
        parts = []
        for batch in index["batches"]:
            payload = json.loads((output / batch["path"]).read_text(encoding="utf-8"))
            parts.extend(payload["issues"])
            recovered.extend(e for part in payload["issues"] for e in part["evidence"])
            self.assertEqual(batch["bytes"], len((output / batch["path"]).read_bytes()))
        expected = [e for record in records for e in record["evidence"]]
        self.assertEqual(recovered, expected)
        self.assertEqual(index["unique_issues"], 2)
        self.assertEqual(index["unique_evidence"], 4)
        self.assertGreater(index["fragments"], 2)
        self.assertTrue(any(b["oversized"] and "e2" in b["evidence_ids"] for b in index["batches"]))
        app_parts = [p for p in parts if p["key"] == "APP-1"]
        self.assertEqual([p["fragment_index"] for p in app_parts], list(range(1, len(app_parts) + 1)))
        self.assertTrue(all(p["fragment_count"] == len(app_parts) for p in app_parts))


if __name__ == "__main__":
    unittest.main()
