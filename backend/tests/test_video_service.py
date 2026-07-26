"""
外部视频搜索服务单元测试。

测试目标：backend/src/service/video/service.py
覆盖范围：
- 视频时长、播放量格式化工具
- ExternalVideoService.search 的 B 站 API 编排与异常降级
"""

from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.src.service.video.service import (  # noqa: E402
    ExternalVideoService,
    _format_duration,
    _format_view_count,
)


class TestFormatDuration:
    def test_empty_values(self):
        assert _format_duration(None) == ""
        assert _format_duration("") == ""
        assert _format_duration(0) == ""

    def test_seconds(self):
        assert _format_duration(45) == "0:45"
        assert _format_duration(125) == "2:05"
        assert _format_duration(3661) == "1:01:01"

    def test_colon_string(self):
        assert _format_duration("12:03") == "12:03"
        assert _format_duration("1:02:03") == "1:02:03"


class TestFormatViewCount:
    def test_empty_values(self):
        assert _format_view_count(None) == ""
        assert _format_view_count(0) == ""

    def test_small_number(self):
        assert _format_view_count(999) == "999次"

    def test_large_number(self):
        assert _format_view_count(10000) == "1万次"
        assert _format_view_count(1234567) == "123.5万次"


class TestExternalVideoServiceSearch:
    @pytest.mark.asyncio
    @patch("backend.src.service.video.service.httpx.AsyncClient")
    async def test_search_success(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "result": [
                    {
                        "bvid": "BV1xx411c7mD",
                        "title": '微机原理 <em class="keyword">8086</em>',
                        "arcurl": "https://www.bilibili.com/video/BV1xx411c7mD",
                        "description": "8086 教学视频",
                        "pic": "//i0.hdslb.com/bfs/archive/a.jpg",
                        "duration": "12:03",
                        "author": "张老师",
                        "play": 123456,
                    }
                ]
            }
        }
        mock_client.get.return_value = mock_resp

        result = await ExternalVideoService.search("微机原理", max_results=3)

        assert len(result) == 1
        assert result[0]["title"] == "微机原理 8086"
        assert result[0]["page_url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
        assert result[0]["embed_url"] == "https://player.bilibili.com/player.html?bvid=BV1xx411c7mD"
        assert result[0]["source"] == "bilibili"
        mock_resp.raise_for_status.assert_called_once()
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    @patch("backend.src.service.video.service.httpx.AsyncClient")
    async def test_search_empty_topic(self, mock_client_cls):
        assert await ExternalVideoService.search("", max_results=3) == []
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.src.service.video.service.httpx.AsyncClient")
    async def test_search_error_returns_empty(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = RuntimeError("network broken")

        assert await ExternalVideoService.search("微机原理", max_results=3) == []
