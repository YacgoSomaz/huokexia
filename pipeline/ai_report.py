"""AI-assisted replay reports.

The model is intentionally not an agent here. 直播复盘侠 gathers local evidence
from SQLite first, asks an OpenAI-compatible chat API to summarize bounded
chunks, validates the structure, then asks for a final Markdown report.
"""

from __future__ import annotations

import json
import math
import re
import html
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import requests

from . import config
from . import export as export_mod


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
CHUNK_CHAR_LIMIT = 10000
MAX_CHUNKS = 16
WORD_LIMIT = 80
# API calls are remote-bound, not local CPU-bound. Start high and tune down if
# the provider returns rate-limit/JSON stability issues during real workloads.
AI_CHUNK_WORKERS = 20
LIVE_KNOWLEDGE_PATH = Path(__file__).resolve().parent / "knowledge" / "live_replay_knowledge.md"

_STOP_WORDS = {
    "我们", "你们", "大家", "这个", "那个", "然后", "就是", "可以", "一下",
    "直播", "直播间", "朋友", "家人", "的话", "现在", "这边", "那边", "一个",
    "还是", "没有", "不是", "因为", "所以", "如果", "来说", "看到", "关注",
    "点击", "下方", "链接", "预约", "直接", "进行", "给到", "来到", "好吧",
    "今天", "这一个", "这一边", "这套", "这个是", "里面", "上面", "下面",
    "是否", "是不是", "什么", "多少", "怎么", "为什么", "对不", "不对",
    "对不对", "真的", "当然", "非常", "比较", "很多", "之前", "之后",
}
_STOP_SUBSTRINGS = {
    "我们", "你们", "大家", "咱们", "这个", "那个", "这边", "那边", "的话",
    "的一", "一个", "一下", "可以", "都可", "有到", "进行", "直接", "点击",
    "下方", "链接", "来看", "看一", "一看", "去看", "来给", "给大", "给到",
    "录音", "音频", "主播", "直播", "房间", "时间", "未标", "标注",
    "是不是", "是否", "什么", "对不", "不对", "好的", "老板", "家人",
    "这个", "那个", "的话", "没有", "不是", "因为", "所以", "如果",
}
_BAD_EDGE_CHARS = set("的一了到这那们个下有可都去来是我你他她它在就和及把被吗呢啊吧哦哈呀上下面里中")
_IMPORTANT_TERMS = {
    "户型", "三房", "四房", "小高层", "洋房", "现房", "精装", "精装修", "毛坯",
    "首付", "月供", "总价", "单价", "优惠", "折扣", "特价房", "认购", "定金",
    "学校", "小学", "初中", "高中", "幼儿园", "学区", "入读", "业主", "户口",
    "地铁", "商圈", "配套", "物业", "绿化率", "容积率", "车位", "交付",
    "盘龙", "昆明", "大华", "锦绣", "麓城", "云师大", "盘龙小学",
}


def _load_live_ops_knowledge(limit: int = 12000) -> str:
    """Load the built-in live-operation playbook for report grounding."""
    try:
        text = LIVE_KNOWLEDGE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text[:limit]


@dataclass(frozen=True)
class AIConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout_sec: int = 180

    @property
    def ready(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())


class AIReportError(RuntimeError):
    """User-facing report generation error."""


def load_config() -> AIConfig:
    path = config.AI_CONFIG_PATH
    if not path.exists():
        return AIConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return AIConfig()
    return AIConfig(
        base_url=str(data.get("base_url") or DEFAULT_BASE_URL).strip(),
        api_key=str(data.get("api_key") or "").strip(),
        model=str(data.get("model") or DEFAULT_MODEL).strip(),
        timeout_sec=int(data.get("timeout_sec") or 180),
    )


def public_config() -> dict[str, object]:
    cfg = load_config()
    extra = _read_raw_config()
    return {
        "base_url": cfg.base_url,
        "model": cfg.model,
        "has_api_key": bool(cfg.api_key),
        "ready": cfg.ready,
        "timeout_sec": cfg.timeout_sec,
        "vision_base_url": str(extra.get("vision_base_url") or "https://ark.cn-beijing.volces.com/api/v3").strip(),
        "vision_model": str(extra.get("vision_model") or "ep-m-20260518173100-t8kjz").strip(),
        "has_vision_api_key": bool(str(extra.get("vision_api_key") or "").strip()),
        "vision_timeout_sec": int(extra.get("vision_timeout_sec") or 120),
    }


