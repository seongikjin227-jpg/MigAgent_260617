# Logic Description

Version: 2026-06-17

이 문서는 `python main.py`와 Streamlit UI에서 실제로 동작하는 agent, repository, service, LLM, RAG 흐름을 코드 기준으로 설명합니다.

## 1. 전체 실행 구조

```text
main.py
  -> SupervisorAgent.run()
      -> build_supervisor_graph()
          -> poll
          -> execute
          -> wait
          -> END
      -> while not stop_requested:
          graph.invoke(state)
```

핵심:

- supervisor graph는 한 번 invoke될 때 한 cycle만 처리합니다.
- process 종료는 `SupervisorAgent.run()`의 바깥 while loop가 stop 신호를 받을 때 발생합니다.
- cycle 시작마다 active LLM fallback model 상태를 reset합니다.
- agent 실행 순서는 DB Migration -> SQL Conversion -> SQL Tuning -> SQL Formatting입니다.

## 2. Supervisor Agent

관련 파일:

```text
server/agents/supervisor/agent.py
server/agents/supervisor/graph.py
server/tools/context.py
```

초기화:

```text
SupervisorAgent.__init__()
  -> MigrationOrchestrator 생성
  -> SqlConversionAgent 생성
  -> SqlTuningAgent 생성
  -> SqlFormattingAgent 생성
  -> build_supervisor_graph(...)
```

agent 선택 flag:

```env
DB_MIGRATION_ONLY=false
SQL_CONVERSION_ONLY=true
SQL_TUNING_ONLY=false
SQL_FORMATTING_ONLY=false
```

```text
HAS_AGENT_SELECTION =
  DB_MIGRATION_ONLY
  OR SQL_CONVERSION_ONLY
  OR SQL_TUNING_ONLY
  OR SQL_FORMATTING_ONLY

RUN_MIGRATION      = DB_MIGRATION_ONLY      OR NOT HAS_AGENT_SELECTION
RUN_SQL_CONVERSION = SQL_CONVERSION_ONLY    OR NOT HAS_AGENT_SELECTION
RUN_SQL_TUNING     = SQL_TUNING_ONLY        OR NOT HAS_AGENT_SELECTION
RUN_SQL_FORMATTING = SQL_FORMATTING_ONLY    OR NOT HAS_AGENT_SELECTION
```

## 3. Supervisor graph

### 3.1 poll node

```text
poll_node(state)
  -> stop / pause 확인
  -> cycle 증가
  -> reset_active_model()
  -> start_cycle_metrics(cycle)
  -> agent별 job 조회
  -> batch size만큼 registry에 저장
```

registry key:

| registry | key |
| --- | --- |
| `mig_registry` | `job.map_id` |
| `sql_registry` | `job.row_id` |
| `tuning_registry` | `job.row_id` |
| `formatting_registry` | `job.row_id` |

### 3.2 execute node

```text
execute_node(state)
  -> DB Migration registry 실행
  -> SQL Conversion registry 실행
  -> SQL Tuning registry 실행
  -> SQL Formatting registry 실행
```

각 tool wrapper는 다음 공통 흐름을 가집니다.

```text
registry에서 job 조회
  -> BATCH_CNT 증가
  -> 실제 agent callback 실행
  -> record_agent_run(agent_name, elapsed, final_status)
```

`server/tools/context.py`의 metrics status 분류:

- success: `SUCCESS`, `PASS`, `CONVERSION-PASS`, `TUNING-PASS`, `TUNING_PASS`, `PASS_NON_SELECT`
- skip: `SKIP`, `NA`
- 그 외: fail

### 3.3 wait node

```text
wait_node()
  -> finish_cycle_metrics()
  -> pause 확인
  -> POLL_INTERVAL_SEC 대기
```

## 4. DB Migration Agent

관련 파일:

```text
server/agents/migration/graph.py
server/repositories/migration/repository.py
server/services/migration/prompt_service.py
server/config/prompts/migration_prompt.json
```

