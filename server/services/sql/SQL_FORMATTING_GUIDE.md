# SQL Formatting Guide

DB에 저장되는 SQL을 사람이 읽기 쉽게 만들기 위한 포맷팅 기준입니다. 실행 의미를 바꾸지 않는 줄바꿈과 들여쓰기만 허용합니다.

## 적용 대상

- `TO_SQL_TEXT`
- `TUNED_SQL`
- `BIND_SQL`
- `TEST_SQL`

현재 자동 적용은 하지 않습니다. 필요할 때 `sql_formatting_service.py`의 함수를 호출해서 결과를 확인한 뒤 저장 경로에 연결합니다.

## 기본 규칙

- SQL 키워드는 대문자를 유지합니다.
- 한 줄에 전체 SQL을 길게 저장하지 않습니다.
- 주요 절은 새 줄에서 시작합니다.
- 들여쓰기는 공백 4칸을 사용합니다.
- 세미콜론은 저장하지 않습니다.
- MyBatis 태그와 바인드 표현식은 의미가 바뀌지 않게 원문을 유지합니다.

## 줄바꿈 기준

아래 키워드는 새 줄에서 시작합니다.

```text
SELECT
FROM
WHERE
GROUP BY
HAVING
ORDER BY
UNION
UNION ALL
JOIN
LEFT JOIN
RIGHT JOIN
INNER JOIN
OUTER JOIN
ON
AND
OR
CASE
WHEN
THEN
ELSE
END
```

## SELECT 컬럼

SELECT list의 최상위 comma는 줄바꿈합니다.

```sql
SELECT
    A.COL1,
    A.COL2,
    COUNT(*) AS CNT
FROM TABLE_A A
```

## TEST_SQL

`TEST_SQL`은 FROM SQL과 TO-BE SQL의 row count를 비교하기 때문에 중첩 쿼리가 길어집니다. 최소한 아래 구조가 보이도록 저장합니다.

```sql
SELECT
    1 AS CASE_NO,
    (
        SELECT COUNT(*)
        FROM (...)
    ) AS FROM_COUNT,
    (
        SELECT COUNT(*)
        FROM (...)
    ) AS TO_COUNT
FROM DUAL
UNION ALL
SELECT
    2 AS CASE_NO,
    ...
FROM DUAL
```

## 수동 사용 예시

```python
from server.services.sql.sql_formatting_service import format_sql_for_storage

formatted_test_sql = format_sql_for_storage(state.test_sql)
```

여러 컬럼을 한 번에 확인할 때:

```python
from server.services.sql.sql_formatting_service import format_sql_fields_for_storage

formatted = format_sql_fields_for_storage(
    tobe_sql=state.tobe_sql,
    tuned_sql=state.tuned_sql,
    bind_sql=state.bind_sql,
    test_sql=state.test_sql,
)
```

자동 저장에 연결하려면 `server/repositories/sql/result_repository.py`의 `update_cycle_result()`에서 payload 생성 전에 위 함수를 적용합니다.
