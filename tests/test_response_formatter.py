"""Unit tests for epicor_mcp.response.formatter."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from epicor_mcp.response.formatter import (
    BLOAT_KEYS,
    _collect_rows_lists,
    _find_largest_rows,
    _is_empty,
    _wire_size_bytes,
    compute_stats,
    format_response,
    offload_response,
    purge_stale_offloads,
    rows_to_csv,
    strip_bloat,
    truncate_and_summarize,
)


def _settings(
    *,
    max_bytes: int = 900_000,
    keep: int = 20,
    top_k: int = 10,
    drop_empty: bool = True,
    offload_dir=None,
    preview_rows: int = 5,
    retention_hours: int = 24,
    public_base_url: str = "https://test.example.com",
):
    """Minimal settings stand-in — format_response uses getattr."""
    return SimpleNamespace(
        response_max_bytes=max_bytes,
        response_truncate_keep=keep,
        response_stats_top_k=top_k,
        response_drop_empty=drop_empty,
        response_offload_dir=offload_dir,
        response_offload_preview_rows=preview_rows,
        response_offload_retention_hours=retention_hours,
        response_public_base_url=public_base_url,
    )


# ---------------------------------------------------------------------------
# strip_bloat
# ---------------------------------------------------------------------------


class TestStripBloat:
    def test_removes_bloat_keys(self):
        row = {
            "JobNum": "T001",
            "RowMod": "",
            "SysRowID": "abc-123",
            "SysRevID": 42,
            "BitFlag": 0,
            "RowIdent": "guid",
            "OpenJob": True,
        }
        cleaned = strip_bloat(row)
        assert "JobNum" in cleaned
        assert "OpenJob" in cleaned
        for k in BLOAT_KEYS:
            assert k not in cleaned

    def test_removes_odata_prefixed_keys(self):
        obj = {
            "value": [1, 2],
            "@odata.context": "https://host/$metadata",
            "@odata.count": 42,
        }
        cleaned = strip_bloat(obj)
        assert cleaned == {"value": [1, 2]}

    def test_drop_empty_removes_none_empty_string_empty_list(self):
        obj = {
            "a": None,
            "b": "",
            "c": [],
            "d": 0,
            "e": False,
            "f": "kept",
        }
        cleaned = strip_bloat(obj, drop_empty=True)
        assert cleaned == {"d": 0, "e": False, "f": "kept"}

    def test_drop_empty_false_keeps_empties(self):
        obj = {"a": None, "b": "", "c": [], "d": 1}
        cleaned = strip_bloat(obj, drop_empty=False)
        assert cleaned == obj

    def test_preserve_row_meta_keeps_bloat_keys(self):
        row = {"PONum": 100, "RowMod": "U", "SysRowID": "abc"}
        cleaned = strip_bloat(row, preserve_row_meta=True, drop_empty=False)
        assert cleaned == row

    def test_recurses_into_nested_structures(self):
        obj = {
            "JobHead": [
                {"JobNum": "T001", "RowMod": "", "SysRowID": "a"},
                {"JobNum": "T002", "RowMod": "", "SysRowID": "b"},
            ],
            "@odata.context": "junk",
        }
        cleaned = strip_bloat(obj)
        assert "@odata.context" not in cleaned
        for row in cleaned["JobHead"]:
            assert "RowMod" not in row
            assert "SysRowID" not in row
            assert "JobNum" in row

    def test_idempotent(self):
        obj = {"x": 1, "RowMod": "A", "@odata.type": "#Erp.JobHead"}
        once = strip_bloat(obj)
        twice = strip_bloat(once)
        assert once == twice == {"x": 1}

    def test_passthrough_scalars(self):
        assert strip_bloat(42) == 42
        assert strip_bloat("hello") == "hello"
        assert strip_bloat(None) is None

    def test_does_not_mutate_input(self):
        original = {"x": 1, "RowMod": "A"}
        snapshot = dict(original)
        strip_bloat(original)
        assert original == snapshot


class TestIsEmpty:
    def test_empty_values(self):
        for v in (None, "", []):
            assert _is_empty(v)

    def test_non_empty_values(self):
        for v in (0, False, "x", [0], {}):
            # dict is not in the empty set — only None/""/[]
            assert not _is_empty(v)


# ---------------------------------------------------------------------------
# rows_to_csv
# ---------------------------------------------------------------------------


class TestRowsToCsv:
    def test_empty_rows(self):
        csv_text, columns = rows_to_csv([])
        assert csv_text == ""
        assert columns == []

    def test_basic_rows(self):
        rows = [
            {"a": 1, "b": "x"},
            {"a": 2, "b": "y"},
        ]
        csv_text, columns = rows_to_csv(rows)
        assert columns == ["a", "b"]
        lines = csv_text.strip().splitlines()
        assert lines[0] == "a,b"
        assert lines[1] == "1,x"
        assert lines[2] == "2,y"

    def test_preserves_first_seen_column_order(self):
        rows = [
            {"z": 1, "a": 2},
            {"b": 3, "a": 4},
        ]
        _, columns = rows_to_csv(rows)
        assert columns == ["z", "a", "b"]

    def test_missing_keys_render_as_empty(self):
        rows = [{"a": 1, "b": 2}, {"a": 3}]
        csv_text, _ = rows_to_csv(rows)
        assert "3," in csv_text  # second row has empty b

    def test_nested_cell_is_json_encoded(self):
        import csv as _csv
        import io as _io

        rows = [{"a": 1, "meta": {"k": "v", "n": 2}}]
        csv_text, _cols = rows_to_csv(rows)
        parsed = list(_csv.reader(_io.StringIO(csv_text)))
        assert parsed[0] == ["a", "meta"]
        # The meta cell round-trips to valid JSON for the original dict.
        assert json.loads(parsed[1][1]) == {"k": "v", "n": 2}

    def test_list_cell_is_json_encoded(self):
        import csv as _csv
        import io as _io

        rows = [{"a": 1, "tags": ["x", "y"]}]
        csv_text, _cols = rows_to_csv(rows)
        parsed = list(_csv.reader(_io.StringIO(csv_text)))
        assert json.loads(parsed[1][1]) == ["x", "y"]

    def test_none_becomes_empty(self):
        rows = [{"a": 1, "b": None}]
        csv_text, _ = rows_to_csv(rows)
        assert csv_text.strip().splitlines()[1] == "1,"

    def test_bool_lowercase(self):
        rows = [{"a": True, "b": False}]
        csv_text, _ = rows_to_csv(rows)
        assert csv_text.strip().splitlines()[1] == "true,false"

    def test_tsv_delimiter(self):
        rows = [{"a": 1, "b": 2}]
        csv_text, _ = rows_to_csv(rows, delimiter="\t")
        assert "\t" in csv_text.splitlines()[0]


# ---------------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------------


class TestComputeStats:
    def test_empty_rows(self):
        assert compute_stats([]) == {}

    def test_numeric_column(self):
        rows = [{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}]
        stats = compute_stats(rows)
        assert stats["x"] == {
            "count": 4,
            "min": 1,
            "max": 4,
            "sum": 10,
            "avg": 2.5,
        }

    def test_numeric_with_nulls(self):
        rows = [{"x": 1}, {"x": None}, {"x": 3}]
        stats = compute_stats(rows)
        assert stats["x"]["count"] == 2
        assert stats["x"]["min"] == 1
        assert stats["x"]["max"] == 3

    def test_low_cardinality_string(self):
        rows = [{"status": "OPEN"}] * 7 + [{"status": "CLOSED"}] * 3
        stats = compute_stats(rows, top_k=5)
        assert stats["status"]["distinct_count"] == 2
        top = dict(stats["status"]["top"])
        assert top["OPEN"] == 7
        assert top["CLOSED"] == 3

    def test_high_cardinality_string_uses_distinct_only(self):
        rows = [{"id": f"JOB-{i}"} for i in range(50)]
        stats = compute_stats(rows, top_k=10)
        assert "top" not in stats["id"]
        assert stats["id"]["distinct_count"] == 50

    def test_nested_column_skipped(self):
        rows = [{"meta": {"k": 1}}, {"meta": {"k": 2}}]
        stats = compute_stats(rows)
        assert "meta" not in stats

    def test_all_null_column_skipped(self):
        rows = [{"x": None}, {"x": None}]
        stats = compute_stats(rows)
        assert "x" not in stats

    def test_bool_not_treated_as_numeric(self):
        rows = [{"flag": True}, {"flag": False}, {"flag": True}]
        stats = compute_stats(rows, top_k=5)
        assert "count" not in stats["flag"]
        assert "distinct_count" in stats["flag"]


# ---------------------------------------------------------------------------
# _find_largest_rows
# ---------------------------------------------------------------------------


class TestFindLargestRows:
    def test_top_level_list(self):
        obj = {"records": [{"a": 1}, {"a": 2}]}
        container, key, rows = _find_largest_rows(obj)
        assert rows == obj["records"]
        assert container is obj
        assert key == "records"

    def test_nested_deeper(self):
        obj = {
            "tiny": [{"a": 1}],
            "baq_results": {
                "Q1": {"records": [{"x": i} for i in range(10)]},
                "Q2": {"records": [{"x": 1}]},
            },
        }
        _, _, rows = _find_largest_rows(obj)
        assert rows is not None
        assert len(rows) == 10

    def test_no_match(self):
        container, key, rows = _find_largest_rows({"a": 1, "b": "x"})
        assert container is None
        assert key is None
        assert rows is None

    def test_list_of_scalars_skipped(self):
        obj = {"ids": [1, 2, 3], "rows": [{"a": 1}, {"a": 2}]}
        _, key, rows = _find_largest_rows(obj)
        assert key == "rows"
        assert len(rows) == 2


class TestCollectRowsLists:
    def test_returns_every_rows_list(self):
        obj = {
            "QuoteHed": [{"h": 1}],
            "QuoteDtl": [{"d": i} for i in range(5)],
            "QuoteAsm": [{"a": i} for i in range(3)],
        }
        collected = _collect_rows_lists(obj)
        keys = sorted(k for _, k, _ in collected)
        assert keys == ["QuoteAsm", "QuoteDtl", "QuoteHed"]

    def test_deeply_nested(self):
        obj = {
            "baq_results": {
                "Q1": {"records": [{"a": 1}, {"a": 2}]},
                "Q2": {"records": [{"b": 1}]},
            }
        }
        collected = _collect_rows_lists(obj)
        assert len(collected) == 2

    def test_empty_lists_excluded(self):
        obj = {"empty": [], "real": [{"a": 1}]}
        collected = _collect_rows_lists(obj)
        assert len(collected) == 1
        _, k, _ = collected[0]
        assert k == "real"


class TestWireSizeBytes:
    def test_accounts_for_json_escape(self):
        # A payload full of quotes and newlines should grow substantially
        # when measured in wire terms.
        payload = json.dumps({"records": [{"a": "x" * 50}] * 20}, indent=2)
        raw = len(payload.encode("utf-8"))
        wire = _wire_size_bytes(payload)
        # Wire includes our payload re-escaped + envelope overhead
        assert wire > raw
        # Should be roughly 15-50% larger for typical pretty-printed JSON
        assert wire < raw * 2

    def test_small_payload(self):
        assert _wire_size_bytes("hi") >= 80  # envelope overhead


# ---------------------------------------------------------------------------
# truncate_and_summarize
# ---------------------------------------------------------------------------


class TestTruncateAndSummarize:
    def test_truncates_top_level(self):
        result = {
            "records": [{"x": i} for i in range(100)],
            "record_count": 100,
        }
        out = truncate_and_summarize(
            result,
            records_key="records",
            actual_bytes=2_000_000,
            settings=_settings(max_bytes=900_000, keep=20),
        )
        assert out["truncated"] is True
        assert out["original_record_count"] == 100
        assert out["returned_record_count"] == 20
        assert len(out["records"]) == 20
        assert "stats" in out
        assert "note" in out

    def test_stats_computed_from_full_set(self):
        result = {"records": [{"v": i} for i in range(100)]}
        out = truncate_and_summarize(
            result,
            records_key="records",
            actual_bytes=2_000_000,
            settings=_settings(max_bytes=900_000, keep=5),
        )
        # sum 0..99 = 4950
        assert out["stats"]["v"]["sum"] == 4950
        assert out["stats"]["v"]["max"] == 99

    def test_falls_back_to_largest_nested_rows(self):
        result = {
            "baq_results": {
                "Q1": {"records": [{"x": i} for i in range(30)]},
            }
        }
        out = truncate_and_summarize(
            result,
            records_key=None,
            actual_bytes=2_000_000,
            settings=_settings(keep=10),
        )
        assert out["truncated"] is True
        assert len(result["baq_results"]["Q1"]["records"]) == 10
        assert out["original_record_count"] == 30

    def test_caps_every_child_table(self):
        """Multi-table Epicor datasets: ALL child lists capped, not just largest."""
        result = {
            "QuoteHed": [{"q": 1}],
            "QuoteDtl": [{"d": i} for i in range(100)],
            "QuoteAsm": [{"a": i} for i in range(50)],
            "QuoteOpr": [{"o": i} for i in range(40)],
        }
        out = truncate_and_summarize(
            result,
            records_key=None,
            actual_bytes=2_000_000,
            settings=_settings(keep=5),
        )
        # Every rows array gets capped, not just the biggest (QuoteDtl)
        assert len(result["QuoteDtl"]) == 5
        assert len(result["QuoteAsm"]) == 5
        assert len(result["QuoteOpr"]) == 5
        # QuoteHed already had 1 row — preserved
        assert len(result["QuoteHed"]) == 1
        # Stats computed from the primary (largest: QuoteDtl)
        assert out["original_record_count"] == 100
        assert out["returned_record_count"] == 5

    def test_no_rows_to_truncate_emits_error(self):
        result = {"only_scalar": 42}
        out = truncate_and_summarize(
            result,
            records_key=None,
            actual_bytes=2_000_000,
            settings=_settings(),
        )
        assert out["truncated"] is True
        assert "error" in out


# ---------------------------------------------------------------------------
# format_response — integration
# ---------------------------------------------------------------------------


class TestFormatResponse:
    def test_small_response_passes_through(self):
        result = {"records": [{"a": 1}, {"a": 2}], "record_count": 2}
        out = format_response(result, records_key="records", settings=_settings())
        data = json.loads(out)
        assert data["record_count"] == 2
        assert len(data["records"]) == 2
        assert data.get("truncated") is None

    def test_strips_bloat_from_rows(self):
        result = {
            "records": [
                {"JobNum": "T001", "RowMod": "", "SysRowID": "a", "@odata.id": "x"},
            ]
        }
        out = format_response(result, records_key="records", settings=_settings())
        data = json.loads(out)
        row = data["records"][0]
        assert "JobNum" in row
        for bad in ("RowMod", "SysRowID", "@odata.id"):
            assert bad not in row

    def test_csv_format_replaces_records(self):
        result = {
            "record_count": 2,
            "records": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}],
        }
        out = format_response(
            result, records_key="records", format="csv", settings=_settings()
        )
        data = json.loads(out)
        assert data["format"] == "csv"
        assert "rows_csv" in data
        assert "records" not in data
        assert "columns" in data
        assert data["record_count"] == 2

    def test_csv_smaller_than_json_for_wide_rows(self):
        result = {
            "records": [
                {f"col{i}": f"value-{j}-{i}" for i in range(8)}
                for j in range(50)
            ]
        }
        json_out = format_response(
            result.copy(),
            records_key="records",
            format="json",
            settings=_settings(),
        )
        csv_out = format_response(
            result.copy(),
            records_key="records",
            format="csv",
            settings=_settings(),
        )
        assert len(csv_out.encode()) < len(json_out.encode())

    def test_size_guard_triggers_truncation(self):
        result = {
            "records": [
                {"x": "a" * 200, "y": i} for i in range(500)
            ]
        }
        out = format_response(
            result,
            records_key="records",
            settings=_settings(max_bytes=10_000, keep=5),
        )
        data = json.loads(out)
        assert data["truncated"] is True
        assert data["returned_record_count"] == 5
        assert data["original_record_count"] == 500
        assert "KB tool-result limit" in data["note"]

    def test_strip_only_preserves_row_meta_and_skips_size_guard(self):
        large_row = {"x": "a" * 5000, "RowMod": "U", "SysRowID": "abc"}
        result = {
            "success": True,
            "action": "update",
            "result": {"POHeader": [large_row]},
        }
        out = format_response(
            result,
            records_key=None,
            strip_only=True,
            settings=_settings(max_bytes=1_000),
        )
        data = json.loads(out)
        row = data["result"]["POHeader"][0]
        # strip_only preserves RowMod / SysRowID
        assert row.get("RowMod") == "U"
        assert row.get("SysRowID") == "abc"
        # No truncation
        assert "truncated" not in data

    def test_non_dict_passthrough(self):
        out = format_response([1, 2, 3], records_key=None, settings=_settings())
        assert json.loads(out) == [1, 2, 3]

    def test_format_json_default_keeps_records_key(self):
        result = {"records": [{"a": 1}]}
        out = format_response(result, records_key="records", settings=_settings())
        data = json.loads(out)
        assert "records" in data
        assert "rows_csv" not in data

    def test_preserves_sibling_fields_with_csv(self):
        result = {
            "baq_id": "zFoo",
            "record_count": 2,
            "records": [{"a": 1}, {"a": 2}],
            "note": "hello",
        }
        out = format_response(
            result, records_key="records", format="csv", settings=_settings()
        )
        data = json.loads(out)
        assert data["baq_id"] == "zFoo"
        assert data["record_count"] == 2
        assert data["note"] == "hello"

    def test_size_guard_with_nested_records(self):
        big_rows = [{"x": "a" * 300} for _ in range(200)]
        result = {
            "dashboard": "D1",
            "baq_results": {
                "Q1": {"record_count": 200, "records": big_rows},
            },
        }
        out = format_response(
            result,
            records_key=None,
            settings=_settings(max_bytes=5_000, keep=3),
        )
        data = json.loads(out)
        assert data["truncated"] is True
        assert len(data["baq_results"]["Q1"]["records"]) == 3

    def test_multi_table_dataset_fits_after_truncation(self):
        """Quote-like multi-child dataset should truncate every table and fit budget."""
        result = {
            "QuoteHed": [{"QuoteNum": 300001, "CustNum": 101, "Comment": "x" * 200}],
            "QuoteDtl": [
                {"QuoteNum": 300001, "QuoteLine": i, "PartNum": f"P-{i}", "Desc": "d" * 100}
                for i in range(80)
            ],
            "QuoteAsm": [
                {"QuoteNum": 300001, "AsmSeq": i, "PartNum": f"A-{i}", "Ext": "e" * 80}
                for i in range(40)
            ],
            "QuoteOpr": [
                {"QuoteNum": 300001, "OprSeq": i, "OpCode": f"OP-{i}", "Ext": "o" * 80}
                for i in range(30)
            ],
            "QuoteMtl": [
                {"QuoteNum": 300001, "MtlSeq": i, "PartNum": f"M-{i}", "Ext": "m" * 80}
                for i in range(60)
            ],
        }
        settings = _settings(max_bytes=10_000, keep=5)
        out = format_response(result, records_key=None, settings=settings)
        wire = _wire_size_bytes(out)
        # Fits the budget
        assert wire <= settings.response_max_bytes
        data = json.loads(out)
        # Truncation fired
        assert data.get("truncated") is True
        # Every child table capped
        for tbl in ("QuoteDtl", "QuoteAsm", "QuoteOpr", "QuoteMtl"):
            assert len(data[tbl]) <= 5
        # Header (1 row) preserved
        assert len(data["QuoteHed"]) == 1

    def test_offload_pointer_returned_when_over_budget(self, tmp_path):
        """Oversize response writes full payload to disk and returns a small pointer."""
        result = {
            "QuoteHed": [{"QuoteNum": 300001, "CustNum": 101}],
            "QuoteDtl": [
                {"QuoteNum": 300001, "QuoteLine": i, "PartNum": f"P-{i}"}
                for i in range(100)
            ],
        }
        settings = _settings(
            max_bytes=1_000, keep=10, offload_dir=tmp_path, preview_rows=3
        )
        out = format_response(result, records_key=None, settings=settings)
        data = json.loads(out)

        assert data["offloaded"] is True
        # Pointer should NOT expose a server-side file path — the URL is
        # the only access capability because Claude's sandbox isn't the
        # server's filesystem.
        assert "file_path" not in data
        # Pointer itself should be very small
        assert len(out.encode()) < 10_000

        # Preview surfaces first N rows of primary list
        assert data["primary_list"]["name"] == "QuoteDtl"
        assert data["primary_list"]["total_rows"] == 100
        assert len(data["primary_list"]["preview"]) == 3

        # Row counts cover every table
        assert data["row_counts"]["QuoteHed"] == 1
        assert data["row_counts"]["QuoteDtl"] == 100

        # Usage hint steers toward the right follow-up tool and warns
        # against treating the file as local.  It mentions all three
        # MCP-side options plus the "just use epicor_query" preference.
        usage = data["usage"]
        assert "epicor_grep_offloaded" in usage
        assert "epicor_filter_offloaded" in usage
        assert "epicor_query" in usage
        assert "not on your sandbox" in usage.lower() or "local path" in usage.lower()
        assert data["url"].startswith("https://test.example.com/response-files/")

        # Underlying file written to disk for server-side audit/debug.
        # Recover it by listing the offload dir — the pointer deliberately
        # doesn't surface the path.
        on_disk = list(tmp_path.glob("*.json"))
        assert len(on_disk) == 1
        with open(on_disk[0]) as f:
            full = json.load(f)
        assert len(full["QuoteDtl"]) == 100

    def test_offload_disabled_falls_back_to_truncation(self):
        """Empty offload_dir => legacy truncate-and-summarise path runs."""
        result = {"records": [{"x": "a" * 100} for _ in range(500)]}
        settings = _settings(
            max_bytes=5_000, keep=5, offload_dir=None
        )
        out = format_response(result, records_key="records", settings=settings)
        data = json.loads(out)
        assert "offloaded" not in data
        assert data.get("truncated") is True

    def test_offload_write_failure_falls_back_to_truncation(self, tmp_path):
        """If the offload dir can't be written, truncation takes over."""
        # Point at a path we can't create: nested under a file
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        bad_dir = blocker / "subdir"  # can't mkdir under a regular file

        result = {"records": [{"x": "a" * 100} for _ in range(500)]}
        settings = _settings(max_bytes=5_000, keep=5, offload_dir=bad_dir)
        out = format_response(result, records_key="records", settings=settings)
        data = json.loads(out)
        # Never offloaded; fell back to truncation
        assert "offloaded" not in data
        assert data.get("truncated") is True


