import copy
import importlib.util
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path


MODULE_PATH = (Path(__file__).resolve().parents[1] / "skills" /
               "jira-achievements" / "scripts" / "jira_evidence.py")
SPEC = importlib.util.spec_from_file_location("jira_evidence", MODULE_PATH)
evidence_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evidence_module
SPEC.loader.exec_module(evidence_module)
normalize_issue = evidence_module.normalize_issue


def dt(value):
    return datetime.fromisoformat(value)


START = dt("2026-07-01T00:00:00+09:00")
END = dt("2026-10-01T00:00:00+09:00")
OBSERVED = dt("2026-11-01T00:00:00+09:00")
ME = {"accountId": "me-id", "displayName": "Kim"}
OTHER = {"accountId": "other-id", "displayName": "Kim"}


def stream(items=(), complete=True, gaps=()):
    return {"items": list(items), "complete": complete, "gaps": list(gaps)}


def issue(assignee=ME, reporter=OTHER):
    return {"id": "1001", "key": "KPI-1", "fields": {
        "summary": "Improve workflow", "description": "Current scope",
        "assignee": copy.deepcopy(assignee), "reporter": copy.deepcopy(reporter),
        "creator": copy.deepcopy(OTHER),
        "created": "2026-06-01T00:00:00+09:00",
        "updated": "2026-10-20T00:00:00+09:00",
        "status": {"id": "3", "name": "In Progress"}, "resolution": None,
        "project": {"id": "10", "key": "KPI", "name": "KPI"},
        "issuetype": {"id": "100", "name": "Task"},
    }}


def change(identifier, at, field, old_id, new_id, old_name=None, new_name=None):
    return {"id": str(identifier), "created": at, "author": OTHER, "items": [{
        "field": field, "from": old_id, "to": new_id,
        "fromString": old_name, "toString": new_name,
    }]}


def normalize(record=None, comments=None, history=None, worklogs=None, **kwargs):
    return normalize_issue(record or issue(), comments or stream(), history or stream(),
                           worklogs or stream(), kwargs.get("target_user", ME),
                           kwargs.get("start", START), kwargs.get("end", END),
                           kwargs.get("observed_at", OBSERVED), "https://example.atlassian.net")