def _read_raw_config() -> dict[str, object]:
    path = config.AI_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(payload: dict[str, object]) -> dict[str, object]:
    old_raw = _read_raw_config()
    old = load_config()
    base_url = str(payload.get("base_url") or old.base_url or DEFAULT_BASE_URL).strip()
    model = str(payload.get("model") or old.model or DEFAULT_MODEL).strip()
    timeout_sec = int(payload.get("timeout_sec") or old.timeout_sec or 180)
    clear_key = bool(payload.get("clear_api_key"))
    raw_key = payload.get("api_key")
    if clear_key:
        api_key = ""
    elif raw_key is None or str(raw_key).strip() == "":
        api_key = old.api_key
    else:
        api_key = str(raw_key).strip()
    vision_base_url = str(
        payload.get("vision_base_url")
        or old_raw.get("vision_base_url")
        or "https://ark.cn-beijing.volces.com/api/v3"
    ).strip()
    vision_model = str(payload.get("vision_model") or old_raw.get("vision_model") or "ep-m-20260518173100-t8kjz").strip()
    vision_timeout_sec = int(payload.get("vision_timeout_sec") or old_raw.get("vision_timeout_sec") or 120)
    clear_vision_key = bool(payload.get("clear_vision_api_key"))
    raw_vision_key = payload.get("vision_api_key")
    if clear_vision_key:
        vision_api_key = ""
    elif raw_vision_key is None or str(raw_vision_key).strip() == "":
        vision_api_key = str(old_raw.get("vision_api_key") or "").strip()
    else:
        vision_api_key = str(raw_vision_key).strip()
    cfg = AIConfig(base_url=base_url, api_key=api_key, model=model, timeout_sec=timeout_sec)
    config.AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.AI_CONFIG_PATH.write_text(
        json.dumps(
            {
                "base_url": cfg.base_url,
                "api_key": cfg.api_key,
                "model": cfg.model,
                "timeout_sec": cfg.timeout_sec,
                "vision_base_url": vision_base_url,
                "vision_api_key": vision_api_key,
                "vision_model": vision_model,
                "vision_timeout_sec": vision_timeout_sec,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return public_config()


def _chat_url(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _chat_completion(
    cfg: AIConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 1800,
    response_format: dict[str, str] | None = None,
) -> str:
    if not cfg.ready:
        raise AIReportError("AI 尚未配置，请先在系统设置里填写 base_url、API Key 和模型名。")
    payload: dict[str, object] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            _chat_url(cfg.base_url),
            headers=headers,
            json=payload,
            timeout=cfg.timeout_sec,
        )
        if resp.status_code == 400 and response_format:
            payload.pop("response_format", None)
            resp = requests.post(
                _chat_url(cfg.base_url),
                headers=headers,
                json=payload,
                timeout=cfg.timeout_sec,
            )
    except requests.RequestException as exc:
        raise AIReportError(f"AI 请求失败：{exc}") from exc
    if resp.status_code >= 400:
        text = (resp.text or "")[:500]
        raise AIReportError(f"AI 接口返回 HTTP {resp.status_code}: {text}")
    try:
        data = resp.json()
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIReportError("AI 返回格式异常，无法读取 message.content。") from exc


def _chat_completion_stream(
    cfg: AIConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 1800,
) -> Iterator[str]:
    """Yield visible assistant answer deltas from an OpenAI-compatible stream.

    Some providers include hidden reasoning fields in streaming chunks.  Those
    fields are intentionally ignored here; the UI shows our own evidence
    processing milestones instead of exposing model internals.
    """
    if not cfg.ready:
        raise AIReportError("AI 尚未配置，请先在系统设置里填写 base_url、API Key 和模型名。")
    payload: dict[str, object] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    try:
        with requests.post(
            _chat_url(cfg.base_url),
            headers=headers,
            json=payload,
            timeout=(10, cfg.timeout_sec),
            stream=True,
        ) as resp:
            if resp.status_code >= 400:
                text = (resp.text or "")[:500]
                raise AIReportError(f"AI 接口返回 HTTP {resp.status_code}: {text}")
            # Keep chunk_size tiny so provider SSE deltas pass through instead
            # of waiting inside requests' default line buffer.
            for raw_line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue
                try:
                    data = json.loads(line)
                    delta = data["choices"][0].get("delta") or {}
                    # Never surface reasoning_content / hidden thinking.
                    content = delta.get("content")
                    if content:
                        yield str(content)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
    except requests.RequestException as exc:
        raise AIReportError(f"AI 流式请求失败：{exc}") from exc


def test_config() -> dict[str, object]:
    cfg = load_config()
    text = _chat_completion(
        cfg,
        [
            {"role": "system", "content": "你是一个接口连通性测试助手。"},
            {"role": "user", "content": "只回复：OK"},
        ],
        max_tokens=20,
        temperature=0,
    )
    return {"ok": True, "reply": text[:100]}


def _fmt_time(ts: int | float | None) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def _bundle_overview(bundle: export_mod.RoomBundle) -> dict[str, object]:
    peak = max((s[1] for s in bundle.stats), default=0)
    last_pv = bundle.stats[-1][2] if bundle.stats else 0
    return {
        "room_id": bundle.rid,
        "nickname": bundle.nickname or bundle.rid,
        "transcript_segments": len(bundle.transcripts),
        "transcript_chars": sum(t.char_count for t in bundle.transcripts),
        "chat_count": len(bundle.chats),
        "event_counts": bundle.event_counts,
        "online_peak": peak,
        "platform_pv_latest": last_pv,
    }


def _load_bundles(rids: list[str]) -> list[export_mod.RoomBundle]:
    ids = [str(rid).strip() for rid in rids if str(rid).strip()]
    if not ids:
        raise AIReportError("请先选择至少一个主播。")
    speaker_labels = export_mod.load_speaker_labels()
    names = export_mod.room_display_names()
    bundles = [export_mod.build_bundle(rid, names.get(rid, ""), speaker_labels) for rid in ids]
    bundles = [b for b in bundles if b.transcripts]
    if not bundles:
        raise AIReportError("所选主播暂无已转写话术，不能生成复盘。")
    return bundles


def _transcript_line(room: export_mod.RoomBundle, item: export_mod.TranscriptRow) -> str:
    start = item.capture_start if item.capture_start is not None else item.segment_ts
    end = item.capture_end
    speaker = f"[{item.speaker_label}]" if item.speaker_label else ""
    text = (item.text or "").replace("\n", " ").strip()
    return (
        f"[{room.nickname or room.rid}][{_fmt_time(start)} - {_fmt_time(end)}]"
        f"{speaker} {text}"
    )


def _all_text(bundles: list[export_mod.RoomBundle], limit: int = 160_000) -> str:
    parts: list[str] = []
    total = 0
    for bundle in bundles:
        for item in bundle.transcripts:
            text = (item.text or "").replace("\n", " ").strip()
            if not text:
                continue
            line = _transcript_line(bundle, item)
            parts.append(line)
            total += len(line)
            if total >= limit:
                return "\n".join(parts)
    return "\n".join(parts)


def _plain_transcript_text(bundles: list[export_mod.RoomBundle], limit: int = 160_000) -> str:
    parts: list[str] = []
    total = 0
    for bundle in bundles:
        for item in bundle.transcripts:
            text = (item.text or "").replace("\n", " ").strip()
            if not text:
                continue
            parts.append(text)
            total += len(text)
            if total >= limit:
                return "\n".join(parts)
    return "\n".join(parts)


def _good_word(word: str) -> bool:
    if not (2 <= len(word) <= 8):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fff]+", word):
        return False
    if word in _STOP_WORDS:
        return False
    if any(s in word for s in _STOP_SUBSTRINGS):
        return False
    if word[0] in _BAD_EDGE_CHARS or word[-1] in _BAD_EDGE_CHARS:
        return False
    return True


def _important_terms(text: str) -> list[str]:
    hits: list[str] = []
    for term in _IMPORTANT_TERMS:
        hits.extend([term] * len(re.findall(re.escape(term), text)))
    money_patterns = [
        r"\d+(?:\.\d+)?万(?:左右|多|起)?",
        r"\d+(?:\.\d+)?平(?:米)?",
        r"\d+(?:\.\d+)?%?",
        r"\d+房",
    ]
    for pattern in money_patterns:
        for m in re.findall(pattern, text):
            if 2 <= len(m) <= 8:
                hits.append(m)
    return hits


def _tokenize_words(text: str) -> list[str]:
    important = _important_terms(text)
    try:
        import jieba  # type: ignore

        words = important[:]
        for word in jieba.cut(text):
            word = str(word).strip()
            if _good_word(word):
                words.append(word)
        if words:
            return words
    except Exception:
        pass

    clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text)
    words: list[str] = important[:]
    for n in (2, 3, 4):
        for i in range(0, max(0, len(clean) - n + 1)):
            word = clean[i:i + n]
            if _good_word(word):
                words.append(word)
    return words


def word_cloud(rids: list[str], limit: int = WORD_LIMIT) -> dict[str, object]:
    bundles = _load_bundles(rids)
    text = _plain_transcript_text(bundles)
    counter = Counter(_tokenize_words(text))
    words = [
        {"word": word, "count": count}
        for word, count in counter.most_common(max(1, min(200, limit)))
        if count >= 2
    ]
    max_count = words[0]["count"] if words else 0
    for item in words:
        item["weight"] = round(float(item["count"]) / float(max_count or 1), 4)
    return {
        "rooms": [_bundle_overview(b) for b in bundles],
        "words": words,
        "total_words": sum(counter.values()),
    }


def _local_brief(report: str, limit: int = 220) -> str:
    plain = re.sub(r"```[\s\S]*?```", " ", report or "")
    plain = re.sub(r"^#{1,6}\s*", "", plain, flags=re.M)
    plain = re.sub(r"[*_>`\-]+", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) <= limit:
        return plain or "报告已生成，请下载完整 PDF 查看。"
    brief = plain[:limit]
    brief = re.sub(r"[，。；、：,.!?！？][^，。；、：,.!?！？]*$", "", brief)
    return (brief or plain[:limit]).strip() + "…"


def _brief_report(cfg: AIConfig, report: str) -> str:
    if not cfg.ready:
        return _local_brief(report)
    system = (
        "你是直播复盘报告编辑。请把完整报告压缩成一段中文简报，"
        "必须控制在 180 到 220 个汉字左右，只保留结论、核心发现和最重要建议。"
        "不要写标题，不要 Markdown，不要寒暄，不要说“根据报告”。"
    )
    user = "完整报告如下：\n\n" + (report or "")[:30_000]
    try:
        brief = _chat_completion(
            cfg,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=500,
        )
    except AIReportError:
        return _local_brief(report)
    brief = re.sub(r"\s+", " ", brief).strip()
    brief = re.sub(r"^(好的|以下是|简报[:：]?|复盘简报[:：]?)\s*", "", brief)
    return _local_brief(brief, 230)


def _build_chunks(bundles: list[export_mod.RoomBundle]) -> tuple[list[dict[str, object]], bool]:
    chunks: list[dict[str, object]] = []
    truncated = False
    current_lines: list[str] = []
    current_rooms: set[str] = set()
    current_chars = 0

    def flush() -> None:
        nonlocal current_lines, current_rooms, current_chars
        if not current_lines:
            return
        chunks.append({
            "rooms": sorted(current_rooms),
            "text": "\n".join(current_lines),
            "char_count": current_chars,
        })
        current_lines = []
        current_rooms = set()
        current_chars = 0

    for bundle in bundles:
        for item in bundle.transcripts:
            line = _transcript_line(bundle, item)
            if current_lines and current_chars + len(line) > CHUNK_CHAR_LIMIT:
                flush()
                if len(chunks) >= MAX_CHUNKS:
                    truncated = True
                    return chunks, truncated
            current_lines.append(line)
            current_rooms.add(bundle.rid)
            current_chars += len(line)
    flush()
    if len(chunks) > MAX_CHUNKS:
        truncated = True
        chunks = chunks[:MAX_CHUNKS]
    return chunks, truncated


def _json_from_text(text: str) -> dict[str, object]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be object")
    return data


def _normalize_summary(data: dict[str, object]) -> dict[str, object]:
    keys = {
        "themes": [],
        "selling_points": [],
        "prices": [],
        "promotions": [],
        "audience_questions": [],
        "risk_claims": [],
        "evidence": [],
    }
    out: dict[str, object] = {}
    for key, default in keys.items():
        value = data.get(key, default)
        out[key] = value if isinstance(value, list) else default
    out["summary"] = str(data.get("summary") or "").strip()
    return out


def _summarize_chunk(cfg: AIConfig, chunk: dict[str, object]) -> dict[str, object]:
    system = (
        "你是直播复盘分析师。只根据用户给出的直播转写证据做结构化提取，"
        "不能编造价格、活动、产品名。输出必须是 JSON，不要 Markdown。"
    )
    schema_hint = (
        "返回 JSON 对象，字段：summary 字符串；themes/selling_points/prices/"
        "promotions/audience_questions/risk_claims/evidence 均为数组。"
        "evidence 每项包含 time、quote、type。quote 必须来自输入原文。"
    )
    user = f"{schema_hint}\n\n直播转写片段：\n{chunk['text']}"
    for attempt in range(2):
        try:
            content = _chat_completion(
                cfg,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=2200,
            )
        except AIReportError as exc:
            return _local_fallback_summary(chunk, f"AI 分段摘要请求失败，已降级使用原文证据：{exc}")
        try:
            return _normalize_summary(_json_from_text(content))
        except ValueError:
            user = "上一次输出不是合法 JSON。请只返回 JSON 对象，不要解释。\n\n" + user
            if attempt == 1:
                return _local_fallback_summary(chunk, "AI 分段摘要不是合法 JSON，已降级使用原文证据。")
    return _local_fallback_summary(chunk, "AI 分段摘要失败，已降级使用原文证据。")


def _local_fallback_summary(chunk: dict[str, object], reason: str | None = None) -> dict[str, object]:
    lines = str(chunk.get("text") or "").splitlines()
    evidence = []
    for line in lines[:5]:
        evidence.append({"time": line[0:32], "quote": line[-180:], "type": "原文样本"})
    return {
        "summary": reason or "本段保留原文样本，未调用 AI 结构化分析。",
        "themes": [],
        "selling_points": [],
        "prices": [],
        "promotions": [],
        "audience_questions": [],
        "risk_claims": [],
        "evidence": evidence,
        "_fallback": True,
        "_fallback_reason": reason or "未调用 AI 结构化分析。",
    }


def _final_report(cfg: AIConfig, overviews: list[dict[str, object]], summaries: list[dict[str, object]], truncated: bool) -> str:
    live_ops_knowledge = _load_live_ops_knowledge()
    system = (
        "你是直播复盘侠的首席直播增长分析师，输出要像一份可直接给管理层看的咨询报告。"
        "根据结构化摘要生成中文 Markdown 报告，要求有标题层级、结论优先、数据看板、证据引用和行动清单。"
        "语气专业、克制、判断明确；禁止出现“好的”“老板”“以下是”“根据你提供”等对话式套话。"
        "第一行必须是 Markdown 一级标题。所有关键结论必须引用 evidence 中的时间或原文；没有证据就写“证据不足”。"
        "不要编造不存在的信息，不要引入 GMV、订单、ROI、成交归因、销售额。"
        "直播优化策略必须结合给定直播运营知识库，但仍以本场真实证据为准。"
        "引用知识库时要区分 A/B/C 来源等级：A 级用于合规边界，B 级用于运营框架，C 级只作为项目经验。"
    )
    user = json.dumps(
        {
            "overviews": overviews,
            "chunk_summaries": summaries,
            "truncated": truncated,
            "live_operation_knowledge_base": live_ops_knowledge,
            "style_contract": {
                "tone": "专业咨询报告，不要流水账",
                "format": "Markdown，标题清晰，重点用粗体，列表短而有力",
                "must_have": [
                    "一页总览",
                    "直播数据看板",
                    "核心洞察",
                    "观众兴趣与问题",
                    "话术资产",
                    "低效/空转片段",
                    "风险复核",
                    "直播优化策略",
                ],
                "quality_bar": [
                    "先给结论，再给证据",
                    "每个结论尽量绑定一个数据、时间点或原话",
                    "区分事实、判断和建议",
                    "不要用空话，例如“继续保持”“加强互动”，必须说明怎么做",
                ],
            },
            "required_sections": [
                "一、一页总览：用 4-6 条项目符号概括本场表现、核心优化方向和最大问题",
                "二、直播数据看板：主播、话术段数、弹幕、在线峰值、累计/最新观看、核心事件，用表格呈现",
                "三、核心洞察：解释为什么这场直播有效或无效，至少 3 条",
                "四、观众兴趣与问题：整理观众最关心的主题、疑问、反复出现的词",
                "五、话术资产：列出可复用表达，必须带原话或时间点",
                "六、低效/空转片段：指出沉默、重复、弱互动、信息密度低的片段；证据不足则写无明确证据",
                "七、风险复核：只列真实敏感词或待复核项，不要过度判定",
                "八、直播优化策略：给出 3-5 个可执行策略，每条包含优化目标、执行方法、观察指标，并说明对应的知识库依据",
            ],
        },
        ensure_ascii=False,
    )
    report = _chat_completion(
        cfg,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.25,
        max_tokens=4200,
    )
    return _clean_report_markdown(report)


def _clean_report_markdown(text: str) -> str:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:markdown|md)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    lines = [line.rstrip() for line in raw.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    junk_patterns = [
        r"^好的[，,、\s]*(老板|老[板闆])?[，,、\s]*",
        r"^老板[，,、\s]*",
        r"^以下是[^\n]*[:：]?\s*",
        r"^根据你提供的[^\n]*[:：]?\s*",
        r"^---+$",
    ]
    while lines:
        first = lines[0].strip()
        changed = False
        for pat in junk_patterns:
            new_first = re.sub(pat, "", first, flags=re.I)
            if new_first != first:
                if new_first:
                    lines[0] = new_first
                else:
                    lines.pop(0)
                changed = True
                break
        if not changed:
            break
    cleaned = "\n".join(lines).strip()
    if cleaned and not cleaned.startswith("#"):
        cleaned = "# AI 直播复盘报告\n\n" + cleaned
    return cleaned + "\n"


def _fallback_markdown(overviews: list[dict[str, object]], summaries: list[dict[str, object]], truncated: bool) -> str:
    lines = ["# AI 直播复盘报告", ""]
    lines.append("> 该报告为本地兜底版本，未完成 AI 总结。")
    if truncated:
        lines.append("> 注意：本次只处理了前半部分文本，完整长直播需分批继续分析。")
    lines += ["", "## 数据概览"]
    for item in overviews:
        lines.append(
            f"- {item['nickname']}：话术 {item['transcript_segments']} 段，"
            f"{item['transcript_chars']} 字，弹幕 {item['chat_count']} 条，"
            f"在线峰值 {item['online_peak']}"
        )
    lines += ["", "## 原文证据样本"]
    for idx, summary in enumerate(summaries, start=1):
        lines.append(f"### 片段 {idx}")
        for ev in summary.get("evidence", [])[:5]:
            if isinstance(ev, dict):
                lines.append(f"- {ev.get('time', '')}：{ev.get('quote', '')}")
    return "\n".join(lines) + "\n"


def _safe_report_name(base: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().rstrip(" .")
    return (stem or "AI复盘")[:80]


def _short_text(text: object, limit: int = 18) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[: max(1, limit - 1)] + "…"


def _event_count(overview: dict[str, object], names: tuple[str, ...]) -> int:
    counts = overview.get("event_counts")
    if not isinstance(counts, dict):
        return 0
    total = 0
    for key, value in counts.items():
        lk = str(key).lower()
        if any(name in lk for name in names):
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                pass
    return total


def _overview_metrics(overviews: list[dict[str, object]]) -> dict[str, int]:
    return {
        "rooms": len(overviews),
        "transcripts": sum(int(o.get("transcript_segments") or 0) for o in overviews),
        "chars": sum(int(o.get("transcript_chars") or 0) for o in overviews),
        "chats": sum(int(o.get("chat_count") or 0) for o in overviews),
        "peak": max((int(o.get("online_peak") or 0) for o in overviews), default=0),
        "pv": max((int(o.get("platform_pv_latest") or 0) for o in overviews), default=0),
        "likes": sum(_event_count(o, ("like", "digg", "点赞")) for o in overviews),
        "member": sum(_event_count(o, ("member", "enter", "进场")) for o in overviews),
        "fansclub": sum(_event_count(o, ("fansclub", "club", "粉丝团")) for o in overviews),
    }


_TECH_LABELS = {
    "chat_count": "弹幕量",
    "online_peak": "峰值在线",
    "platform_pv_latest": "累计观看",
    "transcript_segments": "话术片段",
    "transcript_chars": "话术字数",
    "audience_questions": "观众问题",
    "member": "进场",
    "fansclub": "粉丝团",
    "social": "关注/互动",
    "room_id": "直播间",
}


def _clean_report_text(text: object) -> str:
    """Make AI report wording readable for operators instead of developers."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = re.sub(r"^[*•·\-\s]+", "", s).strip()
    s = s.replace("`", "")
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"(?<!\*)\*(?!\*)", "", s)
    for raw, label in _TECH_LABELS.items():
        s = re.sub(rf"\b{re.escape(raw)}\b", label, s, flags=re.I)
    s = re.sub(r"\bnull\b", "暂无", s, flags=re.I)
    s = re.sub(r"\bNone\b", "暂无", s)
    s = re.sub(r"\s+([，。；：、）])", r"\1", s)
    s = re.sub(r"([（])\s+", r"\1", s)
    return s.strip()


def _metric_score(value: int | float, target: int | float) -> int:
    try:
        value_f = max(0.0, float(value))
        target_f = max(1.0, float(target))
    except (TypeError, ValueError):
        return 0
    return int(round(min(math.log1p(value_f) / math.log1p(target_f), 1.0) * 100))


def _dimension_scores(metrics: dict[str, int]) -> list[dict[str, object]]:
    """Create a lightweight six-dimension visual diagnosis for report readability."""
    heat = max(
        _metric_score(metrics.get("peak", 0), 500),
        _metric_score(metrics.get("pv", 0), 5000),
        _metric_score(metrics.get("member", 0), 3000),
    )
    interact = max(
        _metric_score(metrics.get("chats", 0), 2500),
        _metric_score(metrics.get("likes", 0), 2000),
    )
    content = max(
        _metric_score(metrics.get("transcripts", 0), 80),
        _metric_score(metrics.get("chars", 0), 20000),
    )
    relation = max(
        _metric_score(metrics.get("fansclub", 0), 200),
        _metric_score(metrics.get("member", 0), 3000),
    )
    evidence = max(
        _metric_score(metrics.get("transcripts", 0), 60),
        _metric_score(metrics.get("chats", 0), 1000),
    )
    risk_ready = 78 if metrics.get("transcripts", 0) or metrics.get("chats", 0) else 42
    return [
        {"label": "直播热度", "score": heat, "desc": "观看、进场、在线峰值"},
        {"label": "互动活跃", "score": interact, "desc": "弹幕、点赞、讨论密度"},
        {"label": "内容质量", "score": content, "desc": "话术覆盖与信息密度"},
        {"label": "粉丝粘性", "score": relation, "desc": "进场与强关系互动"},
        {"label": "证据完整", "score": evidence, "desc": "数据与话术可追溯性"},
        {"label": "风险可控", "score": risk_ready, "desc": "敏感表达复核基础"},
    ]


def _radar_points(scores: list[dict[str, object]], radius: float = 74.0, center: float = 90.0) -> str:
    if not scores:
        return ""
    pts: list[str] = []
    total = len(scores)
    for idx, item in enumerate(scores):
        score = max(0, min(100, int(item.get("score") or 0)))
        angle = -math.pi / 2 + idx * (2 * math.pi / total)
        r = radius * (score / 100.0)
        pts.append(f"{center + math.cos(angle) * r:.1f},{center + math.sin(angle) * r:.1f}")
    return " ".join(pts)


def _dimension_visual_html(scores: list[dict[str, object]]) -> str:
    if not scores:
        return ""
    points = _radar_points(scores)
    axis_lines = []
    label_nodes = []
    center = 90.0
    radius = 74.0
    total = len(scores)
    for idx, item in enumerate(scores):
        angle = -math.pi / 2 + idx * (2 * math.pi / total)
        x = center + math.cos(angle) * radius
        y = center + math.sin(angle) * radius
        lx = center + math.cos(angle) * (radius + 19)
        ly = center + math.sin(angle) * (radius + 19)
        axis_lines.append(f'<line x1="{center}" y1="{center}" x2="{x:.1f}" y2="{y:.1f}"/>')
        label_nodes.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle">{html.escape(str(item["label"]))}</text>'
        )
    bars = []
    for item in scores:
        score = max(0, min(100, int(item.get("score") or 0)))
        bars.append(
            '<div class="dim-row">'
            f'<div><b>{html.escape(str(item["label"]))}</b><span>{html.escape(str(item.get("desc") or ""))}</span></div>'
            f'<em>{score}</em><i><u style="width:{score}%"></u></i>'
            '</div>'
        )
    return (
        '<section class="diagnostic-grid">'
        '<div class="diagnostic-card radar-card"><h2>六维运营诊断</h2>'
        '<svg class="radar-chart" viewBox="0 0 180 180" role="img" aria-label="六维分析雷达图">'
        '<polygon class="radar-bg" points="90,16 154,53 154,127 90,164 26,127 26,53"/>'
        '<polygon class="radar-mid" points="90,41 132,65 132,115 90,139 48,115 48,65"/>'
        f'{"".join(axis_lines)}<polygon class="radar-area" points="{points}"/>{"".join(label_nodes)}'
        '</svg></div>'
        '<div class="diagnostic-card"><h2>关键指标强弱</h2><div class="dim-list">'
        f'{"".join(bars)}</div></div>'
        '</section>'
    )


def _mini_radar_panel(scores: list[dict[str, object]]) -> str:
    points = _radar_points(scores, radius=58, center=72)
    axis_lines = []
    labels = []
    center = 72.0
    radius = 58.0
    total = len(scores) or 1
    for idx, item in enumerate(scores):
        angle = -math.pi / 2 + idx * (2 * math.pi / total)
        x = center + math.cos(angle) * radius
        y = center + math.sin(angle) * radius
        lx = center + math.cos(angle) * (radius + 14)
        ly = center + math.sin(angle) * (radius + 14)
        axis_lines.append(f'<line x1="{center}" y1="{center}" x2="{x:.1f}" y2="{y:.1f}"/>')
        labels.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle">{html.escape(str(item["label"])[:4])}</text>')
    return (
        '<div class="mini-panel mini-radar-panel"><h3>六维诊断</h3>'
        '<svg class="mini-radar" viewBox="0 0 144 144" role="img" aria-label="六维分析雷达图">'
        '<polygon class="radar-bg" points="72,14 122,43 122,101 72,130 22,101 22,43"/>'
        '<polygon class="radar-mid" points="72,34 105,53 105,91 72,110 39,91 39,53"/>'
        f'{"".join(axis_lines)}<polygon class="radar-area" points="{points}"/>{"".join(labels)}'
        '</svg></div>'
    )


def _mini_strength_panel(scores: list[dict[str, object]], limit: int = 4) -> str:
    rows = []
    for item in scores[:limit]:
        score = max(0, min(100, int(item.get("score") or 0)))
        rows.append(
            '<div class="mini-bar-row">'
            f'<b>{html.escape(str(item["label"]))}</b><span><i style="width:{score}%"></i></span><em>{score}</em>'
            '</div>'
        )
    return '<div class="mini-panel"><h3>指标强弱</h3>' + "".join(rows) + "</div>"


def _mini_data_panel(rows: list[str]) -> str:
    body = "".join(rows[:4])
    return (
        '<div class="mini-panel mini-table-panel"><h3>数据看板</h3>'
        '<table><thead><tr><th>主播</th><th>峰值</th><th>弹幕</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


def _mini_word_panel(word_html: list[str]) -> str:
    return '<div class="mini-panel"><h3>高频词</h3>' + ("".join(word_html[:7]) or "<p>暂无高频词。</p>") + "</div>"


def _mini_visual_panel(visual: str | None) -> str:
    if not visual:
        return '<div class="mini-panel"><h3>画面线索</h3><p>暂无画面。</p></div>'
    return f'<div class="mini-panel mini-shot-panel"><h3>画面线索</h3>{visual}</div>'


def _metrics_from_report_html(raw_html: str) -> dict[str, int]:
    metrics = {"rooms": 0, "transcripts": 0, "chars": 0, "chats": 0, "peak": 0, "pv": 0, "likes": 0, "member": 0, "fansclub": 0}
    label_map = {
        "复盘主播": "rooms",
        "话术片段": "transcripts",
        "弹幕/评论": "chats",
        "峰值在线": "peak",
        "累计观看": "pv",
    }
    for value, label in re.findall(r"<b>\s*([\d,]+)\s*</b>\s*<span>\s*([^<]+)\s*</span>", raw_html):
        key = label_map.get(html.unescape(label).strip())
        if key:
            try:
                metrics[key] = max(metrics.get(key, 0), int(value.replace(",", "")))
            except ValueError:
                pass
    for label, key in (("话术片段", "transcripts"), ("弹幕", "chats"), ("峰值在线", "peak"), ("累计观看", "pv")):
        found = re.findall(rf"{label}</(?:th|td)>\s*<td[^>]*>\s*([\d,]+)", raw_html)
        for value in found:
            try:
                metrics[key] = max(metrics.get(key, 0), int(value.replace(",", "")))
            except ValueError:
                pass
    return metrics


def _find_avatar_path(rid: object) -> Path | None:
    rid_s = str(rid or "").strip()
    if not rid_s:
        return None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = config.AVATAR_CACHE_DIR / f"{rid_s}{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg  # type: ignore

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return "ffmpeg"


def _extract_frame(video_path: Path, out_path: Path, at_sec: int = 6) -> Path | None:
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg_exe(),
        "-y",
        "-ss",
        str(max(0, at_sec)),
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        return None
    if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    return None


def _visual_assets(bundles: list[export_mod.RoomBundle], out_dir: Path) -> list[dict[str, object]]:
    assets: list[dict[str, object]] = []
    frame_dir = out_dir / "_frames"
    for bundle in bundles:
        rid = str(bundle.rid)
        video_room = config.VIDEO_DIR / rid
        frames: list[Path] = []
        if video_room.exists():
            videos = sorted(
                [p for p in video_room.glob("*.mp4") if p.stat().st_size > 50_000],
                key=lambda p: p.stat().st_mtime,
            )
            for idx, video in enumerate(videos[:2]):
                frame = _extract_frame(video, frame_dir / f"{rid}_{idx + 1}.jpg", at_sec=6)
                if frame:
                    frames.append(frame)
        avatar = _find_avatar_path(rid)
        assets.append({
            "rid": rid,
            "nickname": bundle.nickname or rid,
            "frames": frames,
            "avatar": avatar,
        })
    return assets


def _markdown_body_html(
    markdown: str,
    inline_visuals: list[str] | None = None,
    inline_panels: list[str] | None = None,
) -> str:
    def inline_md(text: str) -> str:
        escaped = html.escape(_clean_report_text(text))
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        return escaped

    chunks: list[str] = []
    in_list = False
    table_rows: list[list[str]] = []
    section_idx = 0
    section_open = False
    paragraph_idx = 0
    visuals = inline_visuals or []
    panels = inline_panels or []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            chunks.append("</ul>")
            in_list = False

    def close_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        header, *body = table_rows
        head = "".join(f"<th>{inline_md(c)}</th>" for c in header)
        rows = []
        for row in body:
            cells = "".join(f"<td>{inline_md(c)}</td>" for c in row)
            rows.append(f"<tr>{cells}</tr>")
        chunks.append(
            "<div class=\"data-table-wrap\"><table class=\"data-table\">"
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
        table_rows = []

    def close_section() -> None:
        nonlocal section_open
        close_list()
        close_table()
        if section_open:
            chunks.append("</article>")
            section_open = False

    def maybe_visual() -> None:
        idx = section_idx - 1
        if idx < len(panels) and panels[idx]:
            side = "right" if idx % 2 == 0 else "left"
            chunks.append(f"<aside class=\"report-float-panel {side}\">{panels[idx]}</aside>")
            return
        if not visuals:
            return
        if idx in (0, 1, 3) and idx < len(visuals):
            side = "right" if idx % 2 == 0 else "left"
            chunks.append(f"<aside class=\"report-float-panel {side}\"><div class=\"float-shot\">{visuals[idx]}</div></aside>")

    def is_table_line(line: str) -> bool:
        return line.startswith("|") and line.endswith("|") and line.count("|") >= 2

    def parse_table_line(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    for raw in (markdown or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            close_list()
            close_table()
            continue
        if line.startswith("# "):
            close_section()
            chunks.append(f"<h1>{inline_md(line[2:])}</h1>")
        elif line.startswith("## "):
            close_section()
            section_idx += 1
            paragraph_idx = 0
            section_open = True
            chunks.append(f"<article class=\"story-section section-{section_idx}\"><h2>{inline_md(line[3:])}</h2>")
            maybe_visual()
        elif line.startswith("### "):
            close_list()
            close_table()
            chunks.append(f"<h3>{inline_md(line[4:])}</h3>")
        elif is_table_line(line):
            close_list()
            cells = parse_table_line(line)
            if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            table_rows.append(cells)
        elif re.match(r"^[-*]\s+", line):
            close_table()
            if not in_list:
                chunks.append("<ul class=\"insight-list\">")
                in_list = True
            chunks.append(f"<li>{inline_md(re.sub(r'^[-*]\\s+', '', line))}</li>")
        elif re.match(r"^\d+[.、]\s*", line):
            close_list()
            close_table()
            num, text = re.split(r"[.、]\s*", line, maxsplit=1)
            chunks.append(f"<div class=\"step-card\"><b>{html.escape(num)}</b><span>{inline_md(text)}</span></div>")
        elif line.startswith(">"):
            close_list()
            close_table()
            chunks.append(f"<blockquote class=\"quote-card\">{inline_md(line.lstrip('> '))}</blockquote>")
        else:
            close_list()
            close_table()
            paragraph_idx += 1
            cls = "lead" if paragraph_idx == 1 else ("insight-bubble" if ("最大" in line or "建议" in line or "问题" in line or "机会" in line) else "")
            cls_attr = f" class=\"{cls}\"" if cls else ""
            chunks.append(f"<p{cls_attr}>{inline_md(line)}</p>")
    close_section()
    return "\n".join(chunks)


def _write_html_report(
    markdown: str,
    path: Path,
    *,
    overviews: list[dict[str, object]],
    words: list[dict[str, object]],
    visual_assets: list[dict[str, object]],
) -> None:
    metrics = _overview_metrics(overviews)
    top_words = words[:12]
    max_word = max((int(w.get("count") or 0) for w in top_words), default=1)
    rows = []
    for o in overviews[:8]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(_short_text(o.get('nickname'), 24))}</td>"
            f"<td>{int(o.get('online_peak') or 0)}</td>"
            f"<td>{int(o.get('platform_pv_latest') or 0)}</td>"
            f"<td>{int(o.get('chat_count') or 0)}</td>"
            f"<td>{int(o.get('transcript_segments') or 0)}</td>"
            "</tr>"
        )
    visuals = []
    for asset in visual_assets[:6]:
        img = None
        frames = asset.get("frames")
        if isinstance(frames, list) and frames:
            img = frames[0]
        elif asset.get("avatar"):
            img = asset.get("avatar")
        if isinstance(img, Path) and img.exists():
            rel = img.resolve().as_uri()
            visuals.append(
                f"<figure><img src=\"{html.escape(rel)}\"><figcaption>{html.escape(_short_text(asset.get('nickname'), 20))}</figcaption></figure>"
            )
        else:
            visuals.append(
                f"<figure class=\"empty-shot\"><div>暂无画面</div><figcaption>{html.escape(_short_text(asset.get('nickname'), 20))}</figcaption></figure>"
            )
    word_html = []
    for w in top_words:
        count = int(w.get("count") or 0)
        width = max(6, min(100, round(count / max_word * 100)))
        word_html.append(
            f"<div class=\"word\"><b>{html.escape(str(w.get('word') or ''))}</b><span><i style=\"width:{width}%\"></i></span><em>{count}</em></div>"
        )
    score_panels = _dimension_scores(metrics)
    mini_rows = []
    for o in overviews[:4]:
        mini_rows.append(
            "<tr>"
            f"<td>{html.escape(_short_text(o.get('nickname'), 10))}</td>"
            f"<td>{int(o.get('online_peak') or 0)}</td>"
            f"<td>{int(o.get('chat_count') or 0)}</td>"
            "</tr>"
        )
    inline_visuals = visuals[:4]
    rail_visuals = visuals[4:] or visuals[:2]
    inline_panels = [
        _mini_radar_panel(score_panels),
        _mini_data_panel(mini_rows),
        _mini_strength_panel(score_panels),
        _mini_word_panel(word_html),
        _mini_visual_panel(inline_visuals[0] if inline_visuals else None),
        _mini_strength_panel(score_panels[2:] + score_panels[:2], limit=4),
        _mini_word_panel(word_html[3:] or word_html),
        _mini_visual_panel(inline_visuals[1] if len(inline_visuals) > 1 else None),
    ]
    body = _markdown_body_html(markdown, inline_visuals=inline_visuals, inline_panels=inline_panels)
    html_doc = f"""<!doctype html>
<html lang="zh-CN" data-report-version="2"><head><meta charset="utf-8"><title>直播复盘侠 AI 报告</title>
<style>
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:linear-gradient(180deg,#eaf1f8,#f6f9fd 42%,#eef4fb);color:#172033;font:14px/1.75 "Microsoft YaHei",Arial,sans-serif}}
.page{{max-width:1160px;margin:0 auto;padding:34px}}.hero{{background:linear-gradient(135deg,#101828,#20345d 62%,#265a75);color:#fff;border-radius:24px;padding:30px 34px;box-shadow:0 18px 48px rgba(16,24,40,.18);position:relative;overflow:hidden}}
.hero:after{{content:"";position:absolute;right:-80px;top:-110px;width:310px;height:310px;border-radius:50%;background:radial-gradient(circle,rgba(53,212,255,.30),transparent 66%)}}
.hero h1{{margin:0 0 8px;font-size:30px}}.hero p{{margin:0;color:#cbd7ea}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:18px}}.kpi{{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);border-radius:16px;padding:14px}}.kpi b{{display:block;font-size:26px}}.kpi span{{color:#cbd7ea;font-size:12px}}
.grid{{display:grid;grid-template-columns:1.15fr .85fr;gap:18px;margin-top:18px}}.card{{background:#fff;border:1px solid #e7eef7;border-radius:20px;padding:20px;box-shadow:0 10px 28px rgba(31,41,55,.07)}}h2{{font-size:18px;margin:0 0 14px;padding-left:10px;border-left:4px solid #05d0ff}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #edf1f5;text-align:left}}th{{color:#7c8798;background:#f7faff}}.word{{display:grid;grid-template-columns:72px 1fr 34px;gap:8px;align-items:center;margin:8px 0}}.word b{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.word span{{height:8px;background:#e9eef8;border-radius:99px;overflow:hidden}}.word i{{display:block;height:100%;background:linear-gradient(90deg,#35d4ff,#5b61ff);border-radius:99px}}.word em{{font-style:normal;color:#738095;text-align:right}}
.diagnostic-grid{{display:grid;grid-template-columns:.92fr 1.08fr;gap:18px;margin-top:18px}}.diagnostic-card{{background:#fff;border:1px solid #e7eef7;border-radius:20px;padding:20px;box-shadow:0 10px 28px rgba(31,41,55,.07);min-height:250px}}.radar-card{{display:flex;flex-direction:column;align-items:center}}.radar-chart{{width:min(100%,310px);height:230px;overflow:visible}}.radar-chart line{{stroke:#d9e7f5;stroke-width:1}}.radar-bg{{fill:#f5f9ff;stroke:#d7e5f5;stroke-width:1}}.radar-mid{{fill:none;stroke:#e4edf8;stroke-width:1}}.radar-area{{fill:rgba(53,212,255,.20);stroke:#4b6cff;stroke-width:3;filter:drop-shadow(0 8px 12px rgba(75,108,255,.18))}}.radar-chart text{{font-size:10px;fill:#6b7890}}.dim-list{{display:grid;gap:12px}}.dim-row{{display:grid;grid-template-columns:132px 38px 1fr;gap:12px;align-items:center}}.dim-row b{{display:block;color:#172033}}.dim-row span{{display:block;font-size:12px;color:#7b8798;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.dim-row em{{font-style:normal;font-weight:800;color:#3859ff;text-align:right}}.dim-row i{{height:9px;border-radius:99px;background:#e9eff8;overflow:hidden}}.dim-row u{{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#35d4ff,#5b61ff);text-decoration:none}}
figure{{margin:0;border-radius:18px;overflow:hidden;background:#f7faff;border:1px solid #e6edf6;box-shadow:0 12px 26px rgba(31,41,55,.08)}}figure img{{width:100%;height:158px;object-fit:cover;display:block}}figcaption{{padding:8px 10px;color:#536176;font-size:12px}}.empty-shot div{{height:158px;display:flex;align-items:center;justify-content:center;color:#9aa5b4;background:linear-gradient(135deg,#f4f8ff,#edf3fb)}}
.report-shell{{margin-top:18px}}.visual-rail{{display:none}}.report{{min-width:0;padding:0;background:transparent;border:none;box-shadow:none}}.report h1{{display:none}}
.story-section{{background:#fff;border:1px solid #e7eef7;border-radius:22px;padding:24px 26px;margin-bottom:18px;box-shadow:0 10px 28px rgba(31,41,55,.06);overflow:hidden;opacity:1;transform:none;animation:sectionRise .55s ease both}}.story-section.visible{{opacity:1;transform:translateY(0)}}@keyframes sectionRise{{from{{opacity:.72;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}.story-section h2{{font-size:21px;margin:0 0 16px}}.story-section h3{{font-size:15px;margin:18px 0 8px;color:#22304a}}.story-section p,.story-section li{{color:#2d3748}}.story-section .lead{{font-size:15px;line-height:1.9;color:#1f2a44;background:linear-gradient(135deg,#f7fbff,#f2f6ff);border:1px solid #e4ecfb;border-radius:16px;padding:14px 16px}}.insight-list,.story-section ul{{display:grid;gap:9px;padding:0;margin:12px 0;list-style:none}}.insight-list li,.story-section ul li{{position:relative;background:#fbfdff;border:1px solid #e7edf7;border-radius:14px;padding:10px 12px 10px 30px}}.insight-list li:before,.story-section ul li:before{{content:"";position:absolute;left:12px;top:17px;width:7px;height:7px;border-radius:50%;background:#35d4ff;box-shadow:0 0 0 4px rgba(53,212,255,.12)}}.insight-bubble{{display:inline-block;max-width:92%;border-radius:18px;background:#fff8e8;border:1px solid #ffe0a8;color:#4b3b14!important;padding:10px 14px;box-shadow:0 8px 18px rgba(245,158,11,.08)}}.quote-card{{background:#f7faff;border-left:4px solid #5b61ff;margin:14px 0;padding:12px 14px;border-radius:0 14px 14px 0;color:#536176}}
.step-card{{display:grid;grid-template-columns:36px 1fr;gap:12px;align-items:start;margin:12px 0;padding:12px 14px;border:1px solid #e6edf7;background:#fff;border-radius:15px}}.step-card b{{width:30px;height:30px;display:flex;align-items:center;justify-content:center;border-radius:10px;background:linear-gradient(135deg,#35d4ff,#5b61ff);color:#fff}}.step-card span{{color:#2d3748}}.data-table-wrap{{overflow:auto;border:1px solid #e7edf7;border-radius:14px;margin:12px 0;background:#fff}}.data-table th,.data-table td{{white-space:nowrap}}.float-shot figure img{{height:168px}}.section-2{{background:linear-gradient(180deg,#fff,#fbfdff)}}.section-4{{background:linear-gradient(135deg,#fff,#f8fbff)}}.report-float-panel{{width:318px;max-width:42%;margin:2px 0 14px 22px;position:relative;z-index:1}}.report-float-panel.right{{float:right}}.report-float-panel.left{{float:left;margin:2px 22px 14px 0}}.mini-panel{{background:linear-gradient(180deg,#fff,#f8fbff);border:1px solid #e2ebf7;border-radius:18px;padding:14px;box-shadow:0 10px 24px rgba(31,41,55,.08)}}.mini-panel h3{{margin:0 0 10px;font-size:14px;color:#172033}}.mini-radar-panel{{text-align:center}}.mini-radar{{width:100%;height:172px;overflow:visible}}.mini-radar line{{stroke:#d9e7f5;stroke-width:1}}.mini-radar text{{font-size:9px;fill:#697993}}.mini-bar-row{{display:grid;grid-template-columns:72px 1fr 30px;gap:8px;align-items:center;margin:9px 0}}.mini-bar-row b{{font-size:12px;white-space:nowrap}}.mini-bar-row span{{height:7px;border-radius:99px;background:#e9eff8;overflow:hidden}}.mini-bar-row i{{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#35d4ff,#5b61ff)}}.mini-bar-row em{{font-style:normal;font-size:12px;color:#5366e8;text-align:right;font-weight:800}}.mini-table-panel table{{font-size:12px}}.mini-table-panel th,.mini-table-panel td{{padding:7px 6px}}.mini-shot-panel figure img{{height:150px}}.story-section:after{{content:"";display:block;clear:both}}
.jumpbar{{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 0}}.jumpbar a{{color:#dbeafe;text-decoration:none;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.10);border-radius:999px;padding:6px 11px;font-size:12px}}.connector-note{{display:flex;align-items:center;gap:8px;color:#65758d;font-size:12px;margin:10px 0 0}}.connector-note i{{height:2px;width:54px;background:linear-gradient(90deg,#35d4ff,#fff,#5b61ff);border-radius:99px}}
@media(max-width:900px){{.page{{padding:18px}}.kpis,.grid,.diagnostic-grid{{grid-template-columns:1fr}}.report-float-panel,.report-float-panel.left,.report-float-panel.right{{float:none;width:100%;max-width:100%;margin:12px 0}}}}
@page{{size:A4;margin:10mm 9mm}}@media print{{html{{scroll-behavior:auto}}body{{background:#fff!important;color:#172033;font-size:11px;line-height:1.62;-webkit-print-color-adjust:exact;print-color-adjust:exact}}.page{{width:192mm;max-width:none;margin:0 auto;padding:0}}.hero{{border-radius:14px;padding:18px 20px;box-shadow:none;break-inside:avoid;page-break-inside:avoid}}.hero h1{{font-size:22px}}.hero p{{font-size:11px}}.hero:after{{display:none}}.jumpbar{{display:none}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr)!important;gap:8px;margin-top:12px}}.kpi{{border-radius:10px;padding:8px 10px}}.kpi b{{font-size:18px}}.grid,.diagnostic-grid{{grid-template-columns:1fr 1fr;gap:9px;margin-top:10px}}.card,.diagnostic-card,.story-section,.mini-panel,figure{{box-shadow:none!important;border-color:#dfe7f2!important}}.diagnostic-card{{min-height:0;padding:12px;border-radius:12px}}.radar-chart{{height:150px}}.dim-row{{grid-template-columns:84px 28px 1fr;gap:7px;margin:5px 0}}.dim-row span{{display:none}}figure{{border-radius:10px}}figure img,.empty-shot div{{height:102px}}figcaption{{font-size:9px;padding:5px 7px}}.report-shell{{margin-top:10px}}.story-section{{break-inside:avoid;page-break-inside:avoid;border-radius:14px;padding:15px 17px;margin:0 0 10px;animation:none!important;overflow:visible}}.story-section h2{{font-size:15px;margin-bottom:10px;border-left-width:3px}}.story-section h3{{font-size:12px;margin:10px 0 5px}}.story-section p,.story-section li{{font-size:10.2px;line-height:1.62}}.story-section .lead{{font-size:11px;line-height:1.66;padding:9px 10px;border-radius:10px}}.insight-list,.story-section ul{{gap:5px;margin:7px 0}}.insight-list li,.story-section ul li{{padding:7px 9px 7px 22px;border-radius:10px;break-inside:avoid}}.insight-list li:before,.story-section ul li:before{{left:9px;top:13px;width:5px;height:5px;box-shadow:0 0 0 3px rgba(53,212,255,.10)}}.step-card{{grid-template-columns:28px 1fr;gap:8px;margin:7px 0;padding:8px 10px;border-radius:10px;break-inside:avoid}}.step-card b{{width:24px;height:24px;border-radius:8px}}.report-float-panel,.report-float-panel.left,.report-float-panel.right{{float:none!important;width:100%!important;max-width:100%!important;margin:8px 0!important;break-inside:avoid}}.report-float-panel .mini-panel,.report-float-panel .diagnostic-card{{padding:10px;border-radius:12px;margin-bottom:7px}}.mini-radar,.report-float-panel .radar-chart{{height:135px}}.mini-bar-row{{grid-template-columns:58px 1fr 24px;margin:5px 0;gap:6px}}.mini-bar-row b,.mini-bar-row em{{font-size:9px}}.mini-table-panel table,.report-float-panel table{{font-size:9px}}.mini-table-panel th,.mini-table-panel td,.report-float-panel th,.report-float-panel td,th,td{{padding:5px 6px}}.data-table-wrap{{overflow:visible;border-radius:10px}}.quote-card{{margin:8px 0;padding:8px 10px;border-radius:0 9px 9px 0}}.connector-note{{display:none}}}}
</style></head><body><main class="page">
<section class="hero"><h1>直播复盘侠 AI 复盘报告</h1><p>话术、互动、热度与视觉证据的综合复盘</p>
<div class="kpis"><div class="kpi"><b>{metrics['rooms']}</b><span>复盘主播</span></div><div class="kpi"><b>{metrics['transcripts']}</b><span>话术片段</span></div><div class="kpi"><b>{metrics['chats']}</b><span>弹幕/评论</span></div><div class="kpi"><b>{metrics['peak']}</b><span>峰值在线</span></div></div><div class="jumpbar"><a href="#report">核心报告</a><a href="#words">高频词</a><a href="#visual">画面线索</a></div></section>
<section class="report-shell" id="report"><div class="report">{body}</div></section>
</main><script>
const observer=new IntersectionObserver(items=>items.forEach(item=>{{if(item.isIntersecting)item.target.classList.add('visible')}}),{{threshold:.12}});
document.querySelectorAll('.story-section').forEach(el=>observer.observe(el));
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")


_REPORT_V2_MARKER = 'data-report-version="2"'


def _legacy_table_html(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    body_rows = rows[1:]
    thead = "".join(f"<th>{cell}</th>" for cell in header)
    tbody = []
    for row in body_rows:
        cells = row + [""] * max(0, len(header) - len(row))
        tbody.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells[: len(header)]) + "</tr>")
    return (
        '<div class="legacy-table-wrap"><table class="legacy-data-table">'
        f"<thead><tr>{thead}</tr></thead><tbody>{''.join(tbody)}</tbody>"
        "</table></div>"
    )


def _legacy_table_line_to_cells(line: str) -> list[str]:
    line = re.sub(r"^<p>\s*|\s*</p>$", "", line.strip(), flags=re.I | re.S)
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return []
    return [cell.strip() for cell in line.strip("|").split("|")]


def _legacy_table_is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", re.sub(r"\s+", "", cell) or "") for cell in cells)


def _upgrade_legacy_tables(document: str) -> str:
    pattern = re.compile(r"((?:\s*<p>\s*\|.*?\|\s*</p>){2,})", re.S)

    def replace(match: re.Match[str]) -> str:
        lines = re.findall(r"<p>\s*\|.*?\|\s*</p>", match.group(1), flags=re.S)
        rows: list[list[str]] = []
        for line in lines:
            cells = _legacy_table_line_to_cells(line)
            if _legacy_table_is_separator(cells):
                continue
            if cells:
                rows.append(cells)
        return _legacy_table_html(rows) or match.group(1)

    return pattern.sub(replace, document)


def _upgrade_legacy_steps(document: str) -> str:
    pattern = re.compile(r"<p>\s*(\d+)[.、]\s*(.*?)</p>", re.S)

    def replace(match: re.Match[str]) -> str:
        num = html.escape(match.group(1))
        text = match.group(2).strip()
        return f'<div class="legacy-step-card"><b>{num}</b><span>{text}</span></div>'

    return pattern.sub(replace, document)


def _wrap_legacy_report_sections(document: str) -> str:
    pattern = re.compile(r'(<section class="card report)([^"]*)(".*?>)(.*?)(</section>)', re.S)
    match = pattern.search(document)
    if not match:
        return document
    prefix, classes, close_tag, content, suffix = match.groups()
    if "legacy-upgraded" in classes or "story-section" in content:
        return document

    parts = re.split(r"(?=<h2>)", content)
    rebuilt: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if part.lstrip().startswith("<h2>"):
            rebuilt.append(f'<article class="story-section visible legacy-section">{part}</article>')
        else:
            rebuilt.append(part)
    replacement = f'{prefix}{classes} legacy-upgraded{close_tag}{"".join(rebuilt)}{suffix}'
    return document[: match.start()] + replacement + document[match.end():]


def _legacy_report_upgrade_css() -> str:
    return """
<style id="livewatch-report-upgrade">
.legacy-upgraded.card.report{background:transparent!important;border:0!important;box-shadow:none!important;padding:0!important}
.legacy-section{background:#fff;border:1px solid #e7eef7;border-radius:22px;padding:24px 26px;margin-bottom:18px;box-shadow:0 12px 30px rgba(31,41,55,.07)}
.legacy-section h2{font-size:21px;margin:0 0 16px;padding-left:12px;border-left:4px solid #05d0ff}
.legacy-section p{line-height:1.9;color:#2d3748;margin:10px 0}
.legacy-section ul{display:grid;gap:10px;padding:0!important;margin:12px 0!important;list-style:none!important}
.legacy-section li{position:relative;background:linear-gradient(135deg,#fbfdff,#f6f9ff);border:1px solid #e7edf7;border-radius:15px;padding:12px 14px 12px 32px!important;color:#263247}
.legacy-section li:before{content:"";position:absolute;left:13px;top:20px;width:7px;height:7px;border-radius:50%;background:#35d4ff;box-shadow:0 0 0 4px rgba(53,212,255,.13)}
.legacy-section li::first-letter{color:transparent}
.legacy-step-card{display:grid;grid-template-columns:38px 1fr;gap:12px;align-items:start;margin:12px 0;padding:14px;border:1px solid #e4ebf7;background:linear-gradient(135deg,#fff,#f8fbff);border-radius:16px;box-shadow:0 8px 18px rgba(31,41,55,.045)}
.legacy-step-card b{width:32px;height:32px;display:flex;align-items:center;justify-content:center;border-radius:11px;background:linear-gradient(135deg,#35d4ff,#5b61ff);color:#fff}
.legacy-step-card span{line-height:1.9;color:#263247}
.legacy-table-wrap{overflow:auto;border:1px solid #e7edf7;border-radius:16px;margin:14px 0;background:#fff;box-shadow:0 8px 18px rgba(31,41,55,.04)}
.legacy-data-table{width:100%;border-collapse:collapse}
.legacy-data-table th{background:#f4f8ff;color:#637089;font-weight:700}
.legacy-data-table th,.legacy-data-table td{padding:11px 13px;border-bottom:1px solid #edf1f7;text-align:left;vertical-align:top}
.legacy-data-table tr:last-child td{border-bottom:0}
.diagnostic-grid{display:grid;grid-template-columns:.92fr 1.08fr;gap:18px;margin:18px 0}.diagnostic-card{background:#fff;border:1px solid #e7eef7;border-radius:20px;padding:20px;box-shadow:0 10px 28px rgba(31,41,55,.07);min-height:250px}.radar-card{display:flex;flex-direction:column;align-items:center}.radar-chart{width:min(100%,310px);height:230px;overflow:visible}.radar-chart line{stroke:#d9e7f5;stroke-width:1}.radar-bg{fill:#f5f9ff;stroke:#d7e5f5;stroke-width:1}.radar-mid{fill:none;stroke:#e4edf8;stroke-width:1}.radar-area{fill:rgba(53,212,255,.20);stroke:#4b6cff;stroke-width:3;filter:drop-shadow(0 8px 12px rgba(75,108,255,.18))}.radar-chart text{font-size:10px;fill:#6b7890}.dim-list{display:grid;gap:12px}.dim-row{display:grid;grid-template-columns:132px 38px 1fr;gap:12px;align-items:center}.dim-row b{display:block;color:#172033}.dim-row span{display:block;font-size:12px;color:#7b8798;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.dim-row em{font-style:normal;font-weight:800;color:#3859ff;text-align:right}.dim-row i{height:9px;border-radius:99px;background:#e9eff8;overflow:hidden}.dim-row u{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#35d4ff,#5b61ff);text-decoration:none}@media(max-width:900px){.diagnostic-grid{grid-template-columns:1fr}}
.report-float-panel{width:318px;max-width:42%;margin:2px 0 14px 22px;position:relative;z-index:1}.report-float-panel.right{float:right}.report-float-panel.left{float:left;margin:2px 22px 14px 0}.report-float-panel .diagnostic-grid{display:block;margin:0}.report-float-panel .diagnostic-card{min-height:0;margin:0 0 12px;padding:14px;border-radius:16px;box-shadow:0 8px 20px rgba(31,41,55,.06)}.report-float-panel .radar-chart{height:172px}.report-float-panel .dim-row{grid-template-columns:72px 1fr 30px;gap:8px;margin:8px 0}.report-float-panel .dim-row span{display:none}.report-float-panel .grid{display:block;margin:0}.report-float-panel .card{padding:14px;border-radius:16px;box-shadow:0 8px 20px rgba(31,41,55,.06);margin-bottom:12px}.report-float-panel table{font-size:12px}.report-float-panel th,.report-float-panel td{padding:7px 6px}.story-section:after{content:"";display:block;clear:both}@media(max-width:900px){.report-float-panel,.report-float-panel.left,.report-float-panel.right{float:none;width:100%;max-width:100%;margin:12px 0}}
@page{size:A4;margin:10mm 9mm}@media print{body{background:#fff!important;color:#172033;font-size:11px;line-height:1.62;-webkit-print-color-adjust:exact;print-color-adjust:exact}.page{width:192mm!important;max-width:none!important;margin:0 auto!important;padding:0!important}.hero{border-radius:14px!important;padding:18px 20px!important;box-shadow:none!important;break-inside:avoid}.hero h1{font-size:22px!important}.hero:after,.jumpbar,.connector-note{display:none!important}.kpis{display:grid!important;grid-template-columns:repeat(4,1fr)!important;gap:8px!important;margin-top:12px!important}.kpi{border-radius:10px!important;padding:8px 10px!important}.kpi b{font-size:18px!important}.grid,.diagnostic-grid{grid-template-columns:1fr 1fr!important;gap:9px!important;margin-top:10px!important}.card,.diagnostic-card,.legacy-section,.story-section,.mini-panel,figure{box-shadow:none!important;border-color:#dfe7f2!important}.diagnostic-card{min-height:0!important;padding:12px!important;border-radius:12px!important}.radar-chart{height:150px!important}.dim-row{grid-template-columns:84px 28px 1fr!important;gap:7px!important;margin:5px 0!important}.dim-row span{display:none!important}.legacy-section,.story-section{break-inside:avoid;page-break-inside:avoid;border-radius:14px!important;padding:15px 17px!important;margin:0 0 10px!important;overflow:visible!important}.legacy-section h2,.story-section h2{font-size:15px!important;margin-bottom:10px!important;border-left-width:3px!important}.legacy-section p,.legacy-section li,.story-section p,.story-section li{font-size:10.2px!important;line-height:1.62!important}.legacy-section ul,.story-section ul{gap:5px!important;margin:7px 0!important}.legacy-section li,.story-section li{padding:7px 9px 7px 22px!important;border-radius:10px!important;break-inside:avoid}.legacy-step-card,.step-card{grid-template-columns:28px 1fr!important;gap:8px!important;margin:7px 0!important;padding:8px 10px!important;border-radius:10px!important;break-inside:avoid}.legacy-step-card b,.step-card b{width:24px!important;height:24px!important;border-radius:8px!important}.report-float-panel,.report-float-panel.left,.report-float-panel.right{float:none!important;width:100%!important;max-width:100%!important;margin:8px 0!important;break-inside:avoid}.mini-radar,.report-float-panel .radar-chart{height:135px!important}.mini-bar-row{grid-template-columns:58px 1fr 24px!important;margin:5px 0!important;gap:6px!important}.mini-bar-row b,.mini-bar-row em{font-size:9px!important}th,td,.legacy-data-table th,.legacy-data-table td,.mini-table-panel th,.mini-table-panel td,.report-float-panel th,.report-float-panel td{padding:5px 6px!important}.legacy-table-wrap,.data-table-wrap{overflow:visible!important;border-radius:10px!important}}
</style>
"""


def _a4_print_patch_css() -> str:
    return """
<style id="livewatch-report-print-a4">
@page{size:A4;margin:10mm 9mm}
@media print{
html{scroll-behavior:auto}
body{background:#fff!important;color:#172033!important;font-size:11px!important;line-height:1.62!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.page{width:192mm!important;max-width:none!important;margin:0 auto!important;padding:0!important}
.hero{border-radius:14px!important;padding:18px 20px!important;box-shadow:none!important;break-inside:avoid;page-break-inside:avoid}
.hero h1{font-size:22px!important}.hero p{font-size:11px!important}.hero:after,.jumpbar,.connector-note{display:none!important}
.kpis{display:grid!important;grid-template-columns:repeat(4,1fr)!important;gap:8px!important;margin-top:12px!important}
.kpi{border-radius:10px!important;padding:8px 10px!important}.kpi b{font-size:18px!important}
.grid,.diagnostic-grid{grid-template-columns:1fr 1fr!important;gap:9px!important;margin-top:10px!important}
.card,.diagnostic-card,.legacy-section,.story-section,.mini-panel,figure{box-shadow:none!important;border-color:#dfe7f2!important}
.diagnostic-card{min-height:0!important;padding:12px!important;border-radius:12px!important}
.radar-chart{height:150px!important}.dim-row{grid-template-columns:84px 28px 1fr!important;gap:7px!important;margin:5px 0!important}.dim-row span{display:none!important}
figure{border-radius:10px!important}figure img,.empty-shot div{height:102px!important}figcaption{font-size:9px!important;padding:5px 7px!important}
.report-shell{margin-top:10px!important}
.legacy-section,.story-section{break-inside:avoid;page-break-inside:avoid;border-radius:14px!important;padding:15px 17px!important;margin:0 0 10px!important;animation:none!important;overflow:visible!important}
.legacy-section h2,.story-section h2{font-size:15px!important;margin-bottom:10px!important;border-left-width:3px!important}
.legacy-section h3,.story-section h3{font-size:12px!important;margin:10px 0 5px!important}
.legacy-section p,.legacy-section li,.story-section p,.story-section li{font-size:10.2px!important;line-height:1.62!important}
.story-section .lead{font-size:11px!important;line-height:1.66!important;padding:9px 10px!important;border-radius:10px!important}
.insight-list,.legacy-section ul,.story-section ul{gap:5px!important;margin:7px 0!important}
.legacy-section li,.insight-list li,.story-section ul li{padding:7px 9px 7px 22px!important;border-radius:10px!important;break-inside:avoid}
.legacy-step-card,.step-card{grid-template-columns:28px 1fr!important;gap:8px!important;margin:7px 0!important;padding:8px 10px!important;border-radius:10px!important;break-inside:avoid}
.legacy-step-card b,.step-card b{width:24px!important;height:24px!important;border-radius:8px!important}
.report-float-panel,.report-float-panel.left,.report-float-panel.right{float:none!important;width:100%!important;max-width:100%!important;margin:8px 0!important;break-inside:avoid}
.report-float-panel .mini-panel,.report-float-panel .diagnostic-card{padding:10px!important;border-radius:12px!important;margin-bottom:7px!important}
.mini-radar,.report-float-panel .radar-chart{height:135px!important}
.mini-bar-row{grid-template-columns:58px 1fr 24px!important;margin:5px 0!important;gap:6px!important}.mini-bar-row b,.mini-bar-row em{font-size:9px!important}
.mini-table-panel table,.report-float-panel table{font-size:9px!important}
th,td,.legacy-data-table th,.legacy-data-table td,.mini-table-panel th,.mini-table-panel td,.report-float-panel th,.report-float-panel td{padding:5px 6px!important}
.legacy-table-wrap,.data-table-wrap{overflow:visible!important;border-radius:10px!important}
.quote-card{margin:8px 0!important;padding:8px 10px!important;border-radius:0 9px 9px 0!important}
}
</style>
"""


def _inline_layout_fix_css() -> str:
    return """
<style id="livewatch-inline-report-fix">
.report-shell{display:block!important;margin-top:18px!important}
.visual-rail{display:none!important}
.report{width:100%!important;max-width:none!important}
.legacy-inline-panel .diagnostic-grid{display:block!important;margin:0!important}
.legacy-inline-panel .diagnostic-card{min-height:0!important;margin:0!important;padding:14px!important;border-radius:16px!important;box-shadow:0 8px 20px rgba(31,41,55,.06)!important}
.legacy-inline-panel .diagnostic-card+ .diagnostic-card{display:none!important}
.legacy-inline-panel .diagnostic-card h2{font-size:14px!important;margin-bottom:8px!important}
.legacy-inline-panel .radar-chart{height:172px!important;width:100%!important}
.legacy-inline-panel .grid{display:block!important;margin:0!important}
.legacy-inline-panel .card{padding:14px!important;border-radius:16px!important;box-shadow:0 8px 20px rgba(31,41,55,.06)!important;margin:0!important}
.legacy-inline-panel .card+ .card{display:none!important}
.legacy-inline-panel table{font-size:12px!important}
.legacy-inline-panel th,.legacy-inline-panel td{padding:7px 6px!important}
.report-float-panel{width:318px!important;max-width:42%!important;margin:2px 0 14px 22px!important;position:relative!important;z-index:1!important}
.report-float-panel.right{float:right!important}
.report-float-panel.left{float:left!important;margin:2px 22px 14px 0!important}
.story-section:after{content:"";display:block;clear:both}
@media(max-width:900px){.report-float-panel,.report-float-panel.left,.report-float-panel.right{float:none!important;width:100%!important;max-width:100%!important;margin:12px 0!important}}
</style>
"""


def _polish_report_html_text(raw_html: str) -> str:
    document = raw_html
    for raw, label in _TECH_LABELS.items():
        document = re.sub(rf"`?\b{re.escape(raw)}\b`?", label, document, flags=re.I)
    document = re.sub(r"`([^`]{1,60})`", r"\1", document)
    document = document.replace("&ast;&ast;", "").replace("&#42;&#42;", "").replace("&#x2a;&#x2a;", "")
    document = document.replace("**", "")
    document = re.sub(r"\*\*([^*<>]{1,120})\*\*", r"\1", document)
    document = re.sub(r"(<li[^>]*>)\s*(?:[-*•·]|&ast;)\s*", r"\1", document, flags=re.I)
    document = re.sub(r"(<p[^>]*>)\s*(?:[-*•·]|&ast;)\s*", r"\1", document, flags=re.I)
    document = re.sub(r"(<span[^>]*>)\s*(?:[-*•·]|&ast;)\s*", r"\1", document, flags=re.I)
    document = re.sub(r"(证据显示[:：]?\s*)", "依据本场数据：", document)
    document = re.sub(r"数据缺失或未记录", "暂无完整记录", document)
    document = re.sub(r"证据不足以判断", "当前样本不足，建议结合录屏回看", document)
    return document


def _insert_diagnostic_block(raw_html: str) -> str:
    if "report-float-panel" in raw_html or "mini-panel" in raw_html:
        return raw_html
    if '<section class="diagnostic-grid"' in raw_html:
        return raw_html
    metrics = _metrics_from_report_html(raw_html)
    visual = _dimension_visual_html(_dimension_scores(metrics))
    if not visual:
        return raw_html
    if "</section>\n<section class=\"grid\"" in raw_html:
        return raw_html.replace("</section>\n<section class=\"grid\"", f"</section>\n{visual}\n<section class=\"grid\"", 1)
    if '<section class="card report' in raw_html:
        return raw_html.replace('<section class="card report', visual + '\n<section class="card report', 1)
    if '<section class="report-shell"' in raw_html:
        return raw_html.replace('<section class="report-shell"', visual + '\n<section class="report-shell"', 1)
    return raw_html


def _interleave_existing_report_blocks(raw_html: str) -> str:
    if 'data-inline-report-layout="1"' in raw_html:
        return raw_html
    if "report-float-panel" in raw_html or "mini-panel" in raw_html:
        return raw_html
    document = raw_html
    floating_blocks: list[str] = []
    for pattern in (
        r'\s*<section class="diagnostic-grid">.*?</section>\s*',
        r'\s*<section class="grid" id="data">.*?</section>\s*',
    ):
        match = re.search(pattern, document, flags=re.S)
        if match:
            floating_blocks.append(match.group(0).strip())
            document = document[: match.start()] + document[match.end():]
    if not floating_blocks:
        return document.replace("<body", '<body data-inline-report-layout="1"', 1)

    article_matches = list(re.finditer(r'<article class="story-section[^"]*">', document))
    if not article_matches:
        return document
    for idx, block in enumerate(floating_blocks):
        target_idx = min(idx, len(article_matches) - 1)
        article = article_matches[target_idx]
        start = article.end()
        h2 = re.search(r"</h2>", document[start:], flags=re.S)
        insert_at = start + h2.end() if h2 else start
        side = "right" if idx % 2 == 0 else "left"
        panel = f'<aside class="report-float-panel {side} legacy-inline-panel">{block}</aside>'
        document = document[:insert_at] + panel + document[insert_at:]
        article_matches = list(re.finditer(r'<article class="story-section[^"]*">', document))
    return document.replace("<body", '<body data-inline-report-layout="1"', 1)


def upgrade_legacy_report_html(raw_html: str) -> str:
    """Upgrade old generated report files to the current card-based reading layout."""
    upgraded = raw_html
    upgraded = _upgrade_legacy_tables(upgraded)
    upgraded = _upgrade_legacy_steps(upgraded)
    upgraded = _wrap_legacy_report_sections(upgraded)
    upgraded = _polish_report_html_text(upgraded)
    upgraded = _insert_diagnostic_block(upgraded)
    upgraded = _interleave_existing_report_blocks(upgraded)
    upgraded = re.sub(r"<li>\s*[-*]\s*", "<li>", upgraded)
    upgraded = re.sub(r"\.section-3\s+\.step-card:nth-of-type\(odd\)\s*\{[^}]*\}", "", upgraded)
    upgraded = re.sub(r"\.step-card\{([^}]*)margin:10px 0;([^}]*)\}", r".step-card{\1margin:12px 0;\2}", upgraded)
    if "livewatch-report-upgrade" not in upgraded:
        css = _legacy_report_upgrade_css()
        if "</head>" in upgraded:
            upgraded = upgraded.replace("</head>", css + "</head>", 1)
        else:
            upgraded = css + upgraded
    if "livewatch-inline-report-fix" not in upgraded:
        css = _inline_layout_fix_css()
        if "</head>" in upgraded:
            upgraded = upgraded.replace("</head>", css + "</head>", 1)
        else:
            upgraded = css + upgraded
    if "livewatch-report-print-a4" not in upgraded:
        css = _a4_print_patch_css()
        if "</head>" in upgraded:
            upgraded = upgraded.replace("</head>", css + "</head>", 1)
        else:
            upgraded = css + upgraded
    if _REPORT_V2_MARKER not in upgraded:
        upgraded = upgraded.replace('<html lang="zh-CN"', '<html lang="zh-CN" data-report-version="2"', 1)
    return upgraded


def report_view_html(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    upgraded = upgrade_legacy_report_html(raw)
    if upgraded != raw:
        try:
            path.write_text(upgraded, encoding="utf-8")
        except OSError:
            pass
    return upgraded


def _html_report_to_pdf(html_path: Path, pdf_path: Path) -> None:
    """Print the polished HTML report to PDF so export matches the preview."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        raise AIReportError("缺少 HTML 转 PDF 依赖 playwright。") from exc

    html_path = html_path.resolve()
    pdf_path = pdf_path.resolve()
    if not html_path.is_file():
        raise AIReportError("缺少 HTML 报告，无法按预览样式导出 PDF。")
    report_view_html(html_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 1600}, device_scale_factor=1)
            page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30_000)
            page.emulate_media(media="print")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                prefer_css_page_size=True,
            )
            browser.close()
    except Exception as exc:
        raise AIReportError(f"HTML 转 PDF 失败：{exc}") from exc


def upgrade_report_file(path: Path) -> bool:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    upgraded = upgrade_legacy_report_html(raw)
    if upgraded == raw:
        return False
    path.write_text(upgraded, encoding="utf-8")
    return True


def _markdown_to_pdf(
    markdown: str,
    path: Path,
    *,
    overviews: list[dict[str, object]] | None = None,
    words: list[dict[str, object]] | None = None,
    visual_assets: list[dict[str, object]] | None = None,
) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.graphics.shapes import Drawing, Rect, String
    except Exception as exc:  # pragma: no cover - dependency is environment-specific
        raise AIReportError("缺少 PDF 生成依赖 reportlab，请安装后重新生成报告。") from exc

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    font = "STSong-Light"
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "LiveWatchBase",
        parent=styles["Normal"],
        fontName=font,
        fontSize=10.5,
        leading=17,
        textColor=colors.HexColor("#1f2937"),
        alignment=TA_LEFT,
        spaceAfter=5,
    )
    h1 = ParagraphStyle(
        "LiveWatchH1",
        parent=base,
        fontSize=18,
        leading=24,
        textColor=colors.HexColor("#111827"),
        spaceBefore=4,
        spaceAfter=12,
    )
    h2 = ParagraphStyle(
        "LiveWatchH2",
        parent=base,
        fontSize=14,
        leading=20,
        textColor=colors.HexColor("#111827"),
        spaceBefore=12,
        spaceAfter=7,
    )
    h3 = ParagraphStyle(
        "LiveWatchH3",
        parent=base,
        fontSize=12,
        leading=18,
        textColor=colors.HexColor("#374151"),
        spaceBefore=9,
        spaceAfter=5,
    )
    bullet = ParagraphStyle(
        "LiveWatchBullet",
        parent=base,
        leftIndent=12,
        firstLineIndent=-8,
    )
    quote = ParagraphStyle(
        "LiveWatchQuote",
        parent=base,
        leftIndent=8,
        textColor=colors.HexColor("#475569"),
        backColor=colors.HexColor("#f7faff"),
        borderColor=colors.HexColor("#d8e3f5"),
        borderWidth=0.5,
        borderPadding=5,
    )
    tiny = ParagraphStyle(
        "LiveWatchTiny",
        parent=base,
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#667085"),
    )
    white = ParagraphStyle(
        "LiveWatchWhite",
        parent=base,
        fontSize=10,
        leading=14,
        textColor=colors.white,
    )
    overviews = overviews or []
    words = words or []
    visual_assets = visual_assets or []
    metrics = _overview_metrics(overviews)

    def inline_md(text: str) -> str:
        escaped = html.escape(text.strip())
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        return escaped

    def metric_card(title: str, value: object, hint: str = "") -> Table:
        card = Table(
            [[Paragraph(f"<b>{html.escape(str(value))}</b>", ParagraphStyle("MetricValue", parent=base, fontSize=20, leading=24, textColor=colors.HexColor("#111827")))],
             [Paragraph(html.escape(title), tiny)],
             [Paragraph(html.escape(hint), tiny) if hint else ""]],
            colWidths=[38 * mm],
            rowHeights=[10 * mm, 6 * mm, 7 * mm],
        )
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fbff")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#dfe8f6")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        return card

    def bar_drawing(title: str, items: list[tuple[str, int]], *, width: int = 470, height: int = 150) -> Drawing:
        d = Drawing(width, height)
        d.add(String(0, height - 12, title, fontName=font, fontSize=10, fillColor=colors.HexColor("#111827")))
        max_value = max((v for _, v in items), default=1) or 1
        y = height - 34
        for label, value in items[:6]:
            bar_w = int((width - 130) * (value / max_value))
            d.add(String(0, y + 3, _short_text(label, 13), fontName=font, fontSize=8, fillColor=colors.HexColor("#475467")))
            d.add(Rect(88, y, width - 140, 8, fillColor=colors.HexColor("#edf2ff"), strokeColor=None, rx=4, ry=4))
            d.add(Rect(88, y, max(4, bar_w), 8, fillColor=colors.HexColor("#4755ff"), strokeColor=None, rx=4, ry=4))
            d.add(String(width - 42, y + 1, str(value), fontName=font, fontSize=8, fillColor=colors.HexColor("#667085")))
            y -= 20
        return d

    def visual_cell(img_path: Path | None, nickname: object) -> Table:
        caption = Paragraph(html.escape(_short_text(nickname, 16)), tiny)
        if img_path and img_path.exists():
            try:
                image: object = Image(str(img_path), width=37 * mm, height=26 * mm)
            except Exception:
                image = Paragraph("暂无画面", tiny)
        else:
            image = Paragraph("暂无画面", tiny)
        cell = Table(
            [[image], [caption]],
            colWidths=[39 * mm],
            rowHeights=[28 * mm, 8 * mm],
        )
        cell.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ffffff")),
            ("BOX", (0, 0), (-1, -1), 0.3, colors.HexColor("#e8eef7")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return cell

    story: list[object] = []
    cover = Table(
        [[Paragraph("<b>直播复盘侠 AI 复盘报告</b>", ParagraphStyle("CoverTitle", parent=white, fontSize=22, leading=28))],
         [Paragraph("话术、互动、热度与视觉证据的综合复盘", white)],
         [Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M"), ParagraphStyle("CoverTime", parent=white, fontSize=9, leading=13, textColor=colors.HexColor("#d9e6ff")))]],
        colWidths=[174 * mm],
        rowHeights=[17 * mm, 10 * mm, 8 * mm],
    )
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#101828")),
        ("BOX", (0, 0), (-1, -1), 0, colors.HexColor("#101828")),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([cover, Spacer(1, 8)])
    story.append(Table([[
        metric_card("复盘主播", metrics["rooms"], "本次选择"),
        metric_card("话术片段", metrics["transcripts"], f"{metrics['chars']} 字"),
        metric_card("弹幕/评论", metrics["chats"], "互动证据"),
        metric_card("峰值在线", metrics["peak"], f"累计观看 {metrics['pv']}"),
    ]], colWidths=[43 * mm] * 4))
    story.append(Spacer(1, 10))
    chart_items = [(_short_text(o.get("nickname"), 14), int(o.get("online_peak") or 0)) for o in overviews]
    if chart_items:
        story.append(bar_drawing("主播峰值在线对比", chart_items))
        story.append(Spacer(1, 6))
    top_words = [(str(w.get("word") or ""), int(w.get("count") or 0)) for w in words[:8]]
    if top_words:
        story.append(bar_drawing("高频词热度排行", top_words))
        story.append(Spacer(1, 8))

    image_cells: list[object] = []
    for asset in visual_assets[:4]:
        img_path: Path | None = None
        frames = asset.get("frames")
        if isinstance(frames, list) and frames:
            img_path = frames[0]
        elif isinstance(asset.get("avatar"), Path):
            img_path = asset.get("avatar")  # type: ignore[assignment]
        image_cells.append(visual_cell(img_path, asset.get("nickname")))
    if image_cells:
        while len(image_cells) % 4:
            image_cells.append("")
        story.append(Paragraph("直播视觉素材", h2))
        for i in range(0, len(image_cells), 4):
            t = Table([image_cells[i:i + 4]], colWidths=[43 * mm] * 4)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fbff")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e8eef7")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(t)
        story.append(Spacer(1, 8))

    for raw in (markdown or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 3))
            continue
        if line == "---":
            story.append(Spacer(1, 8))
            continue
        if line.startswith("# "):
            if story:
                story.append(Spacer(1, 4))
            story.append(Paragraph(inline_md(line[2:]), h1))
        elif line.startswith("## "):
            story.append(Paragraph(inline_md(line[3:]), h2))
        elif line.startswith("### "):
            story.append(Paragraph(inline_md(line[4:]), h3))
        elif line.startswith(">"):
            story.append(Paragraph(inline_md(line.lstrip("> ")), quote))
        elif re.match(r"^[-*]\s+", line):
            story.append(Paragraph("• " + inline_md(re.sub(r"^[-*]\s+", "", line)), bullet))
        else:
            story.append(Paragraph(inline_md(line), base))

    if not story:
        story.append(Paragraph("报告为空。", base))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=path.stem,
    )
    doc.build(story)


def generate_report(rids: list[str]) -> dict[str, object]:
    bundles = _load_bundles(rids)
    cfg = load_config()
    chunks, truncated = _build_chunks(bundles)
    if not chunks:
        raise AIReportError("没有可分析的话术文本。")

    overviews = [_bundle_overview(b) for b in bundles]
    words_payload = word_cloud([b.rid for b in bundles], limit=60)
    summaries: list[dict[str, object]] = []
    for chunk in chunks:
        if cfg.ready:
            summaries.append(_summarize_chunk(cfg, chunk))
        else:
            summaries.append(_local_fallback_summary(chunk))

    if cfg.ready:
        try:
            report = _final_report(cfg, overviews, summaries, truncated)
        except AIReportError:
            report = _fallback_markdown(overviews, summaries, truncated)
    else:
        report = _fallback_markdown(overviews, summaries, truncated)

    title = "、".join((b.nickname or b.rid) for b in bundles[:3])
    if len(bundles) > 3:
        title += f"等{len(bundles)}个主播"
    filename = _safe_report_name(f"AI复盘_{title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}") + ".md"
    config.AI_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.AI_REPORT_DIR / filename
    path.write_text(report, encoding="utf-8")
    # Do not spend an extra model call on the short brief. The full report has
    # already paid for AI reasoning; the UI only needs a compact reading entry.
    brief = _local_brief(report)
    pdf_filename = filename[:-3] + ".pdf"
    html_filename = filename[:-3] + ".html"
    pdf_path = config.AI_REPORT_DIR / pdf_filename
    html_path = config.AI_REPORT_DIR / html_filename
    assets = _visual_assets(bundles, config.AI_REPORT_DIR)
    _write_html_report(
        report,
        html_path,
        overviews=overviews,
        words=words_payload["words"],  # type: ignore[arg-type]
        visual_assets=assets,
    )
    _markdown_to_pdf(
        report,
        pdf_path,
        overviews=overviews,
        words=words_payload["words"],  # type: ignore[arg-type]
        visual_assets=assets,
    )
    return {
        "ok": True,
        "path": str(path),
        "pdf_path": str(pdf_path),
        "html_path": str(html_path),
        "dir": str(path.parent),
        "filename": filename,
        "pdf_filename": pdf_filename,
        "html_filename": html_filename,
        "rooms": len(bundles),
        "chunks": len(chunks),
        "truncated": truncated,
        "used_ai": cfg.ready,
        "brief": brief,
        "preview": report[:4000],
    }


def ensure_pdf_report(filename: str) -> Path:
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".pdf"):
        raise AIReportError("只能生成 PDF 报告。")
    root = config.AI_REPORT_DIR.resolve()
    pdf_path = (config.AI_REPORT_DIR / safe_name).resolve()
    if root not in pdf_path.parents and pdf_path != root:
        raise AIReportError("无效文件名。")
    html_path = pdf_path.with_suffix(".html")
    if html_path.is_file():
        report_view_html(html_path)
        if pdf_path.is_file() and pdf_path.stat().st_size > 0 and pdf_path.stat().st_mtime >= html_path.stat().st_mtime:
            return pdf_path
        try:
            _html_report_to_pdf(html_path, pdf_path)
            return pdf_path
        except AIReportError:
            if pdf_path.exists():
                return pdf_path
    md_path = pdf_path.with_suffix(".md")
    if not md_path.is_file():
        raise AIReportError("缺少报告原文，无法生成 PDF。")
    report = md_path.read_text(encoding="utf-8")
    report_rids = [str(x) for x in _report_room_ids_from_markdown(report)]
    words = word_cloud(report_rids, limit=60)["words"] if report_rids else []
    html_path = pdf_path.with_suffix(".html")
    if not html_path.exists():
        _write_html_report(report, html_path, overviews=[], words=words, visual_assets=[])  # type: ignore[arg-type]
    report_view_html(html_path)
    if pdf_path.is_file() and pdf_path.stat().st_size > 0 and pdf_path.stat().st_mtime >= html_path.stat().st_mtime:
        return pdf_path
    try:
        _html_report_to_pdf(html_path, pdf_path)
        return pdf_path
    except AIReportError:
        pass
    _markdown_to_pdf(
        report,
        pdf_path,
        overviews=[],
        words=words,  # type: ignore[arg-type]
        visual_assets=[],
    )
    return pdf_path


def _report_room_ids_from_markdown(report: str) -> list[str]:
    return sorted(set(re.findall(r"\b\d{6,15}\b", report or "")))


def _estimate_report_seconds(chunks: int, *, ai_ready: bool) -> int:
    if chunks <= 0:
        return 8
    if ai_ready:
        batches = max(1, (chunks + AI_CHUNK_WORKERS - 1) // AI_CHUNK_WORKERS)
        return max(25, min(180, 24 + batches * 18))
    return max(8, min(45, 6 + chunks * 3))


def _streaming_report_preview(
    overviews: list[dict[str, object]],
    summaries: list[dict[str, object]],
    *,
    total_chunks: int,
    stage: str,
) -> str:
    metrics = _overview_metrics(overviews)
    room_names = "、".join(_short_text(o.get("nickname"), 12) for o in overviews[:4]) or "所选主播"
    if len(overviews) > 4:
        room_names += f"等{len(overviews)}个主播"
    bullets: list[str] = []
    for item in summaries[-3:]:
        summary = str(item.get("summary") or "").strip()
        if summary:
            bullets.append(summary[:90])
        for ev in item.get("evidence", []) if isinstance(item.get("evidence"), list) else []:
            if isinstance(ev, dict) and ev.get("quote"):
                bullets.append("证据：" + str(ev.get("quote"))[:80])
                break
    if not bullets:
        bullets = [
            "正在读取话术、弹幕和直播数据。",
            "正在搭建报告结构，完整结论会在分析完成后自动替换。",
        ]
    done = len(summaries)
    return (
        "# AI 复盘报告正在生成\n\n"
        f"## 一、生成状态\n"
        f"- 当前阶段：{stage}\n"
        f"- 分析进度：{done}/{total_chunks} 个文本块\n"
        f"- 复盘对象：{room_names}\n\n"
        "## 二、直播数据看板\n"
        f"- 主播数量：{metrics['rooms']}\n"
        f"- 话术片段：{metrics['transcripts']}\n"
        f"- 弹幕/评论：{metrics['chats']}\n"
        f"- 峰值在线：{metrics['peak']}\n"
        f"- 累计观看：{metrics['pv']}\n\n"
        "## 三、已抽取的阶段性发现\n"
        + "\n".join(f"- {b}" for b in bullets[:5])
        + "\n\n## 四、接下来\n"
        "- 继续抽取观众问题、话术资产、低效片段和风险复核。\n"
        "- 最终报告会先生成可查看版本，PDF 可在需要导出时再生成。"
    )


def _streaming_brief_preview(preview: str) -> str:
    """Create a compact UI brief from an in-progress Markdown preview."""
    return _local_brief(preview, 210)


def generate_report_events(rids: list[str]) -> Iterator[dict[str, object]]:
    started = time.time()
    yield {"type": "start", "message": "AI复盘开始：正在整理本地直播数据", "progress": 3}
    bundles = _load_bundles(rids)
    overviews = [_bundle_overview(b) for b in bundles]
    words_payload = word_cloud([b.rid for b in bundles], limit=60)
    yield {
        "type": "evidence",
        "message": f"直播资料已读取：{len(bundles)} 个主播、{sum(len(b.transcripts) for b in bundles)} 段话术，开始梳理直播过程",
        "progress": 10,
        "rooms": overviews,
        "words": words_payload["words"],
    }

    cfg = load_config()
    chunks, truncated = _build_chunks(bundles)
    if not chunks:
        raise AIReportError("没有可分析的话术文本。")
    summaries: list[dict[str, object]] = []
    total = len(chunks)
    estimate_sec = _estimate_report_seconds(total, ai_ready=cfg.ready)
    partial_preview = _streaming_report_preview(
        overviews,
        summaries,
        total_chunks=total,
        stage="搭建报告框架",
    )
    yield {
        "type": "plan",
        "message": f"已拆分为 {total} 段直播内容，预计约 {estimate_sec} 秒；将边分析边生成报告框架",
        "progress": 12,
        "estimate_sec": estimate_sec,
        "partial_preview": partial_preview,
        "brief_preview": _streaming_brief_preview(partial_preview),
    }
    if cfg.ready and total > 1:
        summary_slots: list[dict[str, object] | None] = [None] * total
        workers = min(AI_CHUNK_WORKERS, total)
        yield {
            "type": "parallel_start",
            "message": f"内容分析开始：{workers} 个 AI 任务同时处理 {total} 段话术",
            "progress": 15,
            "chunks": total,
        }
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai-report-chunk") as pool:
            future_map = {
                pool.submit(_summarize_chunk, cfg, chunk): (idx, chunk)
                for idx, chunk in enumerate(chunks, start=1)
            }
            for future in as_completed(future_map):
                idx, chunk = future_map[future]
                try:
                    summary = future.result()
                except Exception as exc:  # Defensive: one bad chunk must not kill the whole report.
                    summary = _local_fallback_summary(chunk, f"第 {idx}/{total} 段话术处理异常，已保留原始内容继续分析：{exc}")
                summary_slots[idx - 1] = summary
                summaries = [item for item in summary_slots if item is not None]
                done_count = len(summaries)
                if summary.get("_fallback"):
                    yield {
                        "type": "warning",
                        "message": f"第 {idx}/{total} 段话术格式不稳定，已保留原始内容继续分析。",
                        "progress": 13 + int(done_count / max(total, 1) * 58),
                        "chunk": idx,
                        "summary": summary.get("summary", ""),
                    }
                partial_preview = _streaming_report_preview(
                    overviews,
                    summaries,
                    total_chunks=total,
                    stage=f"已完成 {done_count}/{total} 段内容",
                )
                yield {
                    "type": "chunk_done",
                    "message": f"第 {idx}/{total} 段已分析：已整理主题、观众问题和可引用原话",
                    "progress": 14 + int(done_count / max(total, 1) * 58),
                    "chunk": idx,
                    "summary": summary.get("summary", ""),
                    "partial_preview": partial_preview,
                    "brief_preview": _streaming_brief_preview(partial_preview),
                }
        summaries = [item or _local_fallback_summary(chunks[idx], "该段话术未返回结果，已保留原始内容继续分析。") for idx, item in enumerate(summary_slots)]
    else:
        for idx, chunk in enumerate(chunks, start=1):
            yield {
                "type": "chunk_start",
                "message": f"正在分析第 {idx}/{total} 段直播内容，提炼卖点、问题、敏感表达和优化点",
                "progress": 10 + int(idx / max(total, 1) * 58),
                "chunk": idx,
                "chunks": total,
            }
            if cfg.ready:
                summary = _summarize_chunk(cfg, chunk)
            else:
                summary = _local_fallback_summary(chunk)
            summaries.append(summary)
            if cfg.ready and summary.get("_fallback"):
                yield {
                    "type": "warning",
                    "message": f"第 {idx}/{total} 段话术格式不稳定，已保留原始内容继续分析。",
                    "progress": 13 + int(idx / max(total, 1) * 58),
                    "chunk": idx,
                    "summary": summary.get("summary", ""),
                }
            partial_preview = _streaming_report_preview(
                overviews,
                summaries,
                total_chunks=total,
                stage=f"已完成第 {idx}/{total} 段内容",
            )
            yield {
                "type": "chunk_done",
                "message": f"第 {idx}/{total} 段已分析：已整理主题、观众问题和可引用原话",
                "progress": 14 + int(idx / max(total, 1) * 58),
                "chunk": idx,
                "summary": summary.get("summary", ""),
                "partial_preview": partial_preview,
                "brief_preview": _streaming_brief_preview(partial_preview),
            }

    yield {"type": "final_start", "message": "正在汇总全场表现并生成复盘报告", "progress": 78}
    if cfg.ready:
        try:
            report = _final_report(cfg, overviews, summaries, truncated)
            used_ai = True
        except AIReportError as exc:
            yield {"type": "warning", "message": f"AI 报告生成失败，改用本地资料草稿：{exc}", "progress": 82}
            report = _fallback_markdown(overviews, summaries, truncated)
            used_ai = False
    else:
        report = _fallback_markdown(overviews, summaries, truncated)
        used_ai = False

    brief = _local_brief(report)
    yield {"type": "brief_ready", "message": "复盘简报已生成，正在整理网页版报告", "progress": 90, "brief": brief}
    yield {"type": "render_start", "message": "正在整理网页版报告，PDF 可在需要时导出", "progress": 95}
    title = "、".join((b.nickname or b.rid) for b in bundles[:3])
    if len(bundles) > 3:
        title += f"等{len(bundles)}个主播"
    filename = _safe_report_name(f"AI复盘_{title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}") + ".md"
    config.AI_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.AI_REPORT_DIR / filename
    path.write_text(report, encoding="utf-8")
    pdf_filename = filename[:-3] + ".pdf"
    html_filename = filename[:-3] + ".html"
    pdf_path = config.AI_REPORT_DIR / pdf_filename
    html_path = config.AI_REPORT_DIR / html_filename
    _write_html_report(
        report,
        html_path,
        overviews=overviews,
        words=words_payload["words"],  # type: ignore[arg-type]
        visual_assets=[],
    )
    elapsed = round(time.time() - started, 1)
    yield {
        "type": "done",
        "message": f"复盘完成，用时 {elapsed} 秒；可查看报告已生成，PDF 可按需导出",
        "progress": 100,
        "ok": True,
        "path": str(path),
        "pdf_path": str(pdf_path),
        "html_path": str(html_path),
        "dir": str(path.parent),
        "filename": filename,
        "pdf_filename": pdf_filename,
        "html_filename": html_filename,
        "pdf_status": "deferred",
        "rooms": len(bundles),
        "chunks": len(chunks),
        "truncated": truncated,
        "used_ai": used_ai,
        "brief": brief,
        "preview": report[:8000],
    }


def _safe_question_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    safe_messages: list[dict[str, str]] = []
    for msg in messages[-10:]:
        role = msg.get("role") if isinstance(msg, dict) else ""
        content = str(msg.get("content") or "")[:2000] if isinstance(msg, dict) else ""
        if isinstance(msg, dict) and msg.get("thinking"):
            continue
        if content.strip() in {"AI 正在思考", "AI正在思考"}:
            continue
        if role in {"user", "assistant"} and content.strip():
            safe_messages.append({"role": role, "content": content.strip()})
    if not safe_messages or safe_messages[-1]["role"] != "user":
        raise AIReportError("请输入要追问的问题。")
    return safe_messages


def _question_prompt(rids: list[str], messages: list[dict[str, str]]) -> tuple[AIConfig, list[dict[str, str]], int, int]:
    bundles = _load_bundles(rids)
    cfg = load_config()
    if not cfg.ready:
        raise AIReportError("AI 尚未配置，无法追问。")
    safe_messages = _safe_question_messages(messages)
    evidence = _all_text(bundles, limit=45_000)
    overview = json.dumps([_bundle_overview(b) for b in bundles], ensure_ascii=False)
    word_data = json.dumps(word_cloud([b.rid for b in bundles], limit=40)["words"], ensure_ascii=False)
    system = (
        "你是直播复盘侠的直播复盘智能体。你只能根据提供的本地直播证据回答，"
        "如果证据不足就明确说证据不足。回答要给出可追溯的时间点或原话。"
    )
    context = (
        f"主播概览：{overview}\n\n"
        f"高频词：{word_data}\n\n"
        f"直播转写证据（可能截断）：\n{evidence}"
    )
    prompt = [{"role": "system", "content": system}, {"role": "user", "content": context}] + safe_messages
    return cfg, prompt, len(bundles), len(evidence)


def answer_question(rids: list[str], messages: list[dict[str, str]]) -> dict[str, object]:
    cfg, prompt, _bundle_count, _evidence_chars = _question_prompt(rids, messages)
    content = _chat_completion(
        cfg,
        prompt,
        temperature=0.25,
        max_tokens=2600,
    )
    return {"ok": True, "answer": content}


def _chunk_text(text: str, size: int = 16) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i:i + size]


def answer_question_events(rids: list[str], messages: list[dict[str, str]]) -> Iterator[dict[str, object]]:
    yield {"type": "stage", "message": "正在读取已选主播的本地证据"}
    cfg, prompt, bundle_count, evidence_chars = _question_prompt(rids, messages)
    time.sleep(0.12)
    yield {
        "type": "stage",
        "message": f"已整理 {bundle_count} 个主播、约 {evidence_chars} 字话术证据",
    }
    time.sleep(0.12)
    yield {"type": "stage", "message": "字段已载入：主播概览、话术片段、互动反馈"}
    time.sleep(0.12)
    yield {"type": "stage", "message": "字段已载入：高频词、时间线、可引用原话"}
    time.sleep(0.12)
    yield {"type": "stage", "message": "正在匹配问题、弹幕反馈、高频词和可引用原话"}
    time.sleep(0.12)
    yield {"type": "stage", "message": "正在组织回答结构，准备输出结论"}
    time.sleep(0.12)
    yield {"type": "stage", "message": "开始生成回答，内容会逐段显示"}

    wrote = False
    try:
        for delta in _chat_completion_stream(cfg, prompt, temperature=0.25, max_tokens=2600):
            wrote = True
            yield {"type": "delta", "content": delta}
    except AIReportError as exc:
        yield {"type": "stage", "message": f"流式输出不可用，改用普通回答：{exc}"}
        answer = _chat_completion(cfg, prompt, temperature=0.25, max_tokens=2600)
        for delta in _chunk_text(answer):
            wrote = True
            yield {"type": "delta", "content": delta}

    if not wrote:
        yield {"type": "delta", "content": "当前问题没有生成有效回答，请换一个更具体的问题重试。"}
    yield {"type": "done", "message": "回答完成"}
