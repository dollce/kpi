#!/usr/bin/env python3
"""One entrypoint for local setup and read-only Jira Cloud evidence collection."""

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jira_client import JiraClient, JiraError
from jira_evidence import normalize_issue


VERSION = "0.1.0"
SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "1.0"


def state_dir():
    override = os.environ.get("JIRA_ACHIEVEMENTS_HOME")
    return Path(override).expanduser() if override else Path.home() / ".config" / "jira-achievements"


def paths():
    root = state_dir()
    return {"config": root / "config.json", "style": root / "achievement-style.md",
            "outputs": root / "outputs", "skill": SKILL_ROOT}


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def write_private(path, text):
    """Atomic writes prevent half-written config; temporary files start private."""
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def dump_json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def ensure_style():
    destination = paths()["style"]
    if not destination.exists():
        write_private(destination, (SKILL_ROOT / "guidelines" / "achievement-style.md").read_text(encoding="utf-8"))
    return destination


def load_config():
    config_path = paths()["config"]
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise ValueError("설정 파일을 읽을 수 없습니다. setup으로 다시 설정하세요.") from None
        if not isinstance(config, dict):
            raise ValueError("설정 파일은 JSON 객체여야 합니다.")
    else:
        config = {}
    for field, env in (("site_url", "JIRA_URL"), ("email", "JIRA_EMAIL"),
                       ("api_token", "JIRA_API_TOKEN"), ("api_url", "JIRA_API_URL")):
        if os.environ.get(env):
            config[field] = os.environ[env]
    config.setdefault("timezone", "Asia/Seoul")
    config.setdefault("projects", [])
    for field in ("site_url", "email", "api_token"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError("Jira 연결 설정이 필요합니다. setup 또는 JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN을 사용하세요.")
    if (not isinstance(config["projects"], list)
            or any(not isinstance(p, str) or not p.strip() for p in config["projects"])):
        raise ValueError("설정의 projects는 프로젝트 키 문자열 목록이어야 합니다.")
    return config


def client_for(config):
    return JiraClient(base_url=config["site_url"], email=config["email"], token=config["api_token"],
                      api_url=config.get("api_url") or None)


def get_subject(client):
    user = client.current_user()
    account_id = user.get("accountId")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("현재 Cloud 계정의 accountId를 확인하지 못했습니다.")
    # Keep a minimal identity; do not export email, avatars or the config object.
    return {k: user[k] for k in ("accountId", "displayName", "timeZone") if k in user}


def prompt_value(label, default="", allow_clear=False):
    value = input(label + (" [%s]" % default if default else "") + ": ").strip()
    if allow_clear and value == "-":
        return ""
    return value or default


def setup(args):
    config_path = paths()["config"]
    old = {}
    if config_path.exists():
        try:
            old = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        if not isinstance(old, dict):
            old = {}
    print("Jira Cloud 설정. API 토큰은 화면에 표시되지 않습니다.")
    config = {
        "site_url": prompt_value("Jira 사이트 URL", old.get("site_url", "")),
        "email": prompt_value("Atlassian 이메일", old.get("email", "")),
        "api_url": prompt_value("별도 API 주소 (일반 토큰은 빈 값, 기존 값 지우기는 -)", old.get("api_url", ""), True),
        "timezone": prompt_value("보고서 시간대", old.get("timezone", "Asia/Seoul")),
        "projects": [p.strip() for p in prompt_value("기본 프로젝트 키 (쉼표 구분, 전체로 바꾸기는 -)",
                                                     ",".join(old.get("projects", [])), True).split(",") if p.strip()],
    }
    token = getpass.getpass("API 토큰 (기존 저장 토큰을 유지하려면 Enter): ").strip()
    token = token or os.environ.get("JIRA_API_TOKEN") or old.get("api_token", "")
    ZoneInfo(config["timezone"])
    test_config = dict(config, api_token=token)
    client = client_for(test_config)  # Local syntax validation before writing anything.
    store_token = prompt_value("토큰을 이 사용자만 읽는 로컬 설정 파일에 저장할까요? y/N", "N").lower() == "y"
    if store_token:
        config["api_token"] = token
    dump_json(config_path, config)
    ensure_style()
    print("설정 저장:", config_path)
    if not store_token:
        print("다음 실행에는 JIRA_API_TOKEN 환경 변수가 필요합니다.")
    if args.no_check:
        print("연결 확인을 건너뛰었습니다. doctor로 확인할 수 있습니다.")
    else:
        subject = get_subject(client)
        print("연결 확인:", subject.get("displayName", ""), "(%s)" % subject["accountId"])


def period_bounds(start, end, timezone):
    zone = ZoneInfo(timezone)
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if last < first:
        raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
    return (dt.datetime.combine(first, dt.time.min, zone),
            dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min, zone))