DB Migration graph:

```text
fetch_ddl
  -> check_dependency
  -> generate
  -> execute
  -> verify
  -> finalize
```

### 4.1 Dependency

`check_dependency_node()`는 두 종류의 선행 조건을 확인합니다.

```text
1. PRIOR_MAP_ID dependency
   -> NEXT_MIG_INFO.PRIOR_MAP_ID가 NULL/0이면 통과
   -> 지정된 선행 MAP_ID의 STATUS가 PASS이면 통과
   -> FAIL/SKIP이면 현재 job은 SKIP
   -> 그 외 상태이면 WAITING

2. same-target priority dependency
   -> 같은 TO_TABLE 중 현재 job보다 PRIORITY가 낮은 job 조회
   -> 모든 선행 job이 PASS이면 통과
   -> FAIL/SKIP이면 현재 job은 SKIP
   -> 그 외 상태이면 WAITING
```

두 dependency를 모두 통과한 뒤에만 `BATCH_CNT`를 증가시키고 SQL 생성을 시작합니다.

### 4.2 SQL generation retry

```text
generate_sql_node()
  -> is_append = not is_first_job_for_target(map_id, to_table, priority)
  -> generate_sqls(..., is_append)
  -> migration_sql이 비어 있으면 LLM_RETRY
  -> migration_sql이 있으면 NEXT_MIG_LOG에 generated SQL 기록
```

`is_append=True`이면 prompt는 target table에 기존 선행 job 데이터가 있다고 보고 append mode 검증 SQL을 요구합니다.

### 4.3 ORA-00001 MERGE INTO retry

`prompt_service.build_migration_prompt()`는 `last_error`에 `ORA-00001`이 포함되면 `dup_key_suffix`를 추가합니다.

```text
ORA-00001 발생
  -> 다음 LLM retry prompt에 MERGE INTO 전략 강제
  -> INSERT INTO 금지
  -> ON 절은 target DDL의 PK/unique key 후보를 우선 사용
  -> WHEN NOT MATCHED THEN INSERT 형태 권장
```

이 로직은 PK/unique 중복으로 단순 INSERT가 실패한 경우 다음 retry에서 같은 실패를 반복하지 않게 하는 목적입니다.

## 5. Repository polling

관련 파일:

```text
server/repositories/sql/result_repository.py
server/services/sql/statuses.py
```

### 5.1 SQL Conversion job polling

```text
get_pending_jobs()
  -> RESULT_TABLE 기본값 NEXT_SQL_INFO
  -> STATUS 조건:
       STATUS IN (
         'URGENT', 'READY', 'PENDING',
         legacy 'FAIL',
         'FAIL-TOBE', 'FAIL-BIND', 'FAIL-TEST'
       )
       OR STATUS IS NULL
  -> TO_SQL_TEXT 조건:
       TO_SQL_TEXT IS NULL
       OR STATUS NOT IN ('CONVERSION-PASS', legacy 'PASS')
  -> BATCH_CNT < JOB_MAX_BATCH_COUNT
```

정렬 우선순위:

```text
URGENT
READY
legacy FAIL / FAIL-TOBE / FAIL-BIND / FAIL-TEST
PENDING
NULL
UPD_TS NULLS FIRST
effective FR SQL length ASC
SPACE_NM
SQL_ID
```

`SqlInfoJob.source_sql`:

```text
if EDIT_FR_SQL exists and not blank:
  source_sql = EDIT_FR_SQL
else:
  source_sql = FR_SQL_TEXT
```

따라서 conversion generation, bind generation, test generation, conversion RAG 검색은 사용자가 편집한 SQL을 우선 기준으로 합니다.

### 5.2 SQL Tuning job polling

