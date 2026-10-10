"""字幕解析、合并、健全性检查、导出、笔记渲染（离线）。"""
import json

from app import llm
from app import transcript as tr
from app.asr.qwen3_worker import group_words
from app.models import MediaInfo, Segment, Transcript

VTT = """WEBVTT
Kind: captions

00:00:01.000 --> 00:00:03.500 align:start
<c>大家好</c>，欢迎收听

00:00:03.500 --> 00:00:06.000
今天聊聊&amp;开源

1:02:03.250 --> 1:02:05.000
最后一句。
"""


def test_parse_vtt():
    segs = tr.parse_vtt(VTT)
    assert [s.text for s in segs] == ["大家好，欢迎收听", "今天聊聊&开源", "最后一句。"]
    assert segs[0].start == 1.0 and segs[0].end == 3.5
    assert segs[2].start == 3723.25


def test_parse_srt():
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:02,000 --> 00:00:03,000\nworld\n"
    assert [s.text for s in tr.parse_vtt(srt)] == ["Hello", "world"]


def test_rolling_dedupe():
    segs = [Segment(0, 2, "我们今天"), Segment(2, 4, "我们今天聊一聊"), Segment(4, 5, "我们今天聊一聊")]
    out = tr.dedupe_rolling(segs)
    assert [s.text for s in out] == ["我们今天", "聊一聊"]
    assert out[-1].end == 5


def test_parse_json3():
    data = {"events": [
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Hello "}, {"utf8": "there"}]},
        {"tStartMs": 3000, "dDurationMs": 10, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 4000},
    ]}
    segs = tr.parse_json3(json.dumps(data))
    assert len(segs) == 1 and segs[0].text == "Hello there" and segs[0].end == 3.0


def test_parse_bilibili():
    body = {"body": [{"from": 0.5, "to": 2.1, "content": "第一句"}, {"from": 2.1, "to": 3, "content": "  "}]}
    segs = tr.parse_bilibili_json(body)
    assert len(segs) == 1 and segs[0].start == 0.5


def test_merge_respects_speaker_and_punct():
    segs = [Segment(0, 1, "你好", "S1"), Segment(1, 2, "世界。", "S1"), Segment(2, 3, "下一句", "S1"), Segment(3, 4, "换人", "S2")]
    out = tr.merge_segments(segs)
    assert [(s.text, s.speaker) for s in out] == [("你好世界。", "S1"), ("下一句", "S1"), ("换人", "S2")]


def test_merge_english_spacing():
    out = tr.merge_segments([Segment(0, 1, "(Laughter)"), Segment(1, 2, "There")])
    assert out[0].text == "(Laughter) There"


def test_sanity_check():
    good = Transcript("platform_subtitle", "zh", [Segment(i * 10, i * 10 + 9, "这是一句足够长的中文字幕内容啊") for i in range(60)])
    assert tr.sanity_check(good, 600) is None
    short = Transcript("platform_subtitle", "zh", [Segment(0, 100, "只有开头" * 50)])
    assert "覆盖" in tr.sanity_check(short, 600)
    sparse = Transcript("platform_subtitle", "zh", [Segment(0, 600, "嗯")])
    assert "字数" in tr.sanity_check(sparse, 600)
    assert tr.sanity_check(Transcript("platform_subtitle", "zh", []), 600) == "字幕为空"


def test_exports_utf8_and_srt_format():
    t = Transcript("local_asr", "zh", [Segment(0, 1.5, "中文内容", "S1"), Segment(3661.2, 3662, "第二段", "S2")], "Qwen3-ASR-1.7B")
    srt = tr.to_srt(t)
    assert "00:00:00,000 --> 00:00:01,500" in srt and "01:01:01,200" in srt and "[S1] 中文内容" in srt
    md = tr.to_markdown(t, "标题")
    assert "`[1:01:01]` **S2**：第二段" in md
    assert tr.to_llm_text(t).splitlines()[0] == "[00:00] S1: 中文内容"
    assert Transcript.from_dict(json.loads(tr.to_json(t))) == t


def test_clock():
    assert tr.fmt_clock(65) == "01:05" and tr.fmt_clock(3725) == "1:02:05"
    assert tr.parse_clock("01:05") == 65 and tr.parse_clock("1:02:05") == 3725 and tr.parse_clock("ab") is None


def test_group_words_cjk():
    words = [{"text": c, "start": i * 0.2, "end": i * 0.2 + 0.2, "speaker": "SPEAKER_00"} for i, c in enumerate("你好世界。")]
    words += [{"text": c, "start": 2 + i * 0.2, "end": 2.2 + i * 0.2, "speaker": "SPEAKER_01"} for i, c in enumerate("再见")]
    segs = group_words(words, no_space=True)
    assert [(s["text"], s["speaker"]) for s in segs] == [("你好世界。", "SPEAKER_00"), ("再见", "SPEAKER_01")]


