"""
Unit tests for StreamingJsonFileParser.

Covers: normal response, chunked delivery, truncation mid-file,
truncation mid-escape, multi-file ordering, special characters,
empty files array.
"""

from __future__ import annotations

import json

from phalanx.agents.streaming_parser import StreamingJsonFileParser

# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_response(
    summary: str,
    commit_message: str,
    files: list[dict],
) -> str:
    """Build a well-formed builder JSON response string."""
    return json.dumps(
        {
            "summary": summary,
            "commit_message": commit_message,
            "files": files,
        }
    )


def _feed_all(parser: StreamingJsonFileParser, text: str) -> list[dict]:
    """Feed entire text in one chunk."""
    return parser.feed(text)


def _feed_chunked(parser: StreamingJsonFileParser, text: str, size: int = 32) -> list[dict]:
    """Feed text in small chunks of `size` bytes."""
    results: list[dict] = []
    for i in range(0, len(text), size):
        results.extend(parser.feed(text[i : i + size]))
    return results


# ── Normal complete response ──────────────────────────────────────────────────


class TestNormalResponse:
    def test_single_file_emitted(self):
        parser = StreamingJsonFileParser()
        text = _build_response(
            "Built Button component",
            "feat: add Button component",
            [
                {
                    "path": "src/Button.tsx",
                    "action": "create",
                    "content": "export const Button = () => <button/>;",
                }
            ],
        )
        files = _feed_all(parser, text)
        assert len(files) == 1
        assert files[0]["path"] == "src/Button.tsx"
        assert files[0]["content"] == "export const Button = () => <button/>;"

    def test_summary_and_commit_extracted(self):
        parser = StreamingJsonFileParser()
        text = _build_response(
            "Built Button component",
            "feat: add Button",
            [{"path": "src/Button.tsx", "action": "create", "content": "x"}],
        )
        _feed_all(parser, text)
        assert parser.summary == "Built Button component"
        assert parser.commit_message == "feat: add Button"

    def test_multi_file_all_emitted(self):
        parser = StreamingJsonFileParser()
        files = [
            {"path": "src/A.tsx", "action": "create", "content": "A"},
            {"path": "src/B.tsx", "action": "create", "content": "B"},
            {"path": "src/C.tsx", "action": "create", "content": "C"},
        ]
        text = _build_response("Three files", "feat: add A B C", files)
        result = _feed_all(parser, text)
        assert len(result) == 3
        assert [f["path"] for f in result] == ["src/A.tsx", "src/B.tsx", "src/C.tsx"]

    def test_files_emitted_in_order(self):
        parser = StreamingJsonFileParser()
        paths = [f"src/Component{i}.tsx" for i in range(5)]
        files = [
            {"path": p, "action": "create", "content": f"content_{i}"} for i, p in enumerate(paths)
        ]
        text = _build_response("Five components", "feat: components", files)
        result = _feed_all(parser, text)
        assert [f["path"] for f in result] == paths

    def test_empty_files_array(self):
        parser = StreamingJsonFileParser()
        text = _build_response("Nothing to write", "chore: no files", [])
        result = _feed_all(parser, text)
        assert result == []
        assert parser.summary == "Nothing to write"


# ── Chunked delivery ─────────────────────────────────────────────────────────