```text
get_tuning_jobs()
  -> TUNED_TEST column 없으면 []
  -> WHERE TUNED_TEST IN (
       'URGENT', 'READY',
       legacy 'FAIL',
       'FAIL-TUNED', 'FAIL-BIND', 'FAIL-TEST'
     )
     AND TO_SQL_TEXT IS NOT NULL
     AND STATUS IN ('CONVERSION-PASS', legacy 'PASS')
     AND BATCH_CNT < JOB_MAX_BATCH_COUNT
```

주의:

- tuning fail status에는 `FAIL-TOBE`를 포함하지 않습니다.
- conversion 성공 row만 tuning queue에 올라옵니다.

### 5.3 SQL Formatting job polling

```text
get_formatting_jobs()
  -> FORMATTED_SQL 또는 TUNED_TEST column 없으면 []
  -> WHERE TUNED_TEST IN (
       'TUNING-PASS',
       'TUNING_PASS',
       legacy 'PASS',
       'PASS_NON_SELECT'
     )
     AND FORMATTED_SQL is null/empty/blank
     AND BATCH_CNT < JOB_MAX_BATCH_COUNT
```

Tuning agent가 정상 경로에서 `FORMATTED_SQL`을 생성하면 별도 formatting queue에는 들어가지 않습니다.

## 6. NEXT_SQL_INFO 상태값

### 6.1 `STATUS`

| 상태 | 의미 |
| --- | --- |
| `URGENT` | conversion 우선 처리 대기 |
| `READY` | conversion 일반 대기 |
| `PENDING` | conversion 대기 |
| `CONVERSION-PASS` | conversion 성공 |
| `FAIL-TOBE` | TO-BE SQL 생성 또는 사용 stage 실패 |
| `FAIL-BIND` | bind SQL 생성/실행 또는 bind set 생성 stage 실패 |
| `FAIL-TEST` | test SQL 생성/실행 또는 검증 stage 실패 |
| `SKIP` | 사용자 제외 |
| `NA` | 처리 대상 제외 |
| `NULL` | conversion 대기 |

기존 `PASS`, `FAIL`은 호환 조회용입니다. 신규 저장은 `CONVERSION-PASS`, `FAIL-*`를 사용합니다.

### 6.2 `TUNED_TEST`

| 상태 | 의미 |
| --- | --- |
| `URGENT` | tuning 우선 처리 대기 |
| `READY` | tuning 일반 대기 |
| `TUNING-PASS` | tuning 성공 |
| `PASS_NON_SELECT` | non-SELECT tuned validation 생략 성공 |
| `FAIL-TUNED` | tuned SQL 생성 stage 실패 |
| `FAIL-BIND` | tuned test bind 관련 stage 실패 |
| `FAIL-TEST` | tuned test SQL 생성/실행 또는 검증 stage 실패 |
| `NULL` | tuning 대상 아님 또는 conversion 미완료 |
| `NA` | tuning 제외 |

기존 `PASS`, `TUNING_PASS`, `FAIL`은 호환 조회용입니다. 신규 저장은 `TUNING-PASS`, `FAIL-*`를 사용합니다.

## 7. SQL Conversion Agent

관련 파일:

```text
server/agents/sql_conversion/agent.py
server/services/sql/agents.py
server/services/sql/workflow/graph.py
server/services/sql/llm_service.py
```

wrapper:

```text
SqlConversionAgent.process_job(job)
  -> TobeMultiAgentCoordinator.process_job(job)
```

conversion coordinator:

```text
process_job(job)
  -> SQL_LENGTH = classify_sql_length(FR_SQL_TEXT, EDIT_FR_SQL)
  -> MAP_TYPE = get_sql_map_type(TARGET_TABLE)
  -> update_job_classification()
  -> legacy/retry fail이면 reset_tuning_state()
  -> get_unready_target_tables(TARGET_TABLE)
       있으면 STATUS=NA, 종료
  -> 최대 3회 retry
       -> workflow graph 실행
       -> SELECT가 아니면 non-select completion
       -> test validation PASS면 persist success
       -> 실패면 last_error 구성 후 retry
  -> 최종 실패면 STATUS = state.failure_status
```

