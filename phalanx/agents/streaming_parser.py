"""
StreamingJsonFileParser — stateful incremental parser for FORGE builder streaming output.

Consumes chunks of the Claude streaming response and emits complete file dicts
as each file object closes. Trims the buffer after each file so memory stays
bounded to the size of one file at a time, not the full response.

Expected JSON format:
{
  "summary": "...",
  "commit_message": "...",
  "files": [
    {"path": "src/Foo.tsx", "action": "create", "content": "..."},
    {"path": "src/Foo.test.tsx", "action": "create", "content": "..."}
  ]
}
"""

from __future__ import annotations

import json
import re


class StreamingJsonFileParser:
    """
    Stateful parser for streaming builder JSON responses.

    Feed chunks via feed(); get completed FileObject dicts as each file closes.
    Access summary and commit_message after the stream finishes.

    Usage:
        parser = StreamingJsonFileParser()
        with client.messages.stream(...) as stream:
            for chunk in stream.text_stream:
                for file_obj in parser.feed(chunk):
                    write_file(file_obj)
        summary = parser.summary
        commit_message = parser.commit_message
    """

    def __init__(self) -> None:
        self._buf = ""
        self._pos = 0
        self._in_string = False
        self._escape_next = False

        self._found_files_key = False
        self._in_files_array = False
        self._obj_depth = 0      # brace depth within the files array
        self._obj_start = -1     # buf index of the current file object's opening {

        self._summary = ""
        self._commit_message = ""

    # ── Public API ────────────────────────────────────────────────────────────

    def feed(self, chunk: str) -> list[dict]:
        """
        Feed a text chunk. Returns any newly completed file dicts.
        Each dict has at minimum: path, action, content.
        """
        self._buf += chunk
        completed: list[dict] = []

        while self._pos < len(self._buf):

            # ── Phase 1: find the "files" key ────────────────────────────────
            if not self._found_files_key:
                marker = '"files"'
                # Search from slightly before self._pos to catch markers that
                # straddle chunk boundaries (started before pos, ends after).
                search_from = max(0, self._pos - len(marker))
                idx = self._buf.find(marker, search_from)
                if idx == -1:
                    # Not in buffer yet — wait for more chunks
                    self._pos = len(self._buf)
                    break

                # Confirm a ':' follows (after optional whitespace)
                rest = self._buf[idx + len(marker):]
                stripped = rest.lstrip()
                if not stripped:
                    # Colon not yet streamed — wait
                    self._pos = len(self._buf)
                    break
                if stripped[0] != ':':
                    # False match (e.g. "files_list") — skip past it
                    self._pos = idx + len(marker)
                    continue

                self._extract_top_level_fields(idx)
                self._found_files_key = True
                self._pos = idx + len(marker)
                continue

            # ── Phase 2: find the opening '[' ────────────────────────────────
            if not self._in_files_array:
                ch = self._buf[self._pos]
                if ch == '[':
                    self._in_files_array = True
                self._pos += 1
                continue

            # ── Phase 3: parse file objects character by character ────────────
            ch = self._buf[self._pos]

            if self._escape_next:
                self._escape_next = False
                self._pos += 1
                continue

            if ch == '\\' and self._in_string:
                self._escape_next = True
                self._pos += 1
                continue

            if ch == '"':
                self._in_string = not self._in_string
                self._pos += 1
                continue

            if self._in_string:
                self._pos += 1
                continue

            # Structural character (not in a string)
            if ch == '{':
                if self._obj_depth == 0:
                    self._obj_start = self._pos
                self._obj_depth += 1

            elif ch == '}':
                self._obj_depth -= 1
                if self._obj_depth == 0 and self._obj_start >= 0:
                    # File object is complete — parse and emit
                    obj_text = self._buf[self._obj_start:self._pos + 1]
                    try:
                        file_obj = json.loads(obj_text)
                        if isinstance(file_obj, dict) and file_obj.get("path"):
                            completed.append(file_obj)
                    except (json.JSONDecodeError, ValueError):
                        pass  # Malformed object — skip silently
                    self._obj_start = -1
                    # Trim buffer: discard everything up to and including this '}'
                    trim_to = self._pos + 1
                    self._buf = self._buf[trim_to:]
                    self._pos = 0
                    continue  # restart from trimmed buffer start

            elif ch == ']' and self._obj_depth == 0:
                # Files array closed
                self._in_files_array = False

            self._pos += 1

        return completed

    @property
    def summary(self) -> str:
        return self._summary

    @property
    def commit_message(self) -> str:
        return self._commit_message

    # ── Internals ─────────────────────────────────────────────────────────────

    def _extract_top_level_fields(self, up_to: int) -> None:
        """
        Extract summary and commit_message from the pre-files portion of the buffer.
        These appear before the files array so a simple regex scan is sufficient.
        """
        text = self._buf[:up_to]

        m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            self._summary = _unescape_json_string(m.group(1))

        m = re.search(r'"commit_message"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            self._commit_message = _unescape_json_string(m.group(1))


def _unescape_json_string(s: str) -> str:
    """Unescape a JSON string value (content between quotes, without the quotes)."""
    try:
        return json.loads(f'"{s}"')
    except (json.JSONDecodeError, ValueError):
        return s