class TestChunkedDelivery:
    def test_byte_by_byte_delivery(self):
        parser = StreamingJsonFileParser()
        text = _build_response(
            "Byte by byte",
            "feat: chunked",
            [{"path": "src/X.tsx", "action": "create", "content": "hello world"}],
        )
        result = _feed_chunked(parser, text, size=1)
        assert len(result) == 1
        assert result[0]["content"] == "hello world"

    def test_small_chunks(self):
        parser = StreamingJsonFileParser()
        files = [
            {"path": "src/A.tsx", "action": "create", "content": "A content"},
            {"path": "src/B.tsx", "action": "create", "content": "B content"},
        ]
        text = _build_response("Two files", "feat: two", files)
        result = _feed_chunked(parser, text, size=16)
        assert len(result) == 2
        assert result[0]["path"] == "src/A.tsx"
        assert result[1]["path"] == "src/B.tsx"

    def test_summary_extracted_from_chunks(self):
        parser = StreamingJsonFileParser()
        text = _build_response(
            "Chunked summary",
            "feat: chunked commit",
            [{"path": "f.tsx", "action": "create", "content": "x"}],
        )
        _feed_chunked(parser, text, size=8)
        assert parser.summary == "Chunked summary"
        assert parser.commit_message == "feat: chunked commit"

    def test_incremental_emit_order(self):
        """Files should be emitted as each completes, not all at the end."""
        parser = StreamingJsonFileParser()
        files = [
            {"path": "src/First.tsx", "action": "create", "content": "first"},
            {"path": "src/Second.tsx", "action": "create", "content": "second"},
        ]
        text = _build_response("Two", "feat: two", files)

        emitted: list[dict] = []
        for i in range(0, len(text), 4):
            emitted.extend(parser.feed(text[i : i + 4]))

        assert len(emitted) == 2
        assert emitted[0]["path"] == "src/First.tsx"
        assert emitted[1]["path"] == "src/Second.tsx"


# ── Truncation handling ───────────────────────────────────────────────────────


class TestTruncation:
    def test_truncation_mid_second_file_first_file_emitted(self):
        """If stream cuts off mid-second-file, first file is still emitted."""
        parser = StreamingJsonFileParser()
        files = [
            {"path": "src/Complete.tsx", "action": "create", "content": "complete"},
            {"path": "src/Incomplete.tsx", "action": "create", "content": "incomplete"},
        ]
        full = _build_response("Two", "feat: two", files)
        # Find the closing } of the first file and cut there
        first_close = full.index("}, {")
        truncated = full[: first_close + 1]  # include the closing } of first file

        result = _feed_all(parser, truncated)
        assert len(result) == 1
        assert result[0]["path"] == "src/Complete.tsx"

    def test_truncation_mid_content_skips_file(self):
        """Truncation inside a file's content string → that file is not emitted."""
        parser = StreamingJsonFileParser()
        full = _build_response(
            "One",
            "feat: one",
            [{"path": "src/X.tsx", "action": "create", "content": "some long content here"}],
        )
        # Truncate in the middle of the content value
        idx = full.index("some long")
        truncated = full[: idx + 4]  # "some"

        result = _feed_all(parser, truncated)
        assert result == []

    def test_truncation_before_files_key(self):
        """Truncation before the files array starts → nothing emitted."""
        parser = StreamingJsonFileParser()
        result = _feed_all(parser, '{"summary": "test", "commit_message": "feat: x"')
        assert result == []
        # summary may or may not be extracted depending on where truncation occurs
        # — no assertion on summary here, just no crash

    def test_no_crash_on_empty_input(self):
        parser = StreamingJsonFileParser()
        result = parser.feed("")
        assert result == []

    def test_no_crash_on_garbage_input(self):
        parser = StreamingJsonFileParser()
        result = parser.feed("not json at all {{ ]] {{{ garbage")
        assert result == []


# ── Special characters in content ────────────────────────────────────────────