workflow graph:

```text
START
  -> tobe_generation.generate
  -> route_after_generation
       TAG_KIND != SELECT -> END
       TAG_KIND == SELECT -> tobe_generation.validate
  -> END
```

generation/validation stage별 failure status:

```text
TO-BE SQL 생성/사용 중 예외 -> FAIL-TOBE
Bind SQL 생성/실행/bind set 생성 중 예외 -> FAIL-BIND
Test SQL 생성/실행/검증 실패 -> FAIL-TEST
성공 -> CONVERSION-PASS
```

conversion 성공 저장:

```text
TO_SQL_TEXT = state.tobe_sql
BIND_SQL = state.bind_sql
BIND_SET = state.bind_set_json_for_test
TEST_SQL = state.test_sql
STATUS = CONVERSION-PASS
TUNED_TEST = READY
```

non-SELECT conversion:

```text
TAG_KIND != SELECT
  -> TO-BE SQL 생성 또는 TOBE_CORRECT_SQL 사용
  -> bind/test validation 생략
  -> STATUS = CONVERSION-PASS
  -> TUNED_TEST = READY
```

## 8. Correct SQL stage bypass

관련 파일:

```text
server/services/sql/agents.py
server/services/sql/llm_service.py
server/services/sql/correct_sql_rag_service.py
```

### 7.1 `TOBE_CORRECT_SQL`

```text
TO-BE stage
  -> TOBE_CORRECT_SQL 존재?
      Y: state.tobe_sql = TOBE_CORRECT_SQL, LLM 호출 없음
      N: generate_tobe_sql() LLM 호출
```

### 7.2 `BIND_CORRECT_SQL`

```text
Bind stage
  -> BIND_CORRECT_SQL 존재?
      Y:
        state.bind_sql = BIND_CORRECT_SQL
        LLM 호출 없음
        execute_binding_query()
        build_bind_sets()
      N:
        generate_bind_sql() LLM 호출
```

### 7.3 `TEST_CORRECT_SQL`

```text
Test stage
  -> TEST_CORRECT_SQL 존재?
      Y:
        state.test_sql = TEST_CORRECT_SQL
        LLM 호출 없음
        execute_test_query()
        evaluate_status_from_test_rows()
      N:
        generate_test_sql() LLM 호출
```

Correct SQL은 stage 생성만 대체합니다. 실행과 검증은 계속 수행합니다.

## 9. 통합 RAG 로직

관련 파일:

```text
server/services/sql/tobe_sql_tuning_service.py
server/services/sql/llm_service.py
app/utils/rag_db.py
app/pages/rag_manager_page.py
scripts/create_mig_rag_info_table.py
```

RAG 저장소:

```text
NEXT_MIG_RAG_INFO
  RAG_ID
  CATEGORY: SQL_CONVERSION | SQL_TUNING
  RULE_TYPE: GENERAL | SEARCH
  SOURCE_TABLES
  USE_YN
  GUIDANCE_TEXT
  SOURCE_SQL
  TARGET_SQL
  HIT_CNT
```

### 9.1 SOURCE_TABLES matching

```text
job_tables = parse NEXT_SQL_INFO.TARGET_TABLE
rule_tables = parse NEXT_MIG_RAG_INFO.SOURCE_TABLES

if rule_tables is empty:
  match = True
else:
  match = bool(rule_tables & job_tables)
```

중요:

- SQL 본문에서 table을 파싱하지 않습니다.
- `SOURCE_TABLES`는 운영자가 직접 입력합니다.
- `SOURCE_TABLES`는 comma-separated uppercase 형식을 권장합니다. 예: `ASIS_CODE,ASIS_USER`
- `NEXT_SQL_INFO.TARGET_TABLE`이 JSON list이거나 comma/space/semicolon/pipe 구분 문자열이어도 token으로 정규화합니다.

### 9.2 GENERAL rule

