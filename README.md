# Jira 업무 성과 스킬

Jira Cloud에서 현재 또는 분석 기간 중 담당자·보고자가 본인인 이슈를 수집하고, 기간 내 기여를 근거가 연결된 성과로 정리합니다. Python이 API 조회와 정규화를 수행하고 기존 LLM 환경이 분석과 문장 작성을 담당합니다.

## 시작하기

Python 3.9 이상과 Jira Cloud 계정이 필요합니다. Python 표준 라이브러리만 사용하므로 pip 설치, 서버, DB, 별도 LLM API 키가 필요하지 않습니다. 현재 지원 환경은 Claude Code와 Jira Cloud입니다.

저장소 디렉터리에서 실행합니다.

```bash
python3 run.py install
python3 run.py setup
```

`install`은 `~/.claude/skills/jira-achievements`에 이 저장소의 스킬 폴더를 연결합니다. `CLAUDE_CONFIG_DIR`을 설정했으면 그 아래 `skills/jira-achievements`를 사용합니다. 이미 다른 파일이 있으면 보존하고 중단합니다. 설치 후 원본 저장소 경로를 유지하세요. Claude Code에서 다음처럼 요청할 수 있습니다. 목록에 나타나지 않으면 새 세션에서 확인하세요.

> /jira-achievements 2026-07-01 2026-09-30 내 Jira 업무를 성과로 정리해줘.

Claude Code의 [스킬 형식](https://code.claude.com/docs/en/skills)에 맞춰 `SKILL.md`의 프런트매터와 본문을 사용합니다. 별도 에이전트 UI 메타데이터 파일은 포함하지 않습니다. 개인 스킬 폴더를 원본 디렉터리에 심볼릭 링크로 연결하는 방식도 공식 지원됩니다.

`setup`은 사이트 주소, 이메일, API 주소, 시간대, 기본 프로젝트를 입력받고 연결을 확인합니다. “나”는 토큰으로 인증한 계정이며 `/myself`로 확인합니다. 본인 계정의 토큰을 사용하세요. API 토큰은 터미널에서 숨김 입력하며 채팅에 붙여넣을 필요가 없습니다.

- 일반 API 토큰: 사이트 URL은 `https://회사.atlassian.net`, 별도 API 주소는 빈 값.
- 범위가 지정된 API 토큰: 사이트 URL은 동일하며 별도 API 주소에 `https://api.atlassian.com/ex/jira/{cloudId}`를 입력합니다. 현재 계정, 이슈 검색·상세, 댓글, 작업 기록, 변경 이력을 읽을 권한이 필요합니다. [Atlassian API 토큰 안내](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/)

토큰 저장은 선택입니다. 저장하면 사용자 설정 파일에 평문으로 기록하고 POSIX 환경에서 파일 권한 600·디렉터리 700을 적용합니다. 저장하지 않으면 실행 환경의 `JIRA_API_TOKEN`을 사용합니다. 설정과 사용자 지침, 결과물은 기본적으로 `~/.config/jira-achievements/`에 저장되어 저장소 업데이트와 분리됩니다. 위치는 `JIRA_ACHIEVEMENTS_HOME`으로 바꿀 수 있습니다.

비대화형 실행에서는 `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, 선택적인 `JIRA_API_URL` 환경 변수를 사용할 수 있습니다. 환경 변수는 저장된 연결 설정보다 우선합니다. 기존 기본 프로젝트나 API 주소를 지우려면 setup의 해당 입력에서 `-`를 입력합니다.

## 수집과 재분석

```bash
python3 run.py doctor
python3 run.py collect --start 2026-07-01 --end 2026-09-30
python3 run.py collect --start 2026-07-01 --end 2026-09-30 --project SEC --project OPS
python3 run.py paths
```

날짜는 양 끝 날짜를 포함하며 기본 시간대는 Asia/Seoul입니다. `--timezone`, `--output`, `--style`로 실행별 설정을 바꿀 수 있습니다. 기간 중 실제 기여를 찾기 위해 생성일·최종 수정일의 기간 조건으로 후보를 잘라내지 않습니다. 현재 담당/보고 이슈와 과거 담당/보고 이슈를 각각 조회하고 모든 페이지를 읽습니다. 계정의 오래된 이슈가 많으면 수집에 시간이 걸릴 수 있어 프로젝트 범위 지정이 유용합니다.

조회 실패가 있으면 가능한 자료를 보존하고 `coverage: partial` 및 실패 목록을 기록합니다. 종료 코드는 성공 0, 실행 오류 1, 부분 수집 2입니다. 부분 수집은 성과가 없다는 뜻이 아닙니다. 이력 검색이 실패해도 현재 담당자 조회만으로 전체를 수집했다고 표시하지 않습니다. 기존 결과 폴더는 덮어쓰지 않습니다.

결과 폴더의 `manifest.json`과 `analysis-index.json`부터 LLM에 제공하고 스킬의 분석 순서를 따르게 합니다. 생성된 `analyze.md`와 `achievement-style.md`는 해당 실행 시점의 프롬프트·지침 사본입니다. 데이터에 대한 표현 피드백만 바뀌었다면 사용자 지침을 수정한 뒤 기존 데이터를 다시 분석하면 됩니다. 수집 명령 자체는 LLM을 실행하거나 보고서를 작성하지 않습니다.

## 수정할 파일

| 파일 | 책임 |
| --- | --- |
| `skills/jira-achievements/SKILL.md` | 자연어 요청에서 수집·분석으로 이어지는 실행 흐름 |
| `skills/jira-achievements/prompts/analyze.md` | 근거·기여·시점·완료 여부 판정과 보고서 구조 |
| `skills/jira-achievements/guidelines/achievement-style.md` | 신규 사용자에게 복사하는 성과 표현 지침 템플릿 |
| `paths`가 출력하는 사용자 `achievement-style.md` | 실제 피드백을 누적할 개인 지침. 업데이트 시 덮어쓰지 않음 |
| `scripts/jira_client.py` | Cloud API와 페이지 처리 |
| `scripts/jira_evidence.py` | 시각·역할 이력·종료 시 상태·근거 정규화 |
| `scripts/jira_achievements.py` | 설정·설치·수집·분석 배치 생성 |

마지막 세 파일은 스킬 폴더 아래의 `scripts/`에 있습니다. 형식의 상세는 [데이터 계약](skills/jira-achievements/references/data-contract.md)에 정리했습니다.

## 검증과 현재 범위

```bash
python3 -m unittest discover -s tests -v
python3 run.py --help
```

테스트는 모의 API 응답과 임시 디렉터리만 사용합니다. 라이브 Jira 인증·실데이터 수집·실제 성과 보고서의 품질은 아직 검증하지 않았습니다. `complete`는 API에 노출된 조회 범위의 수집 완료이며, 권한 밖 자료·삭제된 댓글·복원 불가능한 과거 본문까지 보증하지 않습니다. 기간 후 수정된 댓글과 소급 작업 기록은 원본 시점과 구분합니다. 첨부파일 내용과 외부 링크의 문서는 수집하지 않습니다.

Cloud 구현의 기준 문서는 [Enhanced JQL search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/), [JQL 날짜 필드](https://support.atlassian.com/jira-software-cloud/docs/jql-fields/), [JQL 이력 연산자](https://support.atlassian.com/jira-software-cloud/docs/jql-operators/)입니다. API나 수집 규칙을 변경할 때 관련 오프라인 회귀 테스트와 데이터 계약도 함께 수정합니다.
