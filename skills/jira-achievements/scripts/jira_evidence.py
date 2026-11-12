"""Normalize Jira records without inferring personal contributions or achievements.

All decisions here concern source identity, timestamps, role intervals, and field
history. Text remains untrusted source material for the separate analysis prompt.
The caller keeps the complete API responses in its raw archive.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote


STATE_FIELDS = {"assignee", "reporter", "status", "resolution"}


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Accept offset-aware Jira/ISO timestamps; never guess a missing timezone."""
    if not isinstance(value, str) or not value:
        return None
    # Jira commonly emits +0000/+0900. Python 3.9's fromisoformat requires
    # the colon form (+00:00/+09:00), unlike newer Python versions.
    canonical = value[:-1] + "+00:00" if value.endswith("Z") else value
    canonical = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", canonical)
    try:
        result = datetime.fromisoformat(canonical)
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _actor(value: Any) -> Dict[str, Optional[str]]:
    value = value if isinstance(value, dict) else {}
    return {
        "account_id": value.get("accountId"),
        "username": value.get("name"),
        "key": value.get("key"),
        "display_name": value.get("displayName"),
    }


def _aliases(value: Any) -> Set[str]:
    if not isinstance(value, dict):
        return set()
    return {
        str(value[key])
        for key in ("accountId", "name", "key", "account_id", "username")
        if value.get(key) is not None and str(value[key])
    }


def _named(value: Any) -> Optional[Dict[str, Optional[str]]]:
    if not isinstance(value, dict):
        return None
    return {"id": str(value["id"]) if value.get("id") is not None else None,
            "name": value.get("name")}