```text
load_universal_rules(category, source_tables)
  -> CATEGORY 일치
  -> RULE_TYPE='GENERAL'
  -> USE_YN='Y'
  -> SOURCE_TABLES match
  -> GUIDANCE_TEXT lines를 mandatory guidance로 반환
```

GENERAL rule은 vector search를 하지 않습니다.

### 9.3 SEARCH rule

```text
retrieve_rag_examples(sql_text, category, source_tables)
  -> sql_text를 main/subquery block으로 분리
  -> CATEGORY 일치
  -> RULE_TYPE='SEARCH'
  -> USE_YN='Y'
  -> SOURCE_TABLES match
  -> SOURCE_SQL 있는 rule만 후보
  -> embedding 가능하면 FAISS vector search
  -> 실패하면 token fallback search
  -> block별 top-k 반환
```

SQL block 분리:

```text
MAIN_SQL
SUBQUERY_1
SUBQUERY_2
...
```

subquery block을 먼저 검색하고 main SQL을 뒤에 붙입니다.

### 9.4 HIT_CNT

```text
_increment_prompt_rag_hits(payloads)
  -> prompt에 실제 포함된 SEARCH rule id 추출
  -> NEXT_MIG_RAG_INFO.HIT_CNT += 1
  -> UPDATED_AT = SYSTIMESTAMP
```

후보로 조회됐지만 prompt에 들어가지 않은 rule은 증가하지 않습니다.

## 10. SQL Conversion RAG

`generate_tobe_sql()` 흐름:

```text
source_tables = _load_target_tables(job)
conversion_general_rules =
  load_universal_conversion_rules(job.source_sql, source_tables)
conversion_examples =
  retrieve_conversion_examples(job.source_sql, source_tables)
mapping_schema_text =
  [MIGRATION_MAPPING_RULES]
  [SQL_CONVERSION_GENERAL_RAG_GUIDANCE]
  [SQL_CONVERSION_SEARCH_RAG_TOP_K_BY_SQL_BLOCK]
```

`job.source_sql`은 `EDIT_FR_SQL` 우선입니다.

prompt 전달 구조:

```text
[SQL_CONVERSION_GENERAL_RAG_GUIDANCE]
- RAG_ID=... | SOURCE_TABLES=...
  - guidance line

[SQL_CONVERSION_SEARCH_RAG_TOP_K_BY_SQL_BLOCK]
[
  {
    "matched_block_sql": "...",
    "guidance": ["..."],
    "source_sql": "conversion source example",
    "target_sql": "conversion target example"
  }
]
```

기본 mapping rule:

```text
[MIGRATION_MAPPING_RULES]
- FR_TABLE=... | FR_COL=... | TO_TABLE=... | TO_COL=...
```

`NEXT_SQL_COMPLEX_MAP`는 더 이상 SQL Conversion 보조 RAG 저장소로 사용하지 않습니다.

## 11. SQL Tuning Agent

관련 파일:

```text
server/agents/sql_tuning/agent.py
server/services/sql/agents.py
server/services/sql/tobe_sql_tuning_service.py
```

wrapper:

```text
SqlTuningAgent.process_job(job)
  -> JobExecutionState 생성
  -> state.tobe_sql = job.to_sql_text
  -> state.bind_set_for_db = job.bind_set
  -> core SqlTuningAgent.run(state)
  -> update_cycle_result(... TUNED_TEST=final_status ...)
```

예외 발생:

```text
update_tuning_error(
  TUNED_TEST = FAIL-TEST,
  TUNED_SQL = state.tuned_sql if exists,
  LOG = '[TUNING_ERROR] ...'
)
```

core run:

```text
run(state)
  -> max_iterations <= 0이면 return
  -> SELECT는 최대 2번 tuning/test attempt
  -> non-SELECT는 1번 tuning 후 validation 생략
  -> _apply_tuning_rules()
  -> no tuning이면 TUNED_TEST = TUNING-PASS
  -> SELECT면 tuned test validation 수행
  -> 성공이면 FORMATTED_SQL 생성
```

