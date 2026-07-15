"""管线统一配置与路径。

目录约定（都在 worker 路线目录下，便于 .gitignore 整目录忽略真实数据）：
  audio/<房间号>/  每个直播间一个文件夹，录音按录制顺序命名 1.mp3/2.mp3…，
                   转写后保留原地不删，方便人工回听、用导出文字反查录音
  audio/failed/    转写失败隔离（避免每轮反复重试卡队列）
  transcripts.db   转写结果库
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import license_runtime

# 录音文件名 seqNNNNN.mp3 的序号解析（segment muxer 用 -segment_start_number 连续编号）
_SEQ_RE = re.compile(r"(?:seq)?0*(\d+)$", re.I)

# douyin_worker_route/（程序代码目录）
ROUTE_DIR = Path(__file__).resolve().parent.parent

# ---------- 程序文件 / 用户数据 / 只读资源 三分离（打包用，开发态行为不变） ----------
# 打包后由启动器注入两个环境变量，把「会被写入的用户数据」与「只读模型资源」从安装目录里分出去：
#   LIVEWATCH_DATA_DIR     → %LOCALAPPDATA%\LiveWatch\data   （cookie、rooms.json、库、audio、exports、日志）
#   LIVEWATCH_RESOURCE_DIR → <安装目录>\models                （SenseVoice / 3D-Speaker 模型）
# 两个变量都未设置时（开发态），全部回退到原来的相对路径，行为与改动前完全一致。
_LEAD_SHRIMP_STANDALONE = os.environ.get("LEADSHRIMP_STANDALONE", "").strip().lower() in {"1", "true", "yes", "on"}
_DATA_ENV = (
    os.environ.get("LEADSHRIMP_DATA_DIR")
    if _LEAD_SHRIMP_STANDALONE
    else os.environ.get("LIVEWATCH_DATA_DIR")
)
_RES_ENV = os.environ.get("LIVEWATCH_RESOURCE_DIR")

# 用户数据根
DATA_DIR = Path(_DATA_ENV).expanduser().resolve() if _DATA_ENV else ROUTE_DIR

# SenseVoice ONNX 模型（开发态复用 asr_bench 下载；打包态用安装目录 models/sensevoice_onnx）
if _RES_ENV:
    RESOURCE_DIR = Path(_RES_ENV).expanduser().resolve()
    MODEL_DIR = RESOURCE_DIR / "sensevoice_onnx"
    SPEAKER_MODEL = RESOURCE_DIR / "speaker" / "3dspeaker_eres2net_zh_16k.onnx"
else:
    RESOURCE_DIR = ROUTE_DIR.parent
    MODEL_DIR = ROUTE_DIR.parent / "asr_bench" / "sensevoice_onnx"
    SPEAKER_MODEL = ROUTE_DIR.parent / "speaker_change_analysis" / "models" / "3dspeaker_eres2net_zh_16k.onnx"
MODEL_ONNX = MODEL_DIR / "model.int8.onnx"
MODEL_TOKENS = MODEL_DIR / "tokens.txt"

# 音频与库（用户数据，落 DATA_DIR）
AUDIO_DIR = DATA_DIR / "audio"
FAILED_DIR = AUDIO_DIR / "failed"
DB_PATH = DATA_DIR / "transcripts.db"

# 视频录制（可选，按房间开关）。与音频同一条 ffmpeg 双输出：音频照旧转写，视频 -c copy 落盘。
# 每 SEGMENT_SEC 一段标准 mp4（正常封口写 moov，全播放器有声），序号与音频段对齐。
VIDEO_DIR = DATA_DIR / "video"
VIDEO_FILENAME = "v%05d.mp4"   # segment muxer 按段序号填充，与音频段同节奏

# 视频画质：用户可选。映射到 rank_m3u8 的目标清晰度分。原画(origin)文件巨大，默认给折中的高清。
VIDEO_QUALITY_PATH = DATA_DIR / "video_quality.txt"
VIDEO_QUALITY_DEFAULT = "hd"
# 顺序即界面下拉顺序；label 给前端展示，target 是清晰度阶梯目标分（见 audio_capture._quality_score）
VIDEO_QUALITY_CHOICES = (
    {"value": "smooth", "label": "流畅（最省空间）", "target": 30},
    {"value": "sd", "label": "标清", "target": 50},
    {"value": "hd", "label": "高清（推荐）", "target": 70},
    {"value": "origin", "label": "原画（文件很大）", "target": 100},
)
_VIDEO_QUALITY_VALUES = {c["value"] for c in VIDEO_QUALITY_CHOICES}
VIDEO_QUALITY_TARGETS = {c["value"]: c["target"] for c in VIDEO_QUALITY_CHOICES}


def get_video_quality() -> str:
    """读用户选择的视频画质；缺失/非法回退默认。"""
    try:
        q = VIDEO_QUALITY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return VIDEO_QUALITY_DEFAULT
    return q if q in _VIDEO_QUALITY_VALUES else VIDEO_QUALITY_DEFAULT


def set_video_quality(quality: str) -> bool:
    """保存视频画质选择；非法值忽略。返回是否成功。"""
    quality = (quality or "").strip()
    if quality not in _VIDEO_QUALITY_VALUES:
        return False
    try:
        VIDEO_QUALITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        VIDEO_QUALITY_PATH.write_text(quality, encoding="utf-8")
        return True
    except OSError:
        return False

# audio/ 下的保留目录名（非房间号），房间目录扫描时排除
RESERVED_AUDIO_SUBDIRS = {"failed", "pending", "done"}

# 弹幕/评论/直播数据库（由当前后端写入；audio_only 模式只负责录音录像，不产生弹幕）
EVENTS_DB = DATA_DIR / "multi_events.db"

# 直播状态 / 弹幕后端：
# - audio_only：默认。只用项目自有取流逻辑判断开播并驱动录音录像，不连接弹幕 WSS。
# - sidecar：可选。连接外部本地 JSON sidecar 服务以接收弹幕/进场/点赞事件。
DANMU_BACKEND = os.environ.get("LIVEWATCH_DANMU_BACKEND", "audio_only").strip().lower() or "audio_only"
DOUYIN_SIDECAR_WS = os.environ.get("LIVEWATCH_DOUYIN_SIDECAR_WS", "ws://127.0.0.1:1088").strip()

# 导出目录（用户数据）
EXPORT_DIR = DATA_DIR / "exports"
AI_CONFIG_PATH = DATA_DIR / "ai_config.json"
AI_REPORT_DIR = EXPORT_DIR / "ai_reports"

# 信任 cookie 缓存、房间清单、日志（用户数据）
COOKIE_CACHE = DATA_DIR / "browser_cookies.json"
SHORT_VIDEO_COOKIE_CACHE = DATA_DIR / "short_video_cookies.json"
DOUYIN_LOGIN_STATE_JSON = DATA_DIR / "douyin_login_state.json"
ROOMS_JSON = DATA_DIR / "rooms.json"
PENDING_JSON = DATA_DIR / "pending_anchors.json"  # 待开播主播清单（只有 sec_user_id，等开播探测直播号）
ANCHOR_PROFILE_CACHE = DATA_DIR / "anchor_profiles.json"
AVATAR_CACHE_DIR = DATA_DIR / "avatar_cache"
SHORT_VIDEO_JOBS_JSON = DATA_DIR / "short_video_jobs.json"
SHORT_VIDEO_PROFILE_CACHE_JSON = DATA_DIR / "short_video_profiles.json"
SHORT_VIDEO_PARSE_CACHE_JSON = DATA_DIR / "short_video_parse_cache.json"
SHORT_VIDEO_BENCHMARKS_JSON = DATA_DIR / "short_video_benchmarks.json"
SHORT_VIDEO_ASSET_DIR = DATA_DIR / "short_video_assets"
COMMENT_LEADS_JSON = DATA_DIR / "comment_leads.json"
COMMENT_LEADS_STATE_JSON = DATA_DIR / "comment_leads_seen.json"
COMMENT_LEADS_LOGIN_STATE_JSON = DATA_DIR / "comment_leads_login_state.json"
COMMENT_LEADS_EXPORT_DIR = EXPORT_DIR / "comment_leads"
COMMENT_LEADS_PROFILE_DIR = DATA_DIR / "comment_leads_browser_profile"
LOG_DIR = DATA_DIR / "logs"

# ---------- 手机号账号登录（短信与支付密钥仅保存在远端账号服务） ----------
# 客户端只调用 HTTPS 账号服务；本地只保存经系统保护的远端会话，不保存短信/支付密钥。
_ACCOUNT_RUNTIME_URL = str(getattr(license_runtime, "ACCOUNT_API_URL", "") or "").strip().rstrip("/")
_ACCOUNT_RUNTIME_PUBLIC_KEY = str(getattr(license_runtime, "ACCOUNT_PUBLIC_KEY", "") or "").strip()
_UPDATE_RUNTIME_PUBLIC_KEY = str(getattr(license_runtime, "UPDATE_PUBLIC_KEY", "") or "").strip()
ACCOUNT_API_BASE_URL = os.environ.get("LIVEWATCH_ACCOUNT_API_BASE_URL", _ACCOUNT_RUNTIME_URL or "https://anyq.site").strip().rstrip("/")
ACCOUNT_SESSION_PATH = DATA_DIR / "account_session.json"
ACCOUNT_REQUEST_TIMEOUT_SEC = max(3.0, float(os.environ.get("LIVEWATCH_ACCOUNT_REQUEST_TIMEOUT_SEC", "12")))
ACCOUNT_REFRESH_INTERVAL_SEC = max(60, int(os.environ.get("LIVEWATCH_ACCOUNT_REFRESH_INTERVAL_SEC", "600")))
# This desktop product is fixed at build time.  It must not be selected by a
# browser request or a user-editable setting.
ACCOUNT_PRODUCT_ID = str(getattr(license_runtime, "ACCOUNT_PRODUCT_CODE", "") or "replay_shrimp").strip()
# Public Ed25519 SPKI DER keys, base64url encoded.  These are safe to ship; the
# matching private key remains only in the remote account service.
ACCOUNT_LICENSE_PUBLIC_KEYS = {
    "account-v1": _ACCOUNT_RUNTIME_PUBLIC_KEY or "MCowBQYDK2VwAyEACqLAEE2KnduTFtw1gVQIExS1qLRa-XI3TaWpbchMbKc",
}
# A missing update key intentionally means that the client cannot trust an
# update manifest.  Commercial packaging requires this key; it is never read
# from a user-editable environment variable in the installed application.
UPDATE_RELEASE_PUBLIC_KEYS = {"update-v1": _UPDATE_RUNTIME_PUBLIC_KEY} if _UPDATE_RUNTIME_PUBLIC_KEY else {}

# ---------- 商业授权（开发态默认不强制，商业包通过编译期 license_runtime 开启） ----------
LICENSE_PATH = DATA_DIR / "license.json"
LICENSE_CLOCK_PATH = DATA_DIR / "license_clock.json"
_DEFAULT_LICENSE_PRODUCT_CODE = "lead_shrimp" if _LEAD_SHRIMP_STANDALONE else "live_replay_xia"
_DEFAULT_LICENSE_PRODUCT_SALT = "lead_shrimp_device_v1" if _LEAD_SHRIMP_STANDALONE else "live_replay_xia_device_v1"
LICENSE_PRODUCT_CODE = str(
    getattr(license_runtime, "LICENSE_PRODUCT_CODE", "") or _DEFAULT_LICENSE_PRODUCT_CODE
).strip()
LICENSE_PRODUCT_SALT = _DEFAULT_LICENSE_PRODUCT_SALT
if license_runtime.LICENSE_ENFORCE:
    # 商业包的授权开关、公钥、服务地址来自编译期注入，不接受本机环境变量覆盖。
    LICENSE_ENFORCE = True
    LICENSE_PUBLIC_KEY = license_runtime.LICENSE_PUBLIC_KEY.strip()
    LICENSE_SERVER_URL = license_runtime.LICENSE_SERVER_URL.strip().rstrip("/")
else:
    _LICENSE_ENV_PREFIX = "LEADSHRIMP" if _LEAD_SHRIMP_STANDALONE else "LIVEWATCH"
    LICENSE_ENFORCE = os.environ.get(f"{_LICENSE_ENV_PREFIX}_LICENSE_ENFORCE", "").strip().lower() in {"1", "true", "yes", "on"}
    LICENSE_PUBLIC_KEY = os.environ.get(f"{_LICENSE_ENV_PREFIX}_LICENSE_PUBLIC_KEY", license_runtime.LICENSE_PUBLIC_KEY).strip()
    LICENSE_SERVER_URL = os.environ.get(f"{_LICENSE_ENV_PREFIX}_LICENSE_SERVER_URL", license_runtime.LICENSE_SERVER_URL).strip().rstrip("/")
LICENSE_REQUEST_TIMEOUT_SEC = max(3.0, float(os.environ.get("LIVEWATCH_LICENSE_REQUEST_TIMEOUT_SEC", "12")))
LICENSE_APP_VERSION = os.environ.get("LIVEWATCH_APP_VERSION", "1.0.0").strip() or "1.0.0"
LICENSE_REFRESH_INTERVAL_SEC = max(60, int(os.environ.get("LIVEWATCH_LICENSE_REFRESH_INTERVAL_SEC", "600")))
LICENSE_CLOCK_ROLLBACK_TOLERANCE_SEC = max(
    60,
    int(os.environ.get("LIVEWATCH_LICENSE_CLOCK_ROLLBACK_TOLERANCE_SEC", "300")),
)

# ---------- 待开播主播开播探测（profile_watch）----------
# 未开播主播只有主页链接、拿不到直播号(web_rid)。后台用匿名 headless 浏览器定期渲染其主页，
# 检测到开播即抠出直播号、转为正式监听房间。风控纪律：长间隔 + 抖动 + 串行 + 数量上限 + 撞验证页全局冷却。
MAX_PENDING_ANCHORS = 20           # 待开播主播数量上限，防轮询面铺太大
PROFILE_POLL_SEC = 240             # 每个待开播主播两次探测的基准间隔（4 分钟）
PROFILE_POLL_JITTER_SEC = 120      # 探测间隔随机抖动上限，错开各主播
PROFILE_LOOP_TICK_SEC = 20         # 轮询线程醒来检查「到期待探测」的节拍
PROFILE_CHECK_GAP_SEC = 8          # 两次浏览器探测之间的最小间隔，绝不并发开浏览器
PROFILE_RENDER_TIMEOUT_SEC = 25    # 单次主页渲染最长等待

# 离线声纹分析结果（生成型用户数据，落 DATA_DIR）。声纹分析独立运行，导出时只读 CSV，不影响监听/录音线程。
# 开发态保持原 speaker_change_analysis 目录，避免动到既有离线分析产物。
SPEAKER_ANALYSIS_DIR = (DATA_DIR / "speaker_analysis") if _DATA_ENV else (ROUTE_DIR.parent / "speaker_change_analysis")
SPEAKER_LABELS_CSV = SPEAKER_ANALYSIS_DIR / "speaker_labels.csv"
SPEAKER_ANALYSIS_DB = SPEAKER_ANALYSIS_DIR / "speaker_analysis.db"
SPEAKER_DB_PATH = DATA_DIR / "speaker_labels.db"
SPEAKER_DELAY_SEC = 120
SPEAKER_POLL_SEC = 60
SPEAKER_BATCH_SIZE = 20
SPEAKER_MATCH_THRESHOLD = 0.70
SPEAKER_MERGE_THRESHOLD = 0.70
SPEAKER_PROFILE_UPDATE_THRESHOLD = 0.80
SPEAKER_NEW_CONFIRM_SEGMENTS = 3

# 录音参数
SEGMENT_SEC = 60            # 每段录制时长
TRANSCRIBE_MIN_FILE_AGE_SEC = 20  # 跳过刚生成/可能仍在写入的音频文件
TRANSCRIBE_MIN_FILE_SIZE = 1024   # 小于此字节数的视为空/残段，隔离不转写
# 旧的「靠 mtime 静默判封口」机制已弃用：现在封口权威是 segment_list csv，
# 段一旦出现在 csv 即已封口，转写直接读 recording_timeline 的待转写段，无需再靠静默判断。
TRANSCRIBE_SEALED_QUIET_SEC = 90  # 保留常量供旧路径兼容，新链路不依赖
ASR_THREADS = 4            # SenseVoice 推理线程
RECORD_REFERER = "Referer: https://live.douyin.com/\r\n"
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"
MAX_STREAM_TRIES = 4       # 404/403 时换备用流地址的最大尝试数

# ---------- segment muxer（单房间单 ffmpeg 连续录、零丢失）参数 ----------
# 文件名只放连续序号（seqNNNNN.mp3），时间全部进台账 recording_timeline。
# 注意：-strftime 1 会让 %d 变成「月内日」，与连续序号互斥，故文件名不含时间。
SEGMENT_FILENAME = "seq%05d.mp3"   # ffmpeg -segment 输出模板
SEGMENT_LIST_NAME = "segments.csv"  # 每个房间目录内的封口权威清单（每次 spawn 覆盖重写）
MUXER_POLL_SEC = 2.0               # 轮询 segment_list 探测新封口段的间隔
MUXER_RESPAWN_BACKOFF_SEC = 3.0    # ffmpeg 退出后重启基准退避
MUXER_RESPAWN_JITTER_SEC = 2.0     # 重启退避抖动上限
MUXER_NO_CANDIDATES_BACKOFF_SEC = 30.0   # 取址为空（多半下播/风控）时退避
MUXER_NO_DATA_TIMEOUT_SEC = 120.0  # 必须 > SEGMENT_SEC；segment muxer 当前段常到封口才写入，过早会误杀
MUXER_INSTANT_FAIL_SEC = 8.0       # ffmpeg 启动后存活不足此秒且 0 段→视为瞬时失败，升级退避
MUXER_MAX_BACKOFF_SEC = 60.0       # 退避上限
GAP_MIN_SEC = 2.0                  # 覆盖断档小于此秒数不记 gap（重启缝隙忽略不计）


def ensure_dirs() -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    AI_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SHORT_VIDEO_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    COMMENT_LEADS_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    SPEAKER_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)


def parse_seq(stem: str) -> int | None:
    """从文件名主干解析序号：兼容 `seq00007` 与裸数字 `7`。无法解析返回 None。"""
    m = _SEQ_RE.fullmatch(stem.strip())
    return int(m.group(1)) if m else None


def max_segment_number(rid: str) -> int:
    """房间目录里现存最大录音序号（无文件返回 0）。文件系统侧的 seq 兜底。"""
    room_dir = AUDIO_DIR / str(rid)
    if not room_dir.exists():
        return 0
    max_seq = 0
    for p in room_dir.glob("*.mp3"):
        seq = parse_seq(p.stem)
        if seq is not None:
            max_seq = max(max_seq, seq)
    return max_seq


def next_segment_path(rid: str) -> Path:
    """为房间分配下一个顺序录音路径 audio/<房间号>/seqNNNNN.mp3。

    seq = 现有最大序号 + 1（对空洞稳健：删了中间段也不复用旧号，避免覆盖）。
    一个直播间一个文件夹、录音按录制顺序编号，便于人工回听与按导出文字定位录音。
    """
    room_dir = AUDIO_DIR / str(rid)
    room_dir.mkdir(parents=True, exist_ok=True)
    return room_dir / (SEGMENT_FILENAME % (max_segment_number(rid) + 1))


def next_segment_number(rid: str) -> int:
    """返回房间下一个可用的录音序号（文件系统侧）。连续分片 ffmpeg 的兜底起号。"""
    room_dir = AUDIO_DIR / str(rid)
    room_dir.mkdir(parents=True, exist_ok=True)
    return max_segment_number(rid) + 1


def room_audio_dirs() -> list[Path]:
    """audio/ 下的房间目录（排除 failed/ 等保留目录）。供转写按房间扫描。"""
    if not AUDIO_DIR.exists():
        return []
    return [
        p for p in AUDIO_DIR.iterdir()
        if p.is_dir() and p.name not in RESERVED_AUDIO_SUBDIRS
    ]


def ensure_export_dir() -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