def jql_string(value):
    if any(ord(c) < 32 for c in value):
        raise ValueError("JQL 값에는 제어 문자를 사용할 수 없습니다.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def candidate_queries(subject, start, end, projects):
    user = jql_string(subject["accountId"])
    try:
        zone = ZoneInfo(subject.get("timeZone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = ZoneInfo("UTC")
    # Candidate boundaries are deliberately wider; evidence uses exact timestamps.
    # Do not use created/updated bounds: retrospective records and later updates matter.
    lower = (start.astimezone(zone) - dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    upper = (end.astimezone(zone) + dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    scope = "project IN (%s) AND " % ", ".join(jql_string(p) for p in projects) if projects else ""
    conditions = [
        ("current_roles", "assignee = %s OR reporter = %s" % (user, user)),
        ("historical_assignee", "assignee WAS %s DURING (%s, %s)" % (user, jql_string(lower), jql_string(upper))),
        ("historical_reporter", "reporter WAS %s DURING (%s, %s)" % (user, jql_string(lower), jql_string(upper))),
    ]
    return [{"purpose": purpose, "jql": scope + "(" + clause + ") ORDER BY key ASC"}
            for purpose, clause in conditions]


def stream_dict(result):
    return {"items": result.items, "complete": result.complete, "gaps": result.gaps}


def json_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def batch_analysis(records, root, max_bytes=120000):
    """Split at evidence boundaries. Never truncate a large piece of evidence."""
    fragments = []
    issue_ids, evidence_ids = set(), set()
    for record in records:
        issue_ids.add(record["key"])
        base = {k: v for k, v in record.items() if k != "evidence"}
        chunks, chunk = [], []
        for item in record.get("evidence", []):
            evidence_ids.add(item["evidence_id"])
            trial = dict(base, evidence=chunk + [item])
            if chunk and len(json_line(trial).encode("utf-8")) > max_bytes:
                chunks.append(chunk)
                chunk = []
            chunk.append(item)
        chunks.append(chunk)
        for index, items in enumerate(chunks, 1):
            fragments.append(dict(base, evidence=items, fragment_index=index, fragment_count=len(chunks)))
    batches, current = [], []
    for fragment in fragments:
        trial = {"schema_version": SCHEMA_VERSION, "issues": current + [fragment]}
        if current and len(json_line(trial).encode("utf-8")) > max_bytes:
            batches.append(current)
            current = []
        current.append(fragment)
    if current:
        batches.append(current)
    directory = root / "analysis"
    private_dir(directory)
    index = {"schema_version": SCHEMA_VERSION, "unique_issues": len(issue_ids),
             "unique_evidence": len(evidence_ids), "fragments": len(fragments),
             "max_bytes_target": max_bytes, "batches": []}
    for number, rows in enumerate(batches, 1):
        relative = "analysis/batch-%04d.json" % number
        payload = json_line({"schema_version": SCHEMA_VERSION, "issues": rows})
        write_private(root / relative, payload)
        index["batches"].append({"path": relative, "bytes": len(payload.encode("utf-8")),
                                  "oversized": len(payload.encode("utf-8")) > max_bytes,
                                  "issue_keys": sorted({row["key"] for row in rows}),
                                  "evidence_ids": [e["evidence_id"] for row in rows for e in row["evidence"]]})
    dump_json(root / "analysis-index.json", index)
    return index


def collect_run(client, config, start, end, output, projects=None, style_path=None, batch_bytes=120000):
    """Inject a client for offline verification. Real service calls occur only here/doctor/setup."""
    if output.exists():
        raise ValueError("결과 폴더가 이미 존재합니다. 새로운 --output 경로를 사용하세요.")
    style = Path(style_path) if style_path else ensure_style()
    style_text = style.read_text(encoding="utf-8")
    subject = get_subject(client)
    private_dir(output)
    private_dir(output / "raw")
    projects = list(config.get("projects", []) if projects is None else projects)
    observed = dt.datetime.now(dt.timezone.utc)
    manifest = {
        "schema_version": SCHEMA_VERSION, "collector_version": VERSION,
        "collected_at": observed.isoformat(), "subject": subject,
        "period": {"start": start.isoformat(), "end_exclusive": end.isoformat(),
                   "timezone": str(start.tzinfo)},
        "site_url": config["site_url"],
        "scope": {"projects": projects, "role_policy": "current_or_during_period",
                  "historical_candidate_buffer_days": 2, "activity_date_filter": "evidence_after_collection"},
        "queries": [], "failures": [], "counts": {},
        "coverage": {"status": "partial", "gaps": [], "limitations": [
            "API-visible data only; inaccessible or deleted records are not enumerated.",
            "Collection is not a transactional snapshot; search indexing and concurrent edits may affect results.",
            "JQL historical search and exposed changelogs may be limited by Jira; collection does not certify all work.",
            "Descriptions and edited comments are current snapshots, not recovered historical text.",
        ]},
    }
    records, candidates = [], {}
    interrupted = False
    try:
        for query in candidate_queries(subject, start, end, projects):
            result = client.search_issues(query["jql"], fields=["summary"])
            manifest["queries"].append(dict(query, complete=result.complete, returned=len(result.items), gaps=result.gaps))
            manifest["coverage"]["gaps"].extend(query["purpose"] + ": " + gap for gap in result.gaps)
            if not result.complete and not result.gaps:
                manifest["coverage"]["gaps"].append(query["purpose"] + ": incomplete search.")
            for row in result.items:
                if row.get("key"):
                    candidates[row["key"]] = row
                else:
                    manifest["coverage"]["gaps"].append("Search returned a record without an issue key.")
        with (output / "raw" / "issues.jsonl").open("w", encoding="utf-8") as raw_handle, \
                (output / "issues.jsonl").open("w", encoding="utf-8") as normalized_handle:
            if os.name == "posix":
                os.chmod(raw_handle.name, 0o600)
                os.chmod(normalized_handle.name, 0o600)
            for number, key in enumerate(sorted(candidates), 1):
                try:
                    issue = client.get_issue(key)
                    streams = {"comments": stream_dict(client.get_comments(key)),
                               "changelog": stream_dict(client.get_changelog(key)),
                               "worklogs": stream_dict(client.get_worklogs(key))}
                    issue_observed = dt.datetime.now(dt.timezone.utc)
                    raw_handle.write(json_line({"issue": issue, **streams, "observed_at": issue_observed.isoformat()}))
                    raw_handle.flush()
                    for name, stream in streams.items():
                        if not stream["complete"]:
                            manifest["coverage"]["gaps"].append(key + " " + name + ": incomplete.")
                    record = normalize_issue(issue=issue, target_user=subject, start=start, end=end,
                                             observed_at=issue_observed, site_url=config["site_url"], **streams)
                    records.append(record)
                    normalized_handle.write(json_line(record))
                    normalized_handle.flush()
                except JiraError as exc:
                    manifest["failures"].append({"key": key, "stage": "fetch", "error": str(exc)})
                except (ValueError, TypeError, KeyError) as exc:
                    manifest["failures"].append({"key": key, "stage": "normalize", "error": type(exc).__name__})
                if number % 10 == 0 or number == len(candidates):
                    print("이슈 처리 %d/%d" % (number, len(candidates)), file=sys.stderr)
    except KeyboardInterrupt:
        interrupted = True
        manifest["coverage"]["gaps"].append("Collection interrupted; some queries or issues were not processed.")
    finally:
        index = batch_analysis(records, output, batch_bytes)
        write_private(output / "achievement-style.md", style_text)
        write_private(output / "analyze.md", (SKILL_ROOT / "prompts" / "analyze.md").read_text(encoding="utf-8"))
        write_private(output / "data-contract.md", (SKILL_ROOT / "references" / "data-contract.md").read_text(encoding="utf-8"))
        manifest["style"] = {"source": str(style.resolve()), "sha256": hashlib.sha256(style_text.encode("utf-8")).hexdigest()}
        processed = {r["key"] for r in records} | {f["key"] for f in manifest["failures"]}
        manifest["unprocessed_issue_keys"] = sorted(set(candidates) - processed)
        manifest["counts"] = {"candidate_issues": len(candidates), "normalized_issues": len(records),
                              "failed_issues": len(manifest["failures"]),
                              "unprocessed_issues": len(manifest["unprocessed_issue_keys"]),
                              "issues_with_evidence_gaps": sum(bool(r.get("gaps")) for r in records),
                              "unique_evidence": index["unique_evidence"],
                              "analysis_fragments": index["fragments"], "analysis_batches": len(index["batches"])}
        complete = (len(manifest["queries"]) == 3 and all(q["complete"] for q in manifest["queries"])
                    and not manifest["coverage"]["gaps"] and not manifest["failures"]
                    and not manifest["unprocessed_issue_keys"] and not interrupted)
        manifest["coverage"]["status"] = "complete" if complete else "partial"
        manifest["analysis_warnings"] = ["Oversized evidence: read all fragments without truncation."] if any(b["oversized"] for b in index["batches"]) else []
        manifest["files"] = {"normalized": "issues.jsonl", "raw": "raw/issues.jsonl", "analysis_index": "analysis-index.json",
                             "prompt": "analyze.md", "style": "achievement-style.md", "contract": "data-contract.md"}
        manifest["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        dump_json(output / "manifest.json", manifest)
    return manifest


def install(destination=None):
    claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")).expanduser()
    target = Path(destination).expanduser() if destination else claude_root / "skills" / "jira-achievements"
    if target.is_symlink() and target.resolve() == SKILL_ROOT:
        return target
    if target.exists() or target.is_symlink():
        raise ValueError("설치 대상이 이미 존재합니다. 기존 파일을 보존했습니다: " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(SKILL_ROOT, target_is_directory=True)
    return target


def parser():
    result = argparse.ArgumentParser(description="Jira Cloud 업무 근거 수집. Python 3.9+, 추가 패키지 없음.")
    result.add_argument("--version", action="version", version=VERSION)
    sub = result.add_subparsers(dest="command", required=True)
    setup_parser = sub.add_parser("setup", help="최초 연결 설정과 사용자 지침 생성")
    setup_parser.add_argument("--no-check", action="store_true", help="설정만 저장하고 실제 연결 확인 생략")
    sub.add_parser("doctor", help="현재 Cloud 연결과 대상 계정 확인")
    sub.add_parser("paths", help="개인 설정·지침·결과 저장 위치")
    installer = sub.add_parser("install", help="Claude Code skills 폴더에 이 스킬 연결")
    installer.add_argument("--dest", help="대상 스킬 폴더 경로 (기본 ~/.claude/skills/jira-achievements)")
    export = sub.add_parser("collect", help="Jira를 읽고 분석 패키지 생성")
    export.add_argument("--start", required=True, help="시작 날짜 YYYY-MM-DD, 포함")
    export.add_argument("--end", required=True, help="종료 날짜 YYYY-MM-DD, 포함")
    export.add_argument("--timezone", help="기본 설정 Asia/Seoul")
    export.add_argument("--project", action="append", help="프로젝트 키, 여러 번 지정 가능")
    export.add_argument("--output", help="새 결과 폴더 (기존 폴더 덮어쓰기 금지)")
    export.add_argument("--style", help="이번 실행에 사용할 성과 작성 지침")
    export.add_argument("--batch-bytes", type=int, default=120000, help="분석 배치 목표 크기, 기본 120000 bytes")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "paths":
            print(json.dumps({k: str(v) for k, v in paths().items()}, ensure_ascii=False, indent=2))
        elif args.command == "install":
            print("스킬 연결:", install(args.dest))
            print("Claude Code에서 /jira-achievements로 사용할 수 있습니다. 목록에 없으면 새 세션에서 확인하세요.")
        elif args.command == "setup":
            setup(args)
        elif args.command == "doctor":
            config = load_config()
            print(json.dumps({"site_url": config["site_url"], "subject": get_subject(client_for(config))}, ensure_ascii=False, indent=2))
        elif args.command == "collect":
            config = load_config()
            start, end = period_bounds(args.start, args.end, args.timezone or config["timezone"])
            if args.batch_bytes < 1000:
                raise ValueError("--batch-bytes는 1000 이상이어야 합니다.")
            style = Path(args.style).expanduser() if args.style else ensure_style()
            if not style.is_file():
                raise ValueError("성과 작성 지침 파일을 찾을 수 없습니다.")
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            output = Path(args.output).expanduser() if args.output else paths()["outputs"] / (args.start + "_" + args.end + "_" + stamp)
            manifest = collect_run(client_for(config), config, start, end, output, args.project, style, args.batch_bytes)
            print(json.dumps({"output": str(output.resolve()), "coverage": manifest["coverage"]["status"],
                              "counts": manifest["counts"]}, ensure_ascii=False, indent=2))
            return 0 if manifest["coverage"]["status"] == "complete" else 2
        return 0
    except JiraError as exc:
        hint = ""
        if exc.status == 401:
            hint = " 이메일·토큰·만료 여부를 확인하세요. 범위 지정 토큰은 api.atlassian.com/ex/jira/{cloudId} API 주소가 필요합니다."
        elif exc.status == 403:
            hint = " 계정 권한과 API 토큰의 Jira 읽기 범위를 확인하세요."
        print("오류:", str(exc) + hint, file=sys.stderr)
        return 1
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        print("오류:", str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("입력을 취소했습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