failure status:

```text
tuned SQL 생성/적용 중 예외 -> FAIL-TUNED
tuned test SQL 생성/실행/검증 실패 -> FAIL-TEST
성공 -> TUNING-PASS
non-SELECT 성공 -> PASS_NON_SELECT
```

### 11.1 `_apply_tuning_rules()`

```text
source_tables = parse NEXT_SQL_INFO.TARGET_TABLE
for iteration in 1..TOBE_SQL_TUNING_MAX_ITERATIONS:
  tuning_examples = retrieve_tuning_examples(current_sql, source_tables)
  update BLOCK_RAG_CONTENT
  if not tuning_examples:
    tuned_result = NO TUNING
    break
  tuned_sql, tuned_result = tune_tobe_sql(...)
  if tuned_sql unchanged:
    tuned_result = NO TUNING
    break
  current_sql = tuned_sql
```

### 11.2 Tuning prompt

`tune_tobe_sql()` payload:

```text
current_tobe_sql
universal_tuning_rules
tuning_examples_json
last_error
```

`universal_tuning_rules`:

```text
CATEGORY='SQL_TUNING'
RULE_TYPE='GENERAL'
SOURCE_TABLES match
```

`tuning_examples_json`:

```json
[
  {
    "source_sql": "current matched SQL block",
    "guidance": ["..."],
    "example_bad_sql": "SOURCE_SQL from RAG",
    "example_tuned_sql": "TARGET_SQL from RAG"
  }
]
```

### 11.3 tuned test validation

```text
_run_tuned_sql_validation(state)
  -> generate_sql_comparison_test_sql(
       baseline_sql = state.tobe_sql,
       candidate_sql = state.tuned_sql,
       bind_set_json = state.bind_set_for_db
     )
  -> execute_test_query(comparison_test_sql)
  -> evaluate_status_from_test_rows()
  -> state.tuned_test = TUNING-PASS or FAIL-TEST
```

tuned test는 AS-IS와 비교하지 않고 baseline TO-BE SQL과 tuned SQL을 비교합니다.

## 12. SQL Formatting Agent

관련 파일:

```text
server/agents/sql_formatting/agent.py
server/tools/sql_formatting.py
```

목적:

- tuning 과정에서 `FORMATTED_SQL` 생성이 누락된 row를 후속 보정합니다.

대상:

```text
TUNED_TEST IN (
  'TUNING-PASS',
  'TUNING_PASS',
  legacy 'PASS',
  'PASS_NON_SELECT'
)
AND FORMATTED_SQL is null/empty/blank
```

흐름:

```text
SqlFormattingAgent.process_job(job)
  -> source_sql = job.tuned_sql or job.to_sql_text
  -> source_sql 없으면 SKIP
  -> generate_formatted_sql()
  -> update_formatted_sql()
  -> return PASS
```

## 13. Correct SQL RAG Hint

관련 파일:

```text
server/services/sql/correct_sql_rag_service.py
server/repositories/sql/result_repository.py
```

correct kind mapping:

| kind | 우선 컬럼 |
| --- | --- |
| `TOBE` | `TOBE_CORRECT_SQL` |
| `BIND` | `BIND_CORRECT_SQL` |
| `TEST` | `TEST_CORRECT_SQL` |

검색 corpus:

```text
correct SQL이 있는 기존 NEXT_SQL_INFO row
  -> 검색 기준 SQL은 EDIT_FR_SQL 우선, 없으면 FR_SQL_TEXT
  -> current row는 제외
```

검색 방식:

```text
if RAG_EMBED_BASE_URL 있고 faiss/numpy 사용 가능:
  vector search
else:
  token fallback search
```

prompt 전달 값:

```json
[
  "SELECT ...",
  "SELECT ..."
]
```

메타데이터는 prompt에 넣지 않습니다.