class TestSpecialCharacters:
    def test_escaped_quotes_in_content(self):
        parser = StreamingJsonFileParser()
        content = 'const msg = "hello \\"world\\"";'
        text = _build_response(
            "Escaped quotes",
            "feat: quotes",
            [{"path": "src/X.tsx", "action": "create", "content": content}],
        )
        result = _feed_all(parser, text)
        assert len(result) == 1
        assert result[0]["content"] == content

    def test_escaped_backslash_in_content(self):
        parser = StreamingJsonFileParser()
        content = "path = C:\\\\Users\\\\foo"
        text = _build_response(
            "Backslash",
            "feat: backslash",
            [{"path": "src/X.tsx", "action": "create", "content": content}],
        )
        result = _feed_all(parser, text)
        assert len(result) == 1

    def test_newlines_in_content(self):
        parser = StreamingJsonFileParser()
        content = "line1\\nline2\\nline3"
        text = _build_response(
            "Newlines",
            "feat: newlines",
            [{"path": "src/X.tsx", "action": "create", "content": content}],
        )
        result = _feed_all(parser, text)
        assert len(result) == 1

    def test_braces_inside_string_not_confused_for_structure(self):
        """Curly braces inside a string value must not affect depth tracking."""
        parser = StreamingJsonFileParser()
        content = "const obj = { key: { nested: true } };"
        text = _build_response(
            "Braces in content",
            "feat: braces",
            [{"path": "src/X.tsx", "action": "create", "content": content}],
        )
        result = _feed_all(parser, text)
        assert len(result) == 1
        assert result[0]["content"] == content

    def test_brackets_inside_string(self):
        parser = StreamingJsonFileParser()
        content = "const arr = [1, [2, 3], 4];"
        text = _build_response(
            "Brackets",
            "feat: brackets",
            [{"path": "src/X.tsx", "action": "create", "content": content}],
        )
        result = _feed_all(parser, text)
        assert len(result) == 1
        assert result[0]["content"] == content

    def test_unicode_in_content(self):
        parser = StreamingJsonFileParser()
        content = "// 日本語コメント 🎉"
        text = _build_response(
            "Unicode",
            "feat: unicode",
            [{"path": "src/X.tsx", "action": "create", "content": content}],
        )
        result = _feed_all(parser, text)
        assert len(result) == 1


# ── Large file ────────────────────────────────────────────────────────────────


class TestLargeFile:
    def test_large_content_completes(self):
        """A file with ~100KB of content is handled correctly."""
        parser = StreamingJsonFileParser()
        large_content = "x" * 100_000
        text = _build_response(
            "Large file",
            "feat: large",
            [{"path": "src/Large.tsx", "action": "create", "content": large_content}],
        )
        result = _feed_chunked(parser, text, size=1024)
        assert len(result) == 1
        assert result[0]["content"] == large_content

    def test_multiple_large_files(self):
        parser = StreamingJsonFileParser()
        files = [
            {"path": f"src/File{i}.tsx", "action": "create", "content": "a" * 10_000}
            for i in range(5)
        ]
        text = _build_response("Five large", "feat: large files", files)
        result = _feed_chunked(parser, text, size=512)
        assert len(result) == 5
        for i, r in enumerate(result):
            assert r["path"] == f"src/File{i}.tsx"
            assert len(r["content"]) == 10_000


# ── Summary/commit_message edge cases ────────────────────────────────────────


class TestMetadataExtraction:
    def test_summary_with_special_chars(self):
        parser = StreamingJsonFileParser()
        text = _build_response(
            'Built "Hero" section — fast & clean',
            "feat: hero section",
            [{"path": "src/Hero.tsx", "action": "create", "content": "x"}],
        )
        _feed_all(parser, text)
        assert "Hero" in parser.summary

    def test_empty_summary_default(self):
        parser = StreamingJsonFileParser()
        # Feed just a files array without prior summary
        text = '{"files": [{"path": "x.tsx", "action": "create", "content": "y"}]}'
        _feed_all(parser, text)
        assert parser.summary == ""
        assert parser.commit_message == ""

    def test_commit_message_extracted(self):
        parser = StreamingJsonFileParser()
        text = _build_response(
            "X",
            "feat(ui): add Navbar with mobile drawer",
            [
                {"path": "x.tsx", "action": "create", "content": "y"},
            ],
        )
        _feed_all(parser, text)
        assert parser.commit_message == "feat(ui): add Navbar with mobile drawer"