class JiraEvidenceTests(unittest.TestCase):
    def test_jira_compact_offsets_parse_on_python_39(self):
        cases = (
            ("2026-08-01T12:34:56.789+0900", "2026-08-01T12:34:56.789+09:00"),
            ("2026-08-01T03:34:56.789+0000", "2026-08-01T03:34:56.789+00:00"),
            ("2026-07-31T23:34:56.789-0400", "2026-07-31T23:34:56.789-04:00"),
            ("2026-08-01T03:34:56Z", "2026-08-01T03:34:56+00:00"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(evidence_module.parse_timestamp(raw), dt(expected))
        self.assertIsNone(evidence_module.parse_timestamp("2026-08-01T12:34:56.789"))
        self.assertIsNone(evidence_module.parse_timestamp("invalid+0900"))

    def test_former_assignee_is_included_and_display_names_are_not_identity(self):
        moved = change(5, "2026-09-15T10:00:00+09:00", "assignee", "me-id", "other-id")
        result = normalize(issue(OTHER), history=stream([moved]))
        roles = result["role_eligibility"]
        self.assertFalse(roles["current"]["assignee"])
        self.assertTrue(roles["during_period"]["assignee"])
        self.assertTrue(roles["eligible"])
        self.assertIn("KPI-1:history:5:0", roles["evidence_ids"])
        unrelated = normalize(issue(OTHER))
        self.assertFalse(unrelated["role_eligibility"]["eligible"])

    def test_reporter_is_only_candidate_scope_not_performance_attribution(self):
        result = normalize(issue(OTHER, ME))
        self.assertTrue(result["role_eligibility"]["current"]["reporter"])
        self.assertTrue(result["role_eligibility"]["eligible"])
        self.assertNotIn("achievements", result)
        self.assertIn("not proof", result["role_eligibility"]["meaning"])
        snapshot = result["evidence"][0]
        self.assertTrue(snapshot["context_only"])
        self.assertEqual(snapshot["timing"], "unknown")

    def test_issue_created_preserves_creator_and_time_without_current_description(self):
        record = issue(OTHER, ME)
        record["fields"]["creator"] = copy.deepcopy(ME)
        record["fields"]["created"] = "2026-08-02T10:00:00+09:00"
        record["fields"]["description"] = "Description revised after the period"
        result = normalize(record)
        created = next(item for item in result["evidence"] if item["type"] == "issue_created")
        self.assertEqual(created["evidence_id"], "KPI-1:issue:created")
        self.assertEqual(created["actor"]["account_id"], "me-id")
        self.assertEqual(created["occurred_at"], record["fields"]["created"])
        self.assertEqual(created["timing"], "in_period")
        self.assertIsNone(created["text"])
        self.assertNotIn("summary", created)
        self.assertFalse(created["context_only"])
        record["fields"]["description"] = "Another current description"
        again = normalize(record)
        self.assertEqual(created, next(item for item in again["evidence"]
                                       if item["type"] == "issue_created"))

    def test_issue_creator_is_not_replaced_by_reporter(self):
        result = normalize(issue(OTHER, ME))
        created = next(item for item in result["evidence"] if item["type"] == "issue_created")
        self.assertEqual(created["actor"]["account_id"], "other-id")

    def test_status_and_resolution_are_reconstructed_before_exact_cutoff(self):
        histories = [
            change(8, "2026-10-01T00:00:00+09:00", "status", "100", "3", "Done", "In Progress"),
            change(9, "2026-10-01T00:00:00+09:00", "resolution", "1", None, "Fixed", None),
        ]
        result = normalize(history=stream(histories))
        state = result["status_at_period_end"]
        self.assertEqual(state["confidence"], "verified")
        self.assertEqual(state["status"], {"id": "100", "name": "Done"})
        self.assertEqual(state["resolution"], {"id": "1", "name": "Fixed"})
        self.assertEqual(result["current"]["status"]["name"], "In Progress")
        for item in result["evidence"]:
            if item["type"] == "field_changed":
                self.assertEqual(item["timing"], "after_period")

    def test_role_loss_at_end_counts_but_gain_at_end_does_not(self):
        lost = normalize(issue(OTHER), history=stream([
            change(1, END.isoformat(), "assignee", "me-id", "other-id")]))
        gained = normalize(issue(ME), history=stream([
            change(1, END.isoformat(), "assignee", "other-id", "me-id")]))
        self.assertTrue(lost["role_eligibility"]["during_period"]["assignee"])
        self.assertFalse(gained["role_eligibility"]["during_period"]["assignee"])
        # A current role still makes the issue a candidate, as required by policy.
        self.assertTrue(gained["role_eligibility"]["eligible"])

    def test_role_loss_at_start_does_not_count(self):
        result = normalize(issue(OTHER), history=stream([
            change(1, START.isoformat(), "assignee", "me-id", "other-id")]))
        self.assertFalse(result["role_eligibility"]["during_period"]["assignee"])

    def test_partial_history_yields_unknown_not_a_negative(self):
        result = normalize(issue(OTHER), history=stream([], False, ["History truncated"]))
        self.assertIsNone(result["role_eligibility"]["during_period"]["assignee"])
        self.assertIsNone(result["role_eligibility"]["eligible"])
        self.assertEqual(result["status_at_period_end"]["confidence"], "unknown")
        self.assertIsNone(result["status_at_period_end"]["status"])
        self.assertFalse(result["coverage"]["changelog"]["complete"])
        self.assertIn("History truncated", result["gaps"])

    def test_current_snapshot_conflicting_with_history_is_not_trusted(self):
        result = normalize(issue(ME), history=stream([
            change(1, "2026-10-02T00:00:00+09:00", "assignee", "me-id", "other-id"),
            change(2, "2026-10-03T00:00:00+09:00", "status", "1", "2", "Open", "Closed")]))
        self.assertIsNone(result["role_eligibility"]["during_period"]["assignee"])
        self.assertEqual(result["status_at_period_end"]["confidence"], "unknown")

    def test_reopened_before_end_is_open_and_cancelled_is_not_called_achievement(self):
        reopened = normalize(history=stream([
            change(1, "2026-09-29T00:00:00+09:00", "status", "100", "3", "Done", "In Progress")]))
        self.assertEqual(reopened["status_at_period_end"]["status"]["name"], "In Progress")
        cancelled_issue = issue()
        cancelled_issue["fields"]["status"] = {"id": "100", "name": "Done"}
        cancelled_issue["fields"]["resolution"] = {"id": "3", "name": "Won't Do"}
        cancelled = normalize(cancelled_issue)
        self.assertEqual(cancelled["status_at_period_end"]["resolution"]["name"], "Won't Do")
        self.assertNotIn("completed", cancelled)

    def test_comment_edited_after_period_does_not_supply_retroactive_text(self):
        result = normalize(comments=stream([{
            "id": "11", "created": "2026-08-01T12:00:00+09:00",
            "updated": "2026-10-03T12:00:00+09:00",
            "author": ME, "updateAuthor": OTHER, "body": "Later claimed impact",
        }]))
        created = next(item for item in result["evidence"] if item["type"] == "comment_created")
        edited = next(item for item in result["evidence"] if item["type"] == "comment_edited")
        self.assertEqual(created["timing"], "in_period")
        self.assertIsNone(created["text"])
        self.assertEqual(created["text_as_of_period_end"], "unknown")
        self.assertIn("period_end_text_unverified", created["flags"])
        self.assertEqual(created["actor"]["account_id"], "me-id")
        self.assertEqual(edited["actor"]["account_id"], "other-id")
        self.assertEqual(edited["timing"], "after_period")
        self.assertEqual(edited["text"], "Later claimed impact")
        self.assertTrue(edited["context_only"])
        self.assertIn("retrospective_candidate", edited["flags"])
        self.assertIn("not_automatically_backdated", edited["flags"])

    def test_post_period_comment_can_be_inspected_for_explicit_retrospective_work(self):
        body = "On 2026-09-20 I completed the migration; recording this in October."
        result = normalize(comments=stream([{
            "id": "20", "created": "2026-10-03T12:00:00+09:00",
            "updated": "2026-10-03T12:00:00+09:00", "author": ME, "body": body,
        }]))
        created = next(item for item in result["evidence"] if item["type"] == "comment_created")
        self.assertEqual(created["text"], body)
        self.assertEqual(created["actor"]["account_id"], "me-id")
        self.assertEqual(created["timing"], "after_period")
        self.assertEqual(created["occurred_at"], "2026-10-03T12:00:00+09:00")
        self.assertTrue(created["context_only"])
        self.assertIn("current_version", created["flags"])
        self.assertIn("retrospective_candidate", created["flags"])
        self.assertEqual(created["text_as_of_period_end"], "unknown")

    def test_comment_missing_version_time_preserves_unattributed_current_snapshot(self):
        result = normalize(comments=stream([{
            "id": "21", "created": "2026-10-03T12:00:00+09:00",
            "author": ME, "body": "Current narrative without a version timestamp",
        }]))
        created = next(item for item in result["evidence"] if item["type"] == "comment_created")
        snapshot = next(item for item in result["evidence"] if item["type"] == "comment_snapshot")
        self.assertIsNone(created["text"])
        self.assertEqual(snapshot["text"], "Current narrative without a version timestamp")
        self.assertIsNone(snapshot["actor"]["account_id"])
        self.assertEqual(snapshot["original_author"]["account_id"], "me-id")
        self.assertEqual(snapshot["timing"], "unknown")
        self.assertTrue(snapshot["context_only"])

    def test_edit_inside_period_is_an_event_by_update_author(self):
        result = normalize(comments=stream([{
            "id": "12", "created": "2026-06-01T12:00:00+09:00",
            "updated": "2026-08-03T12:00:00+09:00",
            "author": OTHER, "updateAuthor": ME, "body": "Updated findings",
        }]))
        edited = next(item for item in result["evidence"] if item["type"] == "comment_edited")
        self.assertEqual(edited["text"], "Updated findings")
        self.assertEqual(edited["actor"]["account_id"], "me-id")
        self.assertEqual(edited["timing"], "in_period")
        self.assertIn("latest_edit_only", edited["flags"])

    def test_current_comment_wording_is_not_attributed_to_its_original_author(self):
        result = normalize(comments=stream([{
            "id": "13", "created": "2026-08-01T12:00:00+09:00",
            "updated": "2026-08-03T12:00:00+09:00",
            "author": ME, "updateAuthor": OTHER, "body": "Replaced by editor",
        }]))
        created = next(item for item in result["evidence"] if item["type"] == "comment_created")
        edited = next(item for item in result["evidence"] if item["type"] == "comment_edited")
        self.assertIsNone(created["text"])
        self.assertIn("text_is_edited_version_not_original", created["flags"])
        self.assertEqual(edited["text"], "Replaced by editor")
        self.assertEqual(edited["actor"]["account_id"], "other-id")

    def test_retrospective_worklog_keeps_work_and_recording_times_distinct(self):
        log = {"id": "7", "started": "2026-09-30T23:00:00+09:00",
               "created": "2026-10-10T12:00:00+09:00",
               "updated": "2026-10-10T12:00:00+09:00", "author": ME,
               "comment": "Late entry for analysis work", "timeSpentSeconds": 3600}
        result = normalize(worklogs=stream([log]))
        item = next(item for item in result["evidence"] if item["type"] == "worklog")
        self.assertEqual(item["timing"], "in_period")
        self.assertNotEqual(item["occurred_at"], item["recorded_at"])
        self.assertIn("recorded_after_period", item["flags"])
        self.assertIn("self_reported_work_time", item["flags"])
        self.assertEqual(item["reported_seconds"], 3600)
        self.assertEqual(item["text_as_of_period_end"], "unknown")
        self.assertEqual(item["text"], log["comment"])

    def test_later_created_issue_can_contain_retrospective_work(self):
        record = issue()
        record["fields"]["created"] = "2026-10-10T00:00:00+09:00"
        result = normalize(record, worklogs=stream([{
            "id": "7", "started": "2026-09-01T00:00:00+09:00",
            "created": "2026-10-10T00:00:00+09:00",
            "updated": "2026-10-10T00:00:00+09:00", "author": ME,
        }]))
        self.assertTrue(result["role_eligibility"]["eligible"])
        self.assertFalse(result["role_eligibility"]["during_period"]["assignee"])
        self.assertEqual(result["status_at_period_end"]["confidence"], "not_created")
        self.assertTrue(any(item["timing"] == "in_period" for item in result["evidence"]))

    def test_adf_preserves_links_mentions_code_and_text(self):
        adf = {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Design", "marks": [
                    {"type": "link", "attrs": {"href": "https://example.com/design"}}]},
                {"type": "hardBreak"},
                {"type": "mention", "attrs": {"id": "me-id", "text": "@Kim"}},
            ]},
            {"type": "codeBlock", "attrs": {"language": "python"}, "content": [
                {"type": "text", "text": "print('done')"}]},
            {"type": "inlineCard", "attrs": {"url": "https://example.com/result"}},
        ]}
        result = evidence_module.render_text(adf)
        self.assertIn("Design (https://example.com/design)", result)
        self.assertIn("@Kim [me-id]", result)
        self.assertIn("```python\nprint('done')\n```", result)
        self.assertIn("https://example.com/result", result)

    def test_adf_date_preserves_source_milliseconds_and_explicit_utc(self):
        body = {"type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "Migration finished on "},
            {"type": "date", "attrs": {"timestamp": "1789776000000"}},
        ]}]}
        result = normalize(comments=stream([{
            "id": "22", "created": "2026-10-03T12:00:00+09:00",
            "updated": "2026-10-03T12:00:00+09:00", "author": ME, "body": body,
        }]))
        created = next(item for item in result["evidence"] if item["type"] == "comment_created")
        self.assertIn("2026-09-19T00:00:00.000+00:00", created["text"])
        self.assertIn('timestamp_ms="1789776000000"', created["text"])
        self.assertIn("timezone=UTC", created["text"])
        self.assertEqual(created["timing"], "after_period")
        self.assertTrue(created["context_only"])
        self.assertIn("retrospective_candidate", created["flags"])
        # Milliseconds are not rounded through floating-point timestamps.
        precise = evidence_module.render_text({"type": "date", "attrs": {"timestamp": "1"}})
        self.assertIn("1970-01-01T00:00:00.001+00:00", precise)
        negative = evidence_module.render_text({"type": "date", "attrs": {"timestamp": "-1"}})
        self.assertIn("1969-12-31T23:59:59.999+00:00", negative)

    def test_adf_invalid_date_keeps_original_value_instead_of_inventing_a_date(self):
        for raw in ("not-a-date", "1.5", "9" * 50, None, True):
            with self.subTest(raw=raw):
                rendered = evidence_module.render_text({"type": "date", "attrs": {"timestamp": raw}})
                self.assertIn("invalid timestamp_ms=", rendered)
                self.assertIn(json.dumps(raw), rendered)
                self.assertNotIn("timezone=UTC", rendered)

    def test_timezone_offsets_compare_as_instants_and_naive_is_unknown(self):
        comments = [
            {"id": "1", "created": "2026-06-30T15:00:00Z", "updated": "2026-06-30T15:00:00Z",
             "author": ME, "body": "Exactly start in Seoul"},
            {"id": "2", "created": "2026-09-30T15:00:00+0000", "updated": "2026-09-30T15:00:00+0000",
             "author": ME, "body": "Exactly end in Seoul"},
            {"id": "3", "created": "2026-08-01T00:00:00", "author": ME, "body": "Unknown zone"},
        ]
        result = normalize(comments=stream(comments))
        by_id = {item["evidence_id"]: item for item in result["evidence"]}
        self.assertEqual(by_id["KPI-1:comment:1:created"]["timing"], "in_period")
        self.assertEqual(by_id["KPI-1:comment:2:created"]["timing"], "after_period")
        self.assertEqual(by_id["KPI-1:comment:3:created"]["timing"], "unknown")
        self.assertTrue(any("unzoned" in gap for gap in result["gaps"]))

    def test_serializable_stable_ids_and_private_email_is_not_exported(self):
        private_user = dict(ME, emailAddress="private@example.com")
        result = normalize(issue(private_user), target_user=private_user)
        self.assertNotIn("private@example.com", json.dumps(result))
        again = normalize(issue(private_user), target_user=private_user)
        self.assertEqual([item["evidence_id"] for item in result["evidence"]],
                         [item["evidence_id"] for item in again["evidence"]])

    def test_incomplete_or_future_observation_does_not_extrapolate_status(self):
        result = normalize(observed_at=dt("2026-09-15T00:00:00+09:00"))
        self.assertEqual(result["status_at_period_end"]["confidence"], "unknown")
        self.assertTrue(result["role_eligibility"]["during_period"]["assignee"])
        with self.assertRaises(ValueError):
            normalize(start=dt("2026-07-01T00:00:00"))


if __name__ == "__main__":
    unittest.main()