def render_text(value: Any) -> str:
    """Render ADF while retaining link targets, mentions, code, and line breaks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(render_text(child) for child in value)
    if not isinstance(value, dict):
        return str(value)
    kind = value.get("type", "")
    attrs = value.get("attrs") or {}
    if kind == "text":
        text = str(value.get("text", ""))
        for mark in value.get("marks") or []:
            href = (mark.get("attrs") or {}).get("href")
            if mark.get("type") == "link" and href and href != text:
                text += " (" + str(href) + ")"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "date":
        # https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/date/
        # Preserve the source value as well as a timezone-explicit rendering;
        # changing the machine locale must not change an activity date in evidence.
        timestamp = attrs.get("timestamp")
        source = json.dumps(timestamp, ensure_ascii=False, sort_keys=True)
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (str, int))
                or not re.fullmatch(r"[+-]?[0-9]+", str(timestamp))):
            return "[ADF date: invalid timestamp_ms=" + source + "]"
        try:
            date = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(timestamp))
        except (ValueError, OverflowError):
            return "[ADF date: invalid timestamp_ms=" + source + "]"
        return (date.isoformat(timespec="milliseconds")
                + " [ADF date; timezone=UTC; timestamp_ms=" + source + "]")
    if kind in {"inlineCard", "blockCard", "embedCard"}:
        return str(attrs.get("url", ""))
    if kind == "mention":
        name = attrs.get("text") or attrs.get("displayName") or "mention"
        identifier = attrs.get("id") or attrs.get("accountId")
        return str(name) + (" [" + str(identifier) + "]" if identifier else "")
    if kind == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    content = render_text(value.get("content") or [])
    if kind == "codeBlock":
        return "```" + str(attrs.get("language", "")) + "\n" + content + "\n```\n"
    if kind == "listItem":
        return "- " + content.rstrip("\n") + "\n"
    if kind in {"paragraph", "heading", "tableRow", "blockquote", "rule"}:
        return content + "\n"
    if content:
        return content
    # ADF extensions/media can carry a useful label without ordinary text nodes.
    for key in ("alt", "text", "title", "url"):
        if attrs.get(key):
            return str(attrs[key])
    return "[" + str(kind) + "]" if kind not in {"doc", ""} else ""


def _timing(value: Optional[datetime], start: datetime, end: datetime) -> str:
    if value is None:
        return "unknown"
    if value < start:
        return "before_period"
    if value >= end:
        return "after_period"
    return "in_period"


def _stream(value: Any, label: str) -> Tuple[List[dict], dict]:
    if not isinstance(value, dict):
        return [], {"complete": False, "item_count": 0,
                    "gaps": [label + ": stream unavailable"]}
    gaps = [str(gap) for gap in value.get("gaps", [])]
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        return [], {"complete": False, "item_count": 0,
                    "gaps": gaps + [label + ": invalid items collection"]}
    items = [item for item in raw_items if isinstance(item, dict)]
    if len(items) != len(raw_items):
        gaps.append(label + ": non-object records omitted")
    return items, {"complete": value.get("complete") is True and not gaps,
                   "item_count": len(items), "gaps": gaps}


def _id(value: dict) -> Tuple[str, List[str]]:
    if value.get("id") is not None:
        return str(value["id"]), []
    return "hash-" + _hash(value), ["source_id_missing"]


def _value(identifier: Any, name: Any = None) -> Optional[dict]:
    if identifier in (None, "") and name in (None, ""):
        return None
    return {"id": str(identifier) if identifier not in (None, "") else None,
            "name": str(name) if name not in (None, "") else None}


def _same_value(left: Optional[dict], right: Optional[dict], field: str) -> bool:
    if left is None or right is None:
        return left is right
    left_ids = set(left.get("aliases") or [])
    right_ids = set(right.get("aliases") or [])
    if left.get("id") is not None:
        left_ids.add(str(left["id"]))
    if right.get("id") is not None:
        right_ids.add(str(right["id"]))
    if left_ids and right_ids:
        return bool(left_ids & right_ids)
    # Display names are never sufficient identity for assignees/reporters.
    return field in {"status", "resolution"} and left.get("name") == right.get("name")


def _current_value(fields: dict, field: str) -> Optional[dict]:
    raw = fields.get(field)
    if field in {"assignee", "reporter"}:
        if raw is None:
            return None
        aliases = _aliases(raw)
        return {"id": next(iter(sorted(aliases)), None),
                "name": raw.get("displayName") if isinstance(raw, dict) else None,
                "aliases": sorted(aliases)}
    return _named(raw)


def _history_order(change: dict) -> tuple:
    source_id = change["history_id"]
    order = (0, int(source_id)) if source_id.isdigit() else (1, source_id)
    return (change["at"], order, change["item_index"])


def _reconstruct(
    fields: dict, changes: List[dict], history_complete: bool,
    created: Optional[datetime], observed: datetime, start: datetime, end: datetime,
    target: Set[str], snapshot_id: str,
) -> Tuple[dict, dict, List[str]]:
    current_roles = {
        field: bool(_aliases(fields.get(field)) & target) if field in fields else None
        for field in ("assignee", "reporter")
    }
    during: Dict[str, Optional[bool]] = {}
    role_sources = [snapshot_id]
    gaps = []
    status_at_end = {"status": None, "resolution": None, "confidence": "unknown",
                     "evidence_ids": [], "as_of_exclusive": end.isoformat()}
    if created is not None and created >= end:
        during = {"assignee": False, "reporter": False}
        status_at_end["confidence"] = "not_created"
        status_at_end["evidence_ids"] = [snapshot_id]
    else:
        for field in ("assignee", "reporter"):
            relevant = [item for item in changes if item["field"] == field]
            usable = [item for item in relevant if item["at"] is not None]
            usable.sort(key=_history_order, reverse=True)
            complete = (history_complete and field in fields and created is not None
                        and len(usable) == len(relevant) and observed >= end)
            value = _current_value(fields, field)
            right = observed
            found = False
            field_ids = []
            for change in usable:
                when = change["at"]
                if when > observed or (created is not None and when < created):
                    complete = False
                low = max(when, start, created) if created else max(when, start)
                high = min(right, end)
                if low < high and value is not None:
                    aliases = set(value.get("aliases") or []) | {value.get("id")}
                    if target & aliases:
                        found = True
                        field_ids.append(change["evidence_id"])
                if not _same_value(value, change["to"], field):
                    complete = False
                value = change["from"]
                right = when
            low = max(created, start) if created else start
            if low < min(right, end) and value is not None:
                aliases = set(value.get("aliases") or []) | {value.get("id")}
                if target & aliases:
                    found = True
                    field_ids.extend(change["evidence_id"] for change in usable)
            # An observed matching current value is proof at that instant, even
            # when older history is missing. Other incomplete intervals are unknown.
            observed_match = start <= observed < end and current_roles[field] is True
            during[field] = found if complete else (True if observed_match else None)
            if during[field] is True:
                role_sources.extend(field_ids)
            if not complete and not observed_match:
                gaps.append(field + ": historical period membership not fully reconstructable")

        state_complete = history_complete and observed >= end and created is not None
        state_ids = [snapshot_id]
        for field in ("status", "resolution"):
            if field not in fields:
                state_complete = False
            value = _current_value(fields, field)
            relevant = [item for item in changes if item["field"] == field]
            if any(item["at"] is None for item in relevant):
                state_complete = False
            later = [item for item in relevant if item["at"] is not None and item["at"] >= end]
            later.sort(key=_history_order, reverse=True)
            for change in later:
                if change["at"] > observed or not _same_value(value, change["to"], field):
                    state_complete = False
                value = change["from"]
                state_ids.append(change["evidence_id"])
            status_at_end[field] = value
        if state_complete and status_at_end["status"] is not None:
            status_at_end["confidence"] = "verified"
            status_at_end["evidence_ids"] = list(dict.fromkeys(state_ids))
        else:
            status_at_end["status"] = None
            status_at_end["resolution"] = None
            gaps.append("status: period-end state not fully reconstructable")
    known = list(current_roles.values()) + list(during.values())
    eligible = True if True in known else (None if None in known else False)
    roles = {"current": current_roles, "during_period": during,
             "eligible": eligible, "evidence_ids": list(dict.fromkeys(role_sources)),
             "meaning": "candidate scope only; not proof of personal contribution"}
    return roles, status_at_end, gaps


def normalize_issue(
    issue: dict, comments: dict, changelog: dict, worklogs: dict,
    target_user: dict, start: datetime, end: datetime, observed_at: datetime,
    site_url: str,
) -> dict:
    """Return one serializable evidence bundle for the half-open interval [start,end)."""
    for name, value in (("start", start), ("end", end), ("observed_at", observed_at)):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(name + " must be an offset-aware datetime")
    if start >= end:
        raise ValueError("start must precede end")
    target = _aliases(target_user)
    if not target:
        raise ValueError("target_user must contain accountId, name, or key")
    key = str(issue.get("key") or issue.get("id") or "unknown-issue")
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    issue_url = site_url.rstrip("/") + "/browse/" + quote(key, safe="")
    created = parse_timestamp(fields.get("created"))
    updated = parse_timestamp(fields.get("updated"))
    comment_items, comment_coverage = _stream(comments, "comments")
    history_items, history_coverage = _stream(changelog, "changelog")
    worklog_items, worklog_coverage = _stream(worklogs, "worklogs")
    coverage = {"comments": comment_coverage, "changelog": history_coverage,
                "worklogs": worklog_coverage}
    gaps = [gap for section in coverage.values() for gap in section["gaps"]]
    evidence: List[dict] = []

    def append(eid: str, kind: str, actor: Any, at: Optional[datetime],
               recorded: Optional[datetime], modified: Optional[datetime],
               text: Optional[str], flags: List[str], source: str,
               context_only: bool = False, **extra: Any) -> dict:
        item = {"evidence_id": eid, "type": kind, "actor": _actor(actor),
                "occurred_at": _iso(at), "recorded_at": _iso(recorded),
                "updated_at": _iso(modified), "observed_at": observed_at.isoformat(),
                "timing": _timing(at, start, end), "source_url": source,
                "text": text, "flags": list(dict.fromkeys(flags)),
                "context_only": context_only}
        item.update(extra)
        evidence.append(item)
        return item

    snapshot_id = key + ":snapshot:" + _hash(fields)
    append(snapshot_id, "issue_snapshot", None, None, created, updated,
           render_text(fields.get("description")).strip(),
           ["current_snapshot", "not_proof_of_period_activity",
            "updated_at_is_issue_update_not_description_version"],
           issue_url, context_only=True, summary=fields.get("summary"),
           text_as_of_period_end="unknown")
    append(key + ":issue:created", "issue_created", fields.get("creator"),
           created, created, None, None,
           ["creation_metadata_only", "not_proof_of_issue_execution"]
           + ([] if _aliases(fields.get("creator")) else ["creator_unknown"]),
           issue_url, context_only=_timing(created, start, end) != "in_period",
           source_fields=["fields.created", "fields.creator"])

    for comment in comment_items:
        cid, flags = _id(comment)
        at = parse_timestamp(comment.get("created"))
        modified = parse_timestamp(comment.get("updated"))
        text = render_text(comment.get("body")).strip()
        source = issue_url + "?focusedCommentId=" + quote(cid, safe="")
        current_hash = _hash(comment.get("body"))
        creation_timing = _timing(at, start, end)
        creation_flags = flags[:]
        unedited = at is not None and modified == at
        creation_text: Optional[str] = (
            text if unedited and creation_timing in {"in_period", "after_period"} else None)
        version_known = at is not None and modified is not None and at <= modified < end
        if not version_known:
            creation_flags.append("period_end_text_unverified")
        if modified is not None and at is not None and modified > at:
            creation_flags.append("text_is_edited_version_not_original")
            # The edit may have a different author. Do not attach the latest
            # wording to the original author's creation event.
            creation_text = None
        if creation_text is not None:
            creation_flags.append("current_version")
            if creation_timing == "after_period":
                creation_flags.extend(["retrospective_candidate", "not_automatically_backdated"])
        if creation_text is None:
            creation_flags.append("text_omitted_use_raw_archive_if_needed")
        append(key + ":comment:" + cid + ":created", "comment_created",
               comment.get("author"), at, at, modified, creation_text,
               creation_flags, source, context_only=creation_timing != "in_period",
               current_text_hash=current_hash,
               text_as_of_period_end="known" if version_known else "unknown")
        if modified is not None and (at is None or modified > at):
            edit_flags = flags[:] + ["latest_edit_only", "current_version"]
            if not comment.get("updateAuthor"):
                edit_flags.append("edit_actor_unknown")
            edit_timing = _timing(modified, start, end)
            edit_text = text if edit_timing in {"in_period", "after_period"} else None
            if edit_timing == "after_period":
                edit_flags.extend(["retrospective_candidate", "not_automatically_backdated",
                                   "period_end_text_unverified"])
            if edit_text is None:
                edit_flags.append("text_omitted_outside_period")
            append(key + ":comment:" + cid + ":edit:" + _hash(comment.get("updated")),
                   "comment_edited", comment.get("updateAuthor"), modified, at, modified,
                   edit_text, edit_flags, source,
                   context_only=edit_timing != "in_period",
                   current_text_hash=current_hash,
                   text_as_of_period_end="known" if modified < end else "unknown")
        elif modified is None or (at is not None and modified < at):
            # A missing/inconsistent update time does not establish authorship
            # of the current wording. Preserve it as an unattributed snapshot.
            append(key + ":comment:" + cid + ":snapshot:" + current_hash,
                   "comment_snapshot", None, None, at, modified, text,
                   flags + ["current_snapshot", "current_version", "text_version_time_unknown",
                            "retrospective_candidate", "not_automatically_backdated",
                            "period_end_text_unverified"],
                   source, context_only=True, original_author=_actor(comment.get("author")),
                   current_text_hash=current_hash, text_as_of_period_end="unknown")
        if at is None:
            gaps.append("comment " + cid + ": missing or unzoned creation timestamp")
        if modified is None or (modified >= end and (at is None or at < end)):
            gaps.append("comment " + cid + ": period-end text version unavailable")

    changes = []
    for history in history_items:
        hid, flags = _id(history)
        at = parse_timestamp(history.get("created"))
        items = history.get("items")
        if not isinstance(items, list):
            history_coverage["complete"] = False
            gaps.append("history " + hid + ": missing change items")
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                history_coverage["complete"] = False
                gaps.append("history " + hid + ": invalid change item")
                continue
            field = str(item.get("fieldId") or item.get("field") or "unknown")
            if field.lower() in STATE_FIELDS:
                field = field.lower()
            eid = key + ":history:" + hid + ":" + str(index)
            before = _value(item.get("from"), item.get("fromString"))
            after = _value(item.get("to"), item.get("toString"))
            changes.append({"field": field, "at": at, "from": before, "to": after,
                            "evidence_id": eid, "history_id": hid, "item_index": index})
            in_period = _timing(at, start, end) == "in_period"
            keep_values = in_period or field in STATE_FIELDS
            append(eid, "field_changed", history.get("author"), at, at, None, None,
                   flags + ([] if keep_values else ["values_omitted_outside_period"]),
                   issue_url, context_only=not in_period,
                   change={"field": field,
                           "from": before if keep_values else None,
                           "to": after if keep_values else None})
        if at is None:
            gaps.append("history " + hid + ": missing or unzoned timestamp")

    for worklog in worklog_items:
        wid, flags = _id(worklog)
        at = parse_timestamp(worklog.get("started"))
        recorded = parse_timestamp(worklog.get("created"))
        modified = parse_timestamp(worklog.get("updated"))
        in_period = _timing(at, start, end) == "in_period"
        flags += ["self_reported_work_time"]
        if recorded is not None and recorded >= end:
            flags.append("recorded_after_period")
        version_known = recorded is not None and modified is not None and recorded < end and modified < end
        if not version_known:
            flags.append("period_end_worklog_version_unverified")
        if not in_period:
            flags.append("text_omitted_outside_period")
        seconds = worklog.get("timeSpentSeconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            seconds = None
        append(key + ":worklog:" + wid, "worklog", worklog.get("author"),
               at, recorded, modified,
               render_text(worklog.get("comment")).strip() if in_period else None,
               flags, issue_url, context_only=not in_period,
               reported_seconds=seconds,
               update_actor=_actor(worklog.get("updateAuthor")),
               text_as_of_period_end="known" if version_known else "unknown",
               current_text_hash=_hash(worklog.get("comment")))
        if at is None:
            gaps.append("worklog " + wid + ": missing or unzoned work timestamp")

    roles, state, reconstruction_gaps = _reconstruct(
        fields, changes, history_coverage["complete"], created, observed_at,
        start, end, target, snapshot_id)
    gaps.extend(reconstruction_gaps)
    if created is None:
        gaps.append("issue: missing or unzoned creation timestamp")
    # Lists remain deterministic even when endpoint pagination order differs.
    evidence.sort(key=lambda item: (
        parse_timestamp(item["occurred_at"]).astimezone(timezone.utc)
        if item["occurred_at"] else datetime.min.replace(tzinfo=timezone.utc),
        item["evidence_id"]))
    links = fields.get("issuelinks") or []
    related = []
    for link in links:
        if isinstance(link, dict):
            for direction in ("inwardIssue", "outwardIssue"):
                linked = link.get(direction)
                if isinstance(linked, dict) and linked.get("key"):
                    related.append(str(linked["key"]))
    return {
        "key": key, "id": str(issue["id"]) if issue.get("id") is not None else None,
        "url": issue_url, "summary": fields.get("summary"),
        "current": {"assignee": _actor(fields.get("assignee")),
                    "reporter": _actor(fields.get("reporter")),
                    "creator": _actor(fields.get("creator")),
                    "status": _named(fields.get("status")),
                    "resolution": _named(fields.get("resolution")),
                    "created_at": _iso(created), "updated_at": _iso(updated),
                    "issue_type": _named(fields.get("issuetype")),
                    "project": {name: (fields.get("project") or {}).get(name)
                                for name in ("id", "key", "name")},
                    "parent_key": (fields.get("parent") or {}).get("key"),
                    "related_issue_keys": sorted(set(related)),
                    "observed_at": observed_at.isoformat()},
        "role_eligibility": roles, "status_at_period_end": state,
        "evidence": evidence, "coverage": coverage,
        "gaps": list(dict.fromkeys(gaps)),
    }