class TestOffloadResponse:
    def test_writes_file_and_returns_pointer(self, tmp_path):
        result = {"records": [{"a": i, "b": f"val-{i}"} for i in range(20)]}
        payload = json.dumps(result, indent=2)
        out = offload_response(
            result, payload, offload_dir=tmp_path, settings=_settings(preview_rows=2)
        )
        data = json.loads(out)
        assert data["offloaded"] is True
        # File exists on disk (server-side), but the path isn't in the pointer
        assert len(list(tmp_path.glob("*.json"))) == 1
        assert "file_path" not in data
        assert data["size_bytes"] == len(payload.encode("utf-8"))
        assert data["primary_list"]["total_rows"] == 20
        assert len(data["primary_list"]["preview"]) == 2
        assert data["primary_list"]["name"] == "records"

    def test_filename_uses_full_uuid_for_entropy(self, tmp_path):
        """128-bit filenames — the URL is the access capability."""
        payload = json.dumps({"records": [{"a": 1}]})
        offload_response(
            {"records": [{"a": 1}]}, payload,
            offload_dir=tmp_path, settings=_settings(),
        )
        on_disk = list(tmp_path.glob("*.json"))
        assert len(on_disk) == 1
        name = on_disk[0].name
        # YYYYMMDDTHHMMSSZ-<32 hex>.json
        import re as _re
        assert _re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{32}\.json$", name)

    def test_url_built_from_public_base_url(self, tmp_path):
        settings = _settings(public_base_url="https://mcp.example.com/")
        out = offload_response(
            {"records": [{"a": 1}]}, json.dumps({"records": [{"a": 1}]}),
            offload_dir=tmp_path, settings=settings,
        )
        data = json.loads(out)
        assert data["url"].startswith("https://mcp.example.com/response-files/")
        # Trailing slash on the base URL is stripped
        assert "//response-files" not in data["url"]

    def test_url_absent_when_base_url_empty(self, tmp_path):
        settings = _settings(public_base_url="")
        out = offload_response(
            {"records": [{"a": 1}]}, json.dumps({"records": [{"a": 1}]}),
            offload_dir=tmp_path, settings=settings,
        )
        data = json.loads(out)
        assert "url" not in data
        assert "curl" not in data["usage"]

    def test_creates_offload_dir_if_missing(self, tmp_path):
        nested = tmp_path / "does" / "not" / "exist"
        result = {"records": [{"a": 1}]}
        payload = json.dumps(result)
        offload_response(
            result, payload, offload_dir=nested, settings=_settings()
        )
        assert nested.exists() and nested.is_dir()


