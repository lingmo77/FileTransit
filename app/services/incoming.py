"""在鉴权之后增量解析请求，只把文件字节送到存储层，不使用框架整包转存。"""
from __future__ import annotations

import asyncio
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from .storage import StorageError, UploadTooLarge
from ..core.config import settings


class IncomingUpload:
    def __init__(self, request, *, max_bytes: int):
        self.filename = request.query_params.get("filename", "unnamed")
        self.size = None
        self.fields = {}
        self._stream = request.stream().__aiter__()
        self._pending = bytearray()
        self._received = 0
        self._max_bytes = max_bytes
        self._done = False
        self._ended = False
        self._files = 0
        self._part_file = False
        self._part_name = ""
        self._field = bytearray()
        self._headers = {}
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._header_bytes = 0
        content_type, options = parse_options_header(request.headers.get("content-type", ""))
        self.multipart = content_type == b"multipart/form-data"
        self._parser = None
        if self.multipart:
            boundary = options.get(b"boundary")
            if not boundary or len(boundary) > 200:
                raise StorageError("上传表单缺少合法边界")
            self._parser = MultipartParser(boundary, {
                "on_part_begin": self._begin,
                "on_header_field": self._header_field,
                "on_header_value": self._header_data,
                "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished,
                "on_part_data": self._data,
                "on_part_end": self._part_end,
                "on_end": self._end,
            })
        elif content_type != b"application/octet-stream":
            raise StorageError("请使用文件流或 multipart 表单上传")

    def _begin(self):
        self._headers = {}
        self._field.clear()
        self._header_bytes = 0

    def _header_field(self, data, start, end):
        self._header_bytes += end - start
        if self._header_bytes > 8192:
            raise StorageError("上传字段头过长")
        self._header_name.extend(data[start:end])

    def _header_data(self, data, start, end):
        self._header_bytes += end - start
        if self._header_bytes > 8192:
            raise StorageError("上传字段头过长")
        self._header_value.extend(data[start:end])

    def _header_end(self):
        self._headers[bytes(self._header_name).lower()] = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def _headers_finished(self):
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        self._part_name = options.get(b"name", b"").decode("utf-8", errors="replace")
        self._part_file = b"filename" in options
        if self._part_file:
            self._files += 1
            if self._files != 1 or self._part_name != "file":
                raise StorageError("每次请求只能包含一个文件")
            self.filename = options[b"filename"].decode("utf-8", errors="replace")

    def _data(self, data, start, end):
        if self._part_file:
            self._pending.extend(data[start:end])
        else:
            if len(self._field) + end - start > 1024:
                raise StorageError("上传字段过长")
            self._field.extend(data[start:end])

    def _part_end(self):
        if not self._part_file:
            if self._part_name in self.fields or len(self.fields) >= 8:
                raise StorageError("上传表单包含重复或过多字段")
            self.fields[self._part_name] = self._field.decode("utf-8", errors="replace")

    def _end(self):
        self._ended = True

    async def read(self, size: int) -> bytes:
        while not self._pending and not self._done:
            try:
                chunk = await asyncio.wait_for(anext(self._stream), settings.upload_idle_timeout_seconds)
            except TimeoutError as exc:
                raise StorageError("上传长时间没有数据，请重试") from exc
            except StopAsyncIteration:
                self._done = True
                if self.multipart and (not self._ended or self._files != 1):
                    raise StorageError("上传表单不完整")
                break
            self._received += len(chunk)
            if self._received > self._max_bytes + (65536 if self.multipart else 0):
                raise UploadTooLarge(self._max_bytes)
            if self._parser:
                try:
                    self._parser.write(chunk)
                except StorageError:
                    raise
                except Exception as exc:
                    raise StorageError("上传表单格式错误") from exc
            else:
                self._pending.extend(chunk)
        result = bytes(self._pending[:size])
        del self._pending[:size]
        return result