## 14. LLM 호출 및 fallback

관련 파일:

```text
server/services/sql/llm_service.py
server/core/llm_fallback.py
```

공통 SQL call:

```text
_call_llm_for_job()
  -> call_llm_api()
  -> 성공:
       insert_sql_log(status=SUCCESS)
       return sql_text
  -> 실패:
       insert_sql_log(status=FAIL, error_message=...)
       raise
```

tuning call:

```text
_call_tuning_llm_for_job()
  -> call_llm_text_api()
  -> _extract_tuning_response()
  -> NEXT_SQL_LOG TUNED_SQL 기록
  -> tuned_result 있으면 NEXT_SQL_LOG TUNED_RESULT 기록
```

fallback 후보:

```text
model_candidates(primary_model)
  -> active_model
  -> primary_model
  -> LLM_FALLBACK_MODELS 순서
  -> 중복 제거
```

fallback 대상 오류:

- `model not allow`
- `model not allowed`
- `team not allowed`
- `model not found`
- `model does not exist`
- `not supported`
- `permission`
- `not authorized`
- `access denied`
- `forbidden`

rate limit, timeout, 429, 504 등 transient 오류는 fallback하지 않고 `LLMRateLimitError`로 올립니다.

## 15. NEXT_SQL_INFO update

관련 파일:

```text
server/repositories/sql/result_repository.py
```

`update_cycle_result()` 갱신 대상:

- `TO_SQL_TEXT`
- `TUNED_SQL`
- `TUNED_RESULT`
- `TUNED_TEST`
- `FORMATTED_SQL`
- `BIND_SQL`
- `BIND_SET`
- `TEST_SQL`
- `STATUS`
- `LOG`
- `UPD_TS`

특징:

- optional column은 존재할 때만 갱신합니다.
- VARCHAR 길이 제한이 있는 컬럼은 UTF-8 byte 기준으로 truncate합니다.
- CLOB 컬럼은 길이 제한 truncate 대상에서 제외합니다.

`reset_tuning_state()`:

```text
TUNED_SQL = NULL
TUNED_TEST = NULL
TUNED_RESULT = NULL
BLOCK_RAG_CONTENT = NULL
```

conversion fail row를 재처리할 때 이전 tuning 결과가 섞이지 않도록 초기화합니다.

## 16. End-to-end scenarios

### 16.1 정상 SELECT conversion + tuning + formatting

```text
Supervisor poll
  -> SQL Conversion job 선택
  -> run_sql_conversion
      -> BATCH_CNT + 1
      -> SQL_LENGTH/MAP_TYPE 저장
      -> mapping readiness 확인
      -> TO-BE SQL 생성
      -> bind SQL 생성/실행
      -> BIND_SET 생성
      -> test SQL 생성/실행
      -> row count 검증 PASS
      -> STATUS = CONVERSION-PASS
      -> TUNED_TEST = READY
  -> run_sql_tuning
      -> SQL_TUNING RAG 조회
      -> BLOCK_RAG_CONTENT 저장
      -> TUNED_SQL/TUNED_RESULT 생성
      -> baseline TO-BE vs tuned SQL test
      -> TUNED_TEST = TUNING-PASS
      -> FORMATTED_SQL 생성
```

### 16.2 TOBE_CORRECT_SQL 포함

```text
TOBE_CORRECT_SQL 있음
  -> TO-BE LLM 호출 생략
  -> state.tobe_sql = TOBE_CORRECT_SQL
  -> bind/test stage는 정상 진행
```

`TOBE_CORRECT_SQL`이 있다는 이유만으로 conversion이 성공 처리되지는 않습니다. 이후 bind/test stage에서 실패하면 `FAIL-BIND` 또는 `FAIL-TEST`가 저장됩니다.

### 16.3 non-SELECT

```text
TAG_KIND != SELECT
  -> TO-BE SQL 생성 또는 TOBE_CORRECT_SQL 사용
  -> conversion success
  -> STATUS = CONVERSION-PASS
  -> TUNED_TEST = READY
```