def test_render_note_timestamps():
    info = MediaInfo("youtube", "abc", "https://www.youtube.com/watch?v=abc", "T", "A", 600)
    t = Transcript("platform_subtitle", "en", [Segment(0, 600, "x")], "YouTube 人工字幕 en")
    body = "观点 [01:05][02:00] 超时 [59:00]\n### [10:00] 结尾"
    out = llm.render_note(body, t, info, lambda s: f"{info.url}&t={s}s")
    assert "[01:05](https://www.youtube.com/watch?v=abc&t=65s) [02:00](" in out
    assert "59:00(?)" in out
    assert "### [10:00](" in out
    assert out.startswith("# T\n")


def test_render_note_no_seek_platform():
    info = MediaInfo("xiaoyuzhou", "e", "https://x/episode/e", "T", "A", 600)
    t = Transcript("local_asr", "zh", [Segment(0, 600, "x")], "Qwen3-ASR-1.7B")
    out = llm.render_note("观点 [01:05]", t, info, lambda s: None)
    assert "`01:05`" in out


def test_split_by_time():
    lines = [(i * 60.0, f"l{i}") for i in range(50)]
    chunks = llm.split_by_time(lines, 20 * 60)
    assert [len(c) for c in chunks] == [20, 20, 10]


def test_restore_punct_tolerates_mismatch():
    from app.asr.qwen3_worker import restore_punct

    text = "好，Hello，大家好。在开 D Tarp 上做了项目，然后谢谢！"
    words = [{"text": t, "start": i, "end": i + 1} for i, t in enumerate(
        ["好", "Hello", "大", "家", "好", "在", "开", "DTarp", "上", "做", "XX", "项", "目", "然", "后", "谢", "谢"])]
    out = [w["text"] for w in restore_punct(words, text)]
    assert out[:5] == ["好，", "Hello，", "大", "家", "好。"]
    assert out[7] == "DTarp"  # 原文带空格也能匹配
    assert out[10] == "XX"  # 对不上的词原样保留，后面重新同步
    assert out[12] == "目，" and out[-1] == "谢！"


def test_assign_speakers_bisect():
    from app.asr.qwen3_worker import assign_speakers

    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
        {"speaker": "SPEAKER_01", "start": 5.2, "end": 9.0},
        {"speaker": "SPEAKER_00", "start": 4.0, "end": 30.0},  # 长段且与前面重叠，乱序给出
    ]
    words = [
        {"text": "a", "start": 1.0, "end": 1.2},
        {"text": "b", "start": 6.0, "end": 6.3},  # 与 01（3.0s 内）和 00 长段都重叠，取重叠更多者：一样长时取先找到的
        {"text": "c", "start": 20.0, "end": 20.5},  # 只有长段覆盖，需要往回看
        {"text": "d", "start": 40.0, "end": 40.5},  # 不在任何段内：取最近
    ]
    out = [w["speaker"] for w in assign_speakers(words, turns)]
    assert out[0] == "SPEAKER_00" and out[2] == "SPEAKER_00" and out[3] == "SPEAKER_00"
    assert out[1] in ("SPEAKER_00", "SPEAKER_01")
    assert assign_speakers([{"text": "x", "start": 1, "end": 2}], []) == [{"text": "x", "start": 1, "end": 2, "speaker": None}]


def test_assign_speakers_matches_library():
    """与库自带（O(n×m)）实现结果一致。"""
    import random

    from mlx_qwen3_asr.diarization import diarize_word_segments

    from app.asr.qwen3_worker import assign_speakers

    rnd = random.Random(0)
    t, turns = 0.0, []
    for i in range(200):
        d = rnd.uniform(0.5, 20)
        turns.append({"speaker": f"SPEAKER_0{i % 3}", "start": t, "end": t + d})
        t += d + rnd.uniform(-0.3, 1.0)
    words = []
    w = 0.0
    while w < t + 5:
        words.append({"text": "字", "start": w, "end": w + 0.2})
        w += rnd.uniform(0.15, 0.6)
    mine = [x["speaker"] for x in assign_speakers(words, turns)]
    lib = [x["speaker"] for x in diarize_word_segments(words, speaker_turns=turns)]
    diff = sum(a != b for a, b in zip(mine, lib))
    assert diff / len(words) < 0.005  # 只允许重叠长度完全相等这类平局取舍不同
