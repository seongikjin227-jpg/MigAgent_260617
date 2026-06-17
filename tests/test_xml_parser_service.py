import tempfile
import unittest
from pathlib import Path

from server.services.sql.xml_parser_service import (
    _extract_target_tables_from_sql,
    parse_single_mapper_xml,
)


class XmlParserServiceTest(unittest.TestCase):
    def _parse_mapper_sql(self, mapper_body: str) -> str:
        mapper_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="sample.Mapper">
  {mapper_body}
</mapper>
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            xml_path = Path(tmp_dir) / "sample.xml"
            xml_path.write_text(mapper_xml, encoding="utf-8")
            items = parse_single_mapper_xml(xml_path)

        self.assertEqual(len(items), 1)
        return items[0].fr_sql_text

    def test_if_tail_text_is_not_duplicated(self):
        sql_text = self._parse_mapper_sql(
            """
<select id="selectWithIf">
  SELECT A
  FROM B
  WHERE 1=1
  <if test="cond != null">
    AND C = #{cond}
  </if>
  ORDER BY D
</select>
"""
        )

        self.assertEqual(sql_text.count("ORDER BY D"), 1)

    def test_foreach_tail_closing_parenthesis_is_not_duplicated(self):
        sql_text = self._parse_mapper_sql(
            """
<select id="selectWithForeach">
  SELECT A
  FROM B
  WHERE ID IN (
  <foreach collection="ids" item="id" separator=",">
    #{id}
  </foreach>
  )
</select>
"""
        )

        self.assertEqual(sql_text.rstrip().count(")"), 1)

    def test_choose_tail_order_by_is_not_duplicated(self):
        sql_text = self._parse_mapper_sql(
            """
<select id="selectWithChoose">
  SELECT A
  FROM B
  <choose>
    <when test="name != null">
      WHERE NAME = #{name}
    </when>
    <otherwise>
      WHERE 1=1
    </otherwise>
  </choose>
  ORDER BY A
</select>
"""
        )

        self.assertEqual(sql_text.count("ORDER BY A"), 1)

    def test_extract_target_tables_in_join_inline_view(self):
        sql_text = """
            SELECT A.ID, B.CNT
            FROM TB_MAIN A
            JOIN (
                SELECT REF_ID, COUNT(*) CNT
                FROM TB_DETAIL
                WHERE STATUS = 'Y'
                GROUP BY REF_ID
            ) B ON A.ID = B.REF_ID
        """

        self.assertEqual(
            _extract_target_tables_from_sql(sql_text),
            ["TB_MAIN", "TB_DETAIL"],
        )

    def test_extract_target_tables_in_nested_subquery(self):
        sql_text = """
            SELECT *
            FROM TB_OUTER O
            WHERE EXISTS (
                SELECT 1
                FROM (
                    SELECT ID
                    FROM TB_INNER
                ) X
                WHERE X.ID = O.ID
            )
        """

        self.assertEqual(
            _extract_target_tables_from_sql(sql_text),
            ["TB_OUTER", "TB_INNER"],
        )

    def test_extract_target_tables_ignores_cte_name_but_keeps_cte_source(self):
        sql_text = """
            WITH V_DATA AS (
                SELECT ID
                FROM TB_SOURCE
            )
            SELECT *
            FROM V_DATA V
            JOIN TB_LOOKUP L ON L.ID = V.ID
        """

        self.assertEqual(
            _extract_target_tables_from_sql(sql_text),
            ["TB_LOOKUP", "TB_SOURCE"],
        )


if __name__ == "__main__":
    unittest.main()