이후 tuning:

```text
TUNED_TEST=READY, STATUS=CONVERSION-PASS
  -> tuning rule 적용
  -> validation skip
  -> TUNED_TEST = PASS_NON_SELECT
  -> FORMATTED_SQL 생성
```

### 16.4 target mapping 미준비

```text
SQL Conversion 시작
  -> get_unready_target_tables(TARGET_TABLE)
  -> 매칭 mapping 없거나 mapping STATUS != PASS
  -> STATUS = NA
  -> LOG = 'NA reason=TARGET_MAPPING_NOT_READY: ...'
  -> NEXT_SQL_LOG ERROR/NA
  -> conversion 종료
```

### 16.5 tuning 실패 후 재시도

```text
TUNED_TEST=READY
  -> tuning attempt 1
  -> tuned test FAIL-TEST
  -> last_error = TUNED_TEST_VALIDATION_FAIL: ...
  -> tuning attempt 2
  -> prompt에 last_error 전달
  -> 성공하면 TUNING-PASS
  -> 계속 실패하면 FAIL-TEST
```

## 17. 화면 관련 로직

### 17.1 Dashboard

상태 normalize:

```text
legacy PASS -> 화면 문맥에 따라 CONVERSION-PASS 또는 TUNING-PASS
TUNING_PASS -> TUNING-PASS
FAIL-* -> fail badge
NA -> 별도 제외/skip 계열
```

Fail analysis:

```text
SQL Conversion:
  STATUS IN ('FAIL', 'FAIL-TOBE', 'FAIL-BIND', 'FAIL-TEST')

SQL Tuning:
  TUNED_TEST IN ('FAIL', 'FAIL-TUNED', 'FAIL-BIND', 'FAIL-TEST')
```

### 17.2 XML Export

```text
get_xml_export_sqls()
  -> SPACE_NM, TAG_KIND, SQL_ID, TUNED_TEST, FORMATTED_SQL 조회
  -> XML 생성 기준 = FORMATTED_SQL
  -> pass 조건:
       TUNED_TEST IN (
         'TUNING-PASS',
         'TUNING_PASS',
         legacy 'PASS',
         'PASS_NON_SELECT'
       )
       AND FORMATTED_SQL exists
```

namespace에 fail이 있으면 해당 namespace 다운로드를 비활성화합니다.

### 17.3 RAG Rule Manager

```text
app/pages/rag_manager_page.py
  -> NEXT_MIG_RAG_INFO list/top hit 조회
  -> CATEGORY / RULE_TYPE / keyword filter
  -> rule 생성, 수정, 삭제
```

입력 필드:

- `CATEGORY`
- `RULE_TYPE`
- `USE_YN`
- `SOURCE_TABLES`
- `GUIDANCE_TEXT`
- `SOURCE_SQL`
- `TARGET_SQL`

SEARCH rule은 `SOURCE_SQL`이 필요합니다.

## 18. 운영 확인 포인트

- `NEXT_SQL_INFO.TARGET_TABLE`은 RAG `SOURCE_TABLES` 필터의 유일한 job table source입니다.
- SQL 본문 파싱으로 RAG table filter를 보강하지 않습니다.
- `EDIT_FR_SQL`이 있으면 conversion과 conversion RAG 검색 모두 `EDIT_FR_SQL`을 기준으로 합니다.
- `NEXT_SQL_INFO.LOG`는 최신 요약이고, 상세 stage 이력은 `NEXT_SQL_LOG`를 봐야 합니다.
- `BLOCK_RAG_CONTENT`에는 tuning SEARCH RAG 결과 원문이 저장됩니다.
- `HIT_CNT`는 실제 프롬프트에 포함된 SEARCH rule만 증가합니다.
- `FAIL-TOBE`는 conversion 전용입니다. tuning fail analysis에는 포함하지 않습니다.
