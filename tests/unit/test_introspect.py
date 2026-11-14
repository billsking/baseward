"""Pure metadata-lookup functions: tokenization, scoring, alias resolution, and query building."""

from types import SimpleNamespace

from app.mcp import introspect


def test_tokenize_splits_and_caps_terms() -> None:
    assert introspect.tokenize(None) == []
    assert introspect.tokenize("  ") == []
    assert introspect.tokenize("批次") == ["批次"]
    assert introspect.tokenize(" 批次   卡密 ") == ["批次", "卡密"]
    assert introspect.tokenize("a b c d e f g") == ["a", "b", "c", "d", "e"]


def test_like_pattern_escapes_wildcards() -> None:
    assert introspect.like_pattern("a%b_c!d") == "%a!%b!_c!!d%"


def test_score_ranks_name_match_above_comment() -> None:
    terms = ["coupon"]
    exact = introspect.score(terms, "coupon", "")
    prefix = introspect.score(terms, "coupon_code", "")
    substring = introspect.score(terms, "t_coupon", "")
    comment_only = introspect.score(terms, "card", "优惠券 coupon")
    assert exact > prefix > substring > comment_only > 0
    assert introspect.score(terms, "orders", "") == 0


def test_score_uses_table_name_for_columns() -> None:
    terms = ["coupon"]
    hit = introspect.score(terms, "code", "", table_name="coupon_code")
    assert hit > 0
    assert introspect.score(terms, "code", "", table_name="orders") == 0


def test_resolve_alias_exact_name() -> None:
    item = SimpleNamespace(name="demo", remark="示例库")
    assert introspect.resolve_alias("demo", [item]) == (item, [])


def test_resolve_alias_by_remark_fragment() -> None:
    item = SimpleNamespace(name="demo", remark="示例库")
    other = SimpleNamespace(name="mall", remark="商城库")
    matched, ambiguous = introspect.resolve_alias("示例", [item, other])
    assert matched is item
    assert ambiguous == []


def test_resolve_alias_ambiguous_returns_candidates() -> None:
    first = SimpleNamespace(name="a", remark="示例库")
    second = SimpleNamespace(name="b", remark="示例库备份")
    matched, ambiguous = introspect.resolve_alias("示例", [first, second])
    assert matched is None
    assert {item.name for item in ambiguous} == {"a", "b"}


def test_resolve_alias_miss_and_empty() -> None:
    item = SimpleNamespace(name="demo", remark=None)
    assert introspect.resolve_alias("不存在", [item]) == (None, [])
    assert introspect.resolve_alias("", [item]) == (None, [])
    assert introspect.resolve_alias("demo", [item]) == (item, [])


def test_search_tables_query_without_scope() -> None:
    query = introspect.search_tables_query(["user"], None, None)
    assert query is not None
    sql, params = query
    assert "LOWER(t.table_schema) NOT IN" in sql
    assert sql.count("LIKE %s") == 3
    assert "ORDER BY" in sql
    assert params == ["%user%", "%user%", "%user%", introspect.FETCH_LIMIT]


def test_search_tables_query_respects_scope_and_database() -> None:
    query = introspect.search_tables_query(
        ["user", "phone"], "shop", [("shop", "users")]
    )
    assert query is not None
    sql, params = query
    assert sql.count("LIKE %s") == 6
    assert params[0] == "shop"
    assert params[1:3] == ["shop", "users"]
    assert params[3:6] == ["%user%", "%user%", "%user%"]
    assert params[6:9] == ["%phone%", "%phone%", "%phone%"]
    assert params[-1] == introspect.FETCH_LIMIT
    assert introspect.search_tables_query(["x"], None, []) is None


def test_search_columns_query_with_column_scope() -> None:
    query = introspect.search_columns_query(
        ["phone"],
        None,
        [("shop", "users")],
        ([("shop", "users")], [("shop", "users", "phone")], []),
    )
    assert query is not None
    sql, params = query
    assert "c.column_comment" in sql
    assert "t.table_comment" in sql
    assert "(c.table_schema, c.table_name, c.column_name) IN" in sql
    assert "shop" in params and "users" in params and "phone" in params
    assert introspect.search_columns_query(["x"], None, [], None) is None


def test_describe_column_tables_query_needs_terms() -> None:
    assert introspect.describe_column_tables_query([], None, None) is None
    query = introspect.describe_column_tables_query(["卡密"], None, None)
    assert query is not None
    sql, params = query
    assert "SELECT DISTINCT" in sql
    assert params[:5] == ["%卡密%"] * 5


def test_columns_for_tables_query_filters_scope() -> None:
    assert introspect.columns_for_tables_query([], None, limit=10) is None

    query = introspect.columns_for_tables_query(
        [("shop", "users")],
        ([("shop", "users")], [("shop", "orders", "amount")], []),
        limit=10,
    )
    assert query is not None
    sql, params = query
    assert "(c.table_schema, c.table_name) IN ((%s,%s))" in sql
    # orders is not in this table list, so its explicit column condition must be dropped
    assert "(c.table_schema, c.table_name, c.column_name)" not in sql
    assert params == ["shop", "users", "shop", "users", 10]

    empty_scope = introspect.columns_for_tables_query(
        [("shop", "users")], ([], [("shop", "orders", "amount")], []), limit=10
    )
    assert empty_scope is None


def test_whole_database_grants_include_all_tables_and_columns() -> None:
    """A whole-database grant must not degrade into an explicit table list: the database dimension must reach the SQL."""
    tables = introspect.search_tables_query(
        ["user"], None, [], unrestricted_databases=["trade"]
    )
    assert tables is not None
    sql, params = tables
    assert "t.table_schema IN (%s)" in sql
    assert params[0] == "trade"

    assert introspect.search_tables_query(["x"], None, []) is None

    columns = introspect.search_columns_query(
        ["user"], None, [], ([], [], ["trade"]), unrestricted_databases=["trade"]
    )
    assert columns is not None
    column_sql, column_params = columns
    assert "c.table_schema IN (%s)" in column_sql
    assert column_params[0] == "trade"

    describe = introspect.describe_column_tables_query(
        ["user"], None, [], unrestricted_databases=["trade"]
    )
    assert describe is not None
    assert "c.table_schema IN (%s)" in describe[0]

    scoped_columns = introspect.columns_for_tables_query(
        [("trade", "orders"), ("shop", "users")],
        ([("shop", "users")], [], ["trade"]),
        limit=10,
    )
    assert scoped_columns is not None
    scoped_sql, scoped_params = scoped_columns
    assert "c.table_schema IN (%s)" in scoped_sql
    # trade in refs once + the whole-database condition once; shop has no whole-database grant and is not filtered
    assert scoped_params.count("trade") == 2
