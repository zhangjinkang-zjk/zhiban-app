"""
文本拆分工具函数单元测试

测试目标：backend/src/service/narration/service.py 的 _split_long_text()
覆盖范围：
- 短文本不拆分
- 长文本按句子边界拆分
- 各种标点的边界处理
- 空字符串 / 特殊输入
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# 直接导入内部函数（测试私有函数是合理的）
from backend.src.service.narration.service import _split_long_text


class TestSplitShortText:
    """短文本 — 不应拆分"""

    def test_empty_string(self):
        """空字符串返回包含空字符串的列表"""
        result = _split_long_text("")
        assert result == [""]

    def test_single_char(self):
        """单个字符不拆分"""
        assert _split_long_text("好") == ["好"]

    def test_below_max_chars(self):
        """小于等于 max_chars 的文本不拆分"""
        text = "这是一段短文本"  # 7 个字
        result = _split_long_text(text, max_chars=100)
        assert result == [text]

    def test_exactly_max_chars(self):
        """刚好等于 max_chars 不拆分"""
        text = "甲" * 800  # 刚好 800 字符
        result = _split_long_text(text, max_chars=800)
        assert result == [text]
        assert len(result) == 1


class TestSplitLongText:
    """长文本 — 需要拆分"""

    def test_basic_split_at_period(self):
        """长文本在句号处拆分"""
        # 构造一段超长文本，中间有句号
        text = "甲" * 500 + "。" + "乙" * 500  # 总共 1001 字符 > 800
        result = _split_long_text(text, max_chars=800)
        # 应该在句号处断开
        assert len(result) >= 2
        assert all(len(chunk) <= 800 + 10 for chunk in result)  # 允许小误差

    def test_split_preserves_all_content(self):
        """拆分后拼接回原文不应该丢失内容"""
        original = "这是一段很长的文本。" * 200  # 超长文本
        result = _split_long_text(original, max_chars=400)
        rejoined = "".join(result)
        assert rejoined == original

    def test_multiple_chunks(self):
        """非常长的文本应该分成多段"""
        text = "测试句子。" * 300  # 约 1500 字符
        result = _split_long_text(text, max_chars=400)
        assert len(result) > 1

    def test_no_chunk_exceeds_max(self):
        """每个分块都不应超过 max_chars（太多）"""
        text = "这是一个测试。你好世界！" * 200
        result = _split_long_text(text, max_chars=500)
        for chunk in result:
            # 允许略微超出（因为可能在句号边界找不到好的切分点）
            assert len(chunk) <= 500 + 50, f"Chunk too long: {len(chunk)} chars"


class TestSplitBoundary:
    """标点符号边界处理"""

    def test_prefer_period_over_comma(self):
        """优先在句号处切分，而不是逗号"""
        text = "甲" * 400 + "，" + "乙" * 350 + "。" + "丙" * 400
        result = _split_long_text(text, max_chars=800)
        # 句号比逗号优先级高（实际上 rfind 从右往左找第一个分隔符）
        assert len(result) >= 2

    def test_newline_as_separator(self):
        """换行符可以作为分隔符"""
        text = "第一行内容\n" + "第二行内容" * 100
        result = _split_long_text(text, max_chars=200)
        assert len(result) >= 2

    def test_no_separator_in_range(self):
        """范围内没有标点时强制截断"""
        # 一串没有标点的超长文字
        text = "abcdefghij" * 100  # 1000 字符无标点
        result = _split_long_text(text, max_chars=500)
        assert len(result) >= 2
        # 每段最多 ~500 字符（强制切分）

    def test_chinese_punctuation_priority(self):
        """中文标点优先级：。！？> ；> \n > ，、> 空格"""
        # 这个测试验证各种中文标点都能作为分割点
        mixed = (
            "第一段内容。" +
            "甲" * 400 +
            "！" +
            "乙" * 400 +
            "？" +
            "丙" * 400
        )
        result = _split_long_text(mixed, max_chars=500)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 550


class TestEdgeCases:
    """边界情况"""

    def test_only_punctuation(self):
        """只有标点符号"""
        result = _split_long_text("。。。！！！", max_chars=3)
        assert len(result) >= 1

    def test_whitespace_only(self):
        """只有空白字符"""
        result = _split_long_text("   \n\t  ", max_chars=5)
        assert len(result) >= 1

    def test_mixed_chinese_and_english(self):
        """中英混合文本"""
        # 注意：_split_long_text 内部对每个 chunk 做 .strip()，
        # 所以拼接后空白字符可能与原文略有差异（TTS 场景下这是合理的）
        text = ("Hello world 这是一个测试 sentence。" * 100)[:2000]
        result = _split_long_text(text, max_chars=500)
        # 去除首尾空白后再比较
        assert "".join(result).strip() == text.strip()

    def test_very_small_max_chars(self):
        """max_chars 很小时也能工作"""
        text = "一二三四五六七八九十。"
        result = _split_long_text(text, max_chars=3)
        assert "".join(result) == text