class TestPurgeStaleOffloads:
    def test_deletes_old_files(self, tmp_path):
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text("{}")
        new.write_text("{}")
        # Backdate the old file 48 hours
        import os
        t = time.time() - 48 * 3600
        os.utime(old, (t, t))

        deleted = purge_stale_offloads(tmp_path, retention_hours=24)
        assert deleted == 1
        assert not old.exists()
        assert new.exists()

    def test_noop_when_retention_zero(self, tmp_path):
        old = tmp_path / "old.json"
        old.write_text("{}")
        import os
        t = time.time() - 48 * 3600
        os.utime(old, (t, t))

        assert purge_stale_offloads(tmp_path, retention_hours=0) == 0
        assert old.exists()

    def test_noop_when_dir_missing(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        assert purge_stale_offloads(missing, retention_hours=24) == 0


    def test_wire_size_triggers_truncation_when_raw_fits(self):
        """Payload with heavy JSON-escape overhead should still trigger the guard."""
        # Lots of strings with quotes → big wire expansion
        rows = [{"name": 'He said "hi"\n' * 20, "n": i} for i in range(400)]
        result = {"records": rows}
        # Raw size is modest, wire size much bigger
        raw_out = json.dumps(result, indent=2)
        raw = len(raw_out.encode("utf-8"))
        wire = _wire_size_bytes(raw_out)
        assert wire > raw  # sanity

        # Budget that fits raw but NOT wire — truncation must still fire
        budget = (raw + wire) // 2
        out = format_response(
            result,
            records_key="records",
            settings=_settings(max_bytes=budget, keep=10),
        )
        assert _wire_size_bytes(out) <= budget
        data = json.loads(out)
        assert data.get("truncated") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
