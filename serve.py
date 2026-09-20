#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板任务池 —— 本地看板服务 + 内嵌调度器

单进程完成三件事：
  1. 看板 UI：新建卡片 / 审核通过 / 打回重做
  2. 内嵌调度器：每 N 秒扫描 1-待开始/，领取一张卡，拉起 WorkBuddy CLI 无头执行
  3. 状态流转：用文件移动实现，同盘内原子

目录结构（脚本在根，运行数据集中在 runtime/）：
  <看板项目>/
    serve.py          启动入口（HTTP 服务 + 内嵌调度器都在这一个文件里）
    automation/       执行 prompt
    example/tasks/    卡片格式样板（不参与运行，见 example/README.md）
    projects/         「统一放看板下」的各项目 task 目录（用到才有）
    config.json       本机主配置（不入库，见 config.example.json）
    projects.json     本机项目清单（不入库，见 projects.example.json）
    runtime/          运行数据区：.runs/ .run-state.json

**卡片目录与贴图目录都是可配置的**：config.json 的 `tasks_dir` 指定卡片放哪（比如直接
放到业务项目的 task/ 里），`assets_dir` 指定截图放哪；都没配就回落到 `runtime/tasks`
和 `<tasks_dir>/assets`。工具本体和卡片因此可以分家，贴图跟着卡片走。

卡片正文里的图片一律写成**相对卡片文件**的 `../assets/<卡片ID>/xxx.png`（正斜杠）。
绝对路径里的反斜杠在 Markdown 里是转义符，会被编辑器吃掉导致图显示不出来；
相对路径则换机器、搬目录、卡片在状态目录间移动都不会失效。

状态即目录（tasks_dir 下的子目录）：
  0-待确认/  人工闸门：调度器永不拾取，需人点「开始」才入队（草稿 / 待定口径）
  1-待开始/  新任务与返工任务的唯一入口队列
  2-进行中/  Agent 正在处理
  3-待审核/  Agent 已完成，等人审核
  4-已发布/  终态
  5-需介入/  执行失败、超时、或 Agent 报告受阻

分工：
  - 本服务独占元数据（comment_round / review_round / claimed_at / updated_at）
  - Agent 只独占内容（执行结果小节 + outcome 字段）

启动:
    python serve.py            # 默认 http://127.0.0.1:8770
    python serve.py 9000       # 指定端口
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, parse_qs, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 配置放根目录（改路径的地方一眼找得到），运行产生的数据集中在 runtime/ 一个子目录里。
# 卡片目录（tasks）另有配置：见 config.json 的 tasks_dir，默认才落在 runtime/tasks。
ROOT = Path(__file__).resolve().parent          # 启动入口
BOARD_DIR = ROOT / "runtime"                    # 运行数据区
DEFAULT_TASKS_DIR = BOARD_DIR / "tasks"
RUNS_DIR = BOARD_DIR / ".runs"
CONFIG_FILE = ROOT / "config.json"
CONFIG_EXAMPLE_FILE = ROOT / "config.example.json"            # 脱敏模板（入库）
STATE_FILE = BOARD_DIR / ".run-state.json"
PROJECTS_FILE = ROOT / "projects.json"          # 项目清单 + 当前激活项目
PROJECTS_EXAMPLE_FILE = ROOT / "projects.example.json"        # 脱敏模板（入库）
CENTRAL_PROJECTS_DIR = ROOT / "projects"         # 「统一放看板下」的项目根
PROMPT_FILE = ROOT / "automation" / "executor.prompt.txt"

PORT = 8770

# 贴图资源：按卡片分目录，与状态目录解耦（卡片移动时图片不动）
# 具体位置见 config.json 的 assets_dir，默认跟卡片目录走：<tasks_dir>/assets
DRAFT_NAME = "_draft"
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DRAFT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# 「待确认」是人工闸门：调度器只从「待开始」取卡，这里的卡片永远不会被自动拾取
DIR_CONFIRM = "0-待确认"
DIR_PENDING = "1-待开始"
DIR_RUNNING = "2-进行中"
DIR_REVIEW = "3-待审核"
DIR_DONE = "4-已发布"
DIR_BLOCKED = "5-需介入"
STATE_DIRS = [DIR_CONFIRM, DIR_PENDING, DIR_RUNNING, DIR_REVIEW, DIR_DONE, DIR_BLOCKED]

# 旧版扁平卡片按 status 字段迁移到目录
LEGACY_STATUS_DIR = {
    "待开始": DIR_PENDING,
    "进行中": DIR_RUNNING,
    "待审核": DIR_REVIEW,
    "已发布": DIR_DONE,
    "失败": DIR_BLOCKED,
}

# 引入「待确认」后各状态目录整体后移一位；启动时自动把旧编号目录里的卡片搬过来
LEGACY_DIR_RENAME = {
    "0-待开始": DIR_PENDING,
    "1-进行中": DIR_RUNNING,
    "2-待审核": DIR_REVIEW,
    "3-已发布": DIR_DONE,
    "4-需介入": DIR_BLOCKED,
}

PRIORITIES = ["高", "中", "低"]
PRI_CLASS = {"高": "pri-high", "中": "pri-mid", "低": "pri-low"}

META_ORDER = [
    "id", "title", "priority", "model",
    "comment_round", "review_round",
    "claimed_at", "updated_at", "outcome",
]
# model 是可选字段：留空表示跟随全局默认模型
QUOTED_META = {"title", "model", "claimed_at", "updated_at"}
SECTION_ORDER = ["需求", "处理意见", "执行结果"]

FM_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$", re.S)
SEC_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
TPL_RE = re.compile(r"\{\{(\w+)\}\}")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEFAULT_CONFIG = {
    "cli_path": "",
    "node_path": "",
    "permission_mode": "bypassPermissions",
    "model": "",
    "available_models": [],
    "max_turns": 80,
    "timeout_sec": 1800,
    "fallback_model": "",
    "auto_scan": True,
    "scan_interval_sec": 60,
    # 同时执行的任务数（页面上可调）
    "max_concurrent": 3,
    # 实时反馈：stream-json 逐事件回传，partial 再打开逐字流式（思考/正文可见）
    "live_partial": True,
    "live_show_thinking": True,
}

# 并发上限：再多会明显抢机器资源（每个任务都是一个独立的 CLI 进程）
MAX_CONCURRENT_LIMIT = 8

# 实时反馈的取数上限
LIVE_TAIL_CHARS = 40000         # 单次最多回传多少字符，超出让前端重置
LIVE_MAX_CHARS = 400000         # 转录文件超过这个长度只保留末尾
TOOL_ARG_LIMIT = 240            # 工具参数摘要长度
TOOL_RESULT_LIMIT = 300         # 工具结果摘要长度


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def as_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bootstrap_from_example(dst: Path, src: Path) -> None:
    """本机首次运行：从入库模板复制一份本机配置出来。

    config.json / projects.json 含绝对路径与 CLI 安装位置，不入库；仓库里只有
    *.example.json 模板。这里补齐缺失的本机文件，让 clone 下来直接能跑。
    已有本机文件时一字不动。
    """
    if dst.exists() or not src.exists():
        return
    try:
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[bootstrap] 已从 {src.name} 生成本机 {dst.name}")
    except Exception as exc:
        print(f"[warn] 生成本机 {dst.name} 失败: {exc}")


def _read_config_raw() -> dict:
    """只读 config.json 本体，不叠加项目路径（迁移 / 写回时用）。"""
    _bootstrap_from_example(CONFIG_FILE, CONFIG_EXAMPLE_FILE)
    config = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
    except Exception as exc:
        print(f"[warn] config.json 解析失败，使用默认配置: {exc}")
    return config


def load_config() -> dict:
    """主配置 + 当前激活项目的路径覆盖。

    项目清单在 projects.json（见 load_projects）。config.json 里的
    project_root / tasks_dir / assets_dir 退化为兼容字段：切换项目时同步写回，
    这样所有老读取方（tasks_dir()、execute_one…）一行不改就拿到正确路径。
    """
    config = _read_config_raw()
    proj = active_project()
    if proj:
        if proj.get("root"):
            config["project_root"] = proj["root"]
        if proj.get("tasks_dir"):
            config["tasks_dir"] = proj["tasks_dir"]
        if proj.get("assets_dir"):
            config["assets_dir"] = proj["assets_dir"]
    return config


# ---- 多项目管理 ----------------------------------------------------------

def load_projects() -> dict:
    """projects.json：{active: 名称, projects: {名称: {root, tasks_dir, assets_dir}}}。

    文件缺失 / 损坏时从 config.json 迁移一份现存配置，老安装无缝升级。
    """
    _bootstrap_from_example(PROJECTS_FILE, PROJECTS_EXAMPLE_FILE)
    try:
        if PROJECTS_FILE.exists():
            data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects"), dict):
                data.setdefault("active", "")
                return data
    except Exception as exc:
        print(f"[warn] projects.json 解析失败，重新迁移: {exc}")

    config = _read_config_raw()
    projects = {}
    name = ""
    root = str(config.get("project_root") or "").strip()
    if root and Path(root).is_dir():
        name = Path(root).name or "default"
        projects[name] = {
            "root": root,
            "tasks_dir": str(config.get("tasks_dir") or Path(root) / "task"),
            "assets_dir": str(config.get("assets_dir") or ""),
        }
    data = {"active": name, "projects": projects}
    save_projects(data)
    if projects:
        print(f"[migrate] 项目配置已从 config.json 迁移到 projects.json（{name}）")
    return data


def save_projects(data: dict) -> None:
    try:
        PROJECTS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[warn] 写入 projects.json 失败: {exc}")


def active_project() -> dict:
    """当前激活项目（dict 带 name 键）；没有项目时返回空 dict。"""
    data = load_projects()
    name = str(data.get("active") or "")
    proj = data["projects"].get(name)
    if proj is None and data["projects"]:
        # active 指向的项目被删了：退回第一个，避免整板卡片“凭空消失”
        name = next(iter(data["projects"]))
        proj = data["projects"][name]
        data["active"] = name
        save_projects(data)
    if proj is None:
        return {}
    merged = dict(proj)
    merged["name"] = name
    return merged


def set_active_project(name: str) -> dict:
    data = load_projects()
    proj = data["projects"].get(name)
    if proj is None:
        raise KeyError(f"项目「{name}」不存在")
    data["active"] = name
    save_projects(data)
    # 同步写回 config.json 的兼容字段，排查问题时直接看 config 也能对上
    config = _read_config_raw()
    config["project_root"] = proj.get("root") or ""
    config["tasks_dir"] = proj.get("tasks_dir") or ""
    config["assets_dir"] = proj.get("assets_dir") or ""
    save_config(config)
    return proj


def concurrency_limit(config: dict) -> int:
    """同时执行的任务数，夹在 1..MAX_CONCURRENT_LIMIT 之间。"""
    value = as_int(config.get("max_concurrent"), DEFAULT_CONFIG["max_concurrent"])
    return max(1, min(MAX_CONCURRENT_LIMIT, value))


def save_config(config: dict) -> None:
    try:
        CONFIG_FILE.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[warn] 写入 config.json 失败: {exc}")


def tasks_dir_from(config: dict) -> Path:
    """卡片目录：config.json 的 `tasks_dir` 说了算，没配就回落到 runtime/tasks。

    卡片故意可以和工具分家（比如放在真正的业务项目里），所以这里读配置而不是写死路径。
    配了但目录不存在时按原样返回——交给 ensure_dirs() 去建，不在这里偷偷回落，
    否则「配置写错路径」会表现为「卡片凭空消失」，很难查。
    """
    raw = str(config.get("tasks_dir") or "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_TASKS_DIR


def tasks_dir() -> Path:
    return tasks_dir_from(load_config())


def assets_dir_from(config: dict) -> Path:
    """贴图目录：config.json 的 `assets_dir` 说了算，没配就跟卡片目录同级。

    贴图跟着卡片走（而不是跟着工具走）：卡片正文里写相对路径 `../assets/...`，
    于是把整个 task/ 拷到别的机器、换盘符、换项目，卡片和图都还能对上。
    """
    raw = str(config.get("assets_dir") or "").strip()
    if raw:
        return Path(raw)
    return tasks_dir_from(config) / "assets"


def assets_dir() -> Path:
    return assets_dir_from(load_config())


def draft_dir() -> Path:
    return assets_dir() / DRAFT_NAME


def card_image_ref(path: Path) -> str:
    """把图片绝对路径写成 Markdown 里能正常显示的相对引用（相对卡片所在的状态目录）。"""
    try:
        rel = os.path.relpath(str(path), str(tasks_dir()))
    except ValueError:
        return str(path).replace("\\", "/")
    return "../" + rel.replace("\\", "/")


def resolve_image_ref(ref: str) -> Path:
    """把卡片正文里的图片引用解析成真实路径。

    支持三种写法：
      1. `../assets/T-002/x.png`  相对卡片所在目录 —— 新卡片的写法，md 编辑器可直接显示
      2. `assets/T-002/x.png`     相对 tasks 根目录
      3. `E:\\...\\assets\\T-002\\x.png`  绝对路径 —— 老卡片，兼容保留

    前两种只差一层，剥掉 `..` 后都以 tasks 根为基准，结果一致。
    """
    ref = str(ref or "").strip()
    if not ref:
        return Path("")
    if re.match(r"^[A-Za-z]:[\\/]", ref) or ref.startswith(("/", "\\")):
        return Path(ref)
    parts = [p for p in ref.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return tasks_dir().joinpath(*parts) if parts else tasks_dir()


def under_assets_dir(target: Path) -> bool:
    """安全校验：目标必须在贴图目录内，防止 /api/image 被拿来读任意文件。"""
    try:
        base = str(assets_dir().resolve()).lower() + os.sep
        probe = str(Path(target).resolve()).lower()
    except OSError:
        return False
    return probe.startswith(base)


def resolve_node(config: dict):
    configured = str(config.get("node_path") or "").strip()
    if configured and Path(configured).exists():
        return configured

    found = shutil.which("node")
    if found:
        return found

    managed = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if managed.is_dir():
        for exe in sorted(managed.glob("*/node.exe"), reverse=True):
            return str(exe)
    return None


def resolve_project_root(config: dict) -> Path:
    """Agent 干活的目标项目根目录（不是看板目录）。

    两层区分很关键：
      - ROOT          看板系统自己（automation/、runtime/），Agent 不该乱动
      - project_root  真正的业务项目（如 E:\\Code\\eshop），Agent 在这里读写文件

    一定要在 config.json 里用 project_root 显式指定。没配时退而求其次取「卡片目录的
    上一级」——卡片通常就放在业务项目里（如 <项目>/task/），这比取看板自己的上级目录
    （工具搬出业务项目后就完全不对了）靠谱得多。
    """
    configured = str(config.get("project_root") or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_dir():
            return candidate.resolve()
        print(f"[warn] config.json 的 project_root 不存在，回退到默认：{candidate}")
    return tasks_dir_from(config).parent.resolve()


# --------------------------------------------------------------------------
# 贴图资源
# --------------------------------------------------------------------------

def image_refs(text: str) -> list:
    """取出 Markdown 里的图片绝对路径。"""
    return [m.group(1).strip() for m in IMG_RE.finditer(text or "") if m.group(1).strip()]


def commit_draft_assets(draft: str, card_id: str, need: str) -> str:
    """把草稿区的图片归入卡片目录，并把需求里的路径改写成正式路径。"""
    if not draft or not DRAFT_RE.match(draft):
        return need
    src = draft_dir() / draft
    if not src.is_dir():
        return need

    dst = assets_dir() / card_id
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            for item in list(src.iterdir()):
                shutil.move(str(item), str(dst / item.name))
            shutil.rmtree(src, ignore_errors=True)
        else:
            os.rename(src, dst)
    except OSError as exc:
        print(f"[warn] 草稿图片归位失败 {src} → {dst}: {exc}")
        return need

    new_text = card_image_ref(dst)
    # 正文里可能是相对写法（新）或绝对写法（老），两种都替换
    for old in (str(src), str(src).replace("/", "\\"), card_image_ref(src)):
        if old:
            need = need.replace(old, new_text)
    return need


def clean_stale_drafts(max_age_hours: int = 24) -> int:
    """清掉长时间未提交的草稿图片。"""
    if not draft_dir().is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for folder in draft_dir().iterdir():
        if not folder.is_dir():
            continue
        try:
            if folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# 运行状态
# --------------------------------------------------------------------------

_state_lock = threading.Lock()
_scan_wake = threading.Event()
_stop = threading.Event()


def wake_scanner() -> None:
    """卡片流转后提醒调度器尽快领卡。

    只在轮询开启时才唤醒：轮询被人关掉意味着「先别动」，此时任何流转都不该
    偷偷把 Agent 叫起来（尤其是「待确认」点「开始」，那只是一次入队）。
    手动点「立即执行一轮」不走这里，那个按钮本来就是要强制执行一轮。
    """
    if not load_config().get("auto_scan", True):
        return
    _scan_wake.set()

_state = {
    "running": False,
    "current_card": "",
    "started_at": "",
    "last_finished_at": "",
    "last_exit_code": None,
    "last_log": "",
    "last_model": "",
    "last_scan_at": "",
    "last_scan_action": "",
    # 正在跑的任务：card_id -> {log, model, started_at, title}
    # 并发执行后这里可能同时有多条；不进 .run-state.json（重启即作废）
    "tasks": {},
}

PERSIST_KEYS = ("last_finished_at", "last_exit_code", "last_log", "last_model")


def active_tasks() -> dict:
    with _state_lock:
        return dict(_state.get("tasks") or {})


def load_state() -> None:
    try:
        if STATE_FILE.exists():
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for key in PERSIST_KEYS:
                if key in saved:
                    _state[key] = saved[key]
    except Exception as exc:
        print(f"[warn] 读取运行状态失败: {exc}")


def save_state() -> None:
    try:
        with _state_lock:
            snapshot = {k: v for k, v in _state.items() if k != "tasks"}
        STATE_FILE.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[warn] 写入运行状态失败: {exc}")


def read_log_text(path_str: str, max_chars: int = LIVE_MAX_CHARS) -> str:
    """读取执行转录。文件很大时只读末尾，避免拖慢接口。"""
    if not path_str:
        return ""
    path = Path(path_str)
    try:
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_chars * 3:
                handle.seek(size - max_chars * 3)
            blob = handle.read()
    except OSError:
        return ""
    return blob.decode("utf-8", errors="replace")[-max_chars:]


def tail_log(path_str: str, limit: int = 2000) -> str:
    text = read_log_text(path_str).strip()
    return text[-limit:] if len(text) > limit else text


def append_log(path: Path, text: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        pass


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _brief(value, limit: int) -> str:
    """把工具参数 / 工具结果压成一行摘要。"""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            value = "\n".join(parts)
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def last_log_error(path: Path, limit: int = 300) -> str:
    """从执行转录里取一条可用于诊断的行（优先错误行，跳过表头与分隔线）。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    fallback = ""
    for line in reversed(lines):
        text = line.strip()
        if not text or text.startswith("#") or set(text) <= {"-"}:
            continue
        if "[错误]" in text:
            return text if len(text) <= limit else text[:limit] + "…"
        if not fallback and ("[CLI]" in text or "本轮结束" not in text):
            fallback = text
    return fallback if len(fallback) <= limit else fallback[:limit] + "…"


class StreamTranslator:
    """把 CLI 的 stream-json 事件流翻译成人类可读的进度转录。

    同时落两份文件：
      - 可读转录（.log）  —— 看板实时展示 / 事后排查
      - 原始事件流（.jsonl）—— 出问题时逐事件回溯

    去重策略：开启 --include-partial-messages 时，思考与正文统一由流式增量输出，
    assistant 事件只取 tool_use，因此不会重复；关闭时反过来从 assistant 事件取正文。
    """

    def __init__(self, log_handle, raw_handle, partial: bool = True, show_thinking: bool = True):
        self.log = log_handle
        self.raw = raw_handle
        self.partial = partial
        self.show_thinking = show_thinking
        self.block = None           # 当前打开的块：text / thinking / None
        self.marker_open = False    # 当前块是否已写过标题行
        self.saw_text = False       # 是否已经输出过正文
        self.tools = {}             # tool_use_id -> 工具名
        self.lock = threading.Lock()
        self.last_flush = 0.0

    # ---- 写入 ----

    def _write(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            try:
                self.log.write(text)
            except Exception:
                pass

    def _flush(self, force: bool = True) -> None:
        now = time.time()
        if not force and now - self.last_flush < 0.25:
            return
        self.last_flush = now
        with self.lock:
            try:
                self.log.flush()
            except Exception:
                pass

    def note(self, text: str) -> None:
        """调度器自己写的一行备注。"""
        self._finish_open_block()
        self._write(f"[{_clock()}] {text}\n")
        self._flush()

    def close(self) -> None:
        self._finish_open_block()
        self._flush()

    def _emit_marker(self, kind: str) -> None:
        if self.marker_open:
            return
        if self.block is None:
            self.block = kind
        label = {"thinking": "思考", "text": "回复"}.get(kind, kind)
        self._write(f"\n[{_clock()}] 【{label}】\n")
        self.marker_open = True

    def _finish_open_block(self) -> None:
        if self.marker_open and self.block in ("text", "thinking"):
            self._write("\n")
        self.block = None
        self.marker_open = False

    # ---- 事件分派 ----

    def feed(self, line: str) -> None:
        try:
            self.raw.write(line if line.endswith("\n") else line + "\n")
        except Exception:
            pass

        text = line.strip()
        if not text:
            return

        try:
            event = json.loads(text)
        except Exception:
            # 非 JSON 行 = CLI 自身打到 stderr 的提示（如 Git Bash 告警），留着便于排查
            self._write(f"[{_clock()}] [CLI] {text[:400]}\n")
            self._flush()
            return

        etype = event.get("type")
        if etype == "stream_event":
            self._on_stream_event(event.get("event") or {})
        elif etype == "assistant":
            self._on_assistant(event.get("message") or {})
        elif etype == "user":
            self._on_user(event.get("message") or {})
        elif etype == "result":
            self._on_result(event)
        elif etype == "system" and event.get("subtype") == "init":
            model = event.get("model") or "(默认)"
            session_id = str(event.get("session_id") or "")
            suffix = f" · 会话 {session_id[:8]}" if session_id else ""
            self._write(f"[{_clock()}] [会话] 模型 {model}{suffix}\n")
            self._flush()

    def _on_stream_event(self, event: dict) -> None:
        kind = event.get("type")

        if kind == "content_block_start":
            # 上游并不保证每个块都有配对 content_block_stop，开新块前先把上一块收尾
            self._finish_open_block()
            self.block = (event.get("content_block") or {}).get("type")
            self.marker_open = False
            return

        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            # 空增量不落标记行，否则会冒出「【思考】」后面什么都没有的空块
            if dtype == "thinking_delta":
                if not self.show_thinking:
                    return
                chunk = str(delta.get("thinking") or "")
                if not chunk:
                    return
                self._emit_marker("thinking")
                self._write(chunk)
                self._flush(force=False)
            elif dtype == "text_delta":
                chunk = str(delta.get("text") or "")
                if not chunk:
                    return
                self._emit_marker("text")
                self.saw_text = True
                self._write(chunk)
                self._flush(force=False)
            # input_json_delta / signature_delta：工具参数交给 assistant 事件统一呈现
            return

        if kind == "content_block_stop":
            if self.marker_open and self.block in ("text", "thinking"):
                self._write("\n")
            self.block = None
            self.marker_open = False
            self._flush()
            return

        if kind == "message_stop":
            self._finish_open_block()
            self._flush()

    def _on_assistant(self, message: dict) -> None:
        for block in message.get("content") or []:
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name") or "工具")
                self.tools[str(block.get("id") or "")] = name
                self._finish_open_block()
                args = _brief(block.get("input"), TOOL_ARG_LIMIT)
                self._write(f"[{_clock()}] ▶ 调用 {name}  {args}\n")
            elif not self.partial and btype in ("text", "thinking"):
                if btype == "thinking" and not self.show_thinking:
                    continue
                self._emit_marker(btype)
                if btype == "text":
                    self.saw_text = True
                body = block.get("text") if btype == "text" else block.get("thinking")
                self.block = None
                self.marker_open = False
                self._write(str(body or "").strip() + "\n")
        self._flush()

    def _on_user(self, message: dict) -> None:
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            name = self.tools.get(str(block.get("tool_use_id") or ""), "工具")
            flag = "失败" if block.get("is_error") else "完成"
            summary = _brief(block.get("content"), TOOL_RESULT_LIMIT)
            self._write(f"[{_clock()}] ⤷ {name} {flag} · {summary}\n")
        self._flush()

    def _on_result(self, event: dict) -> None:
        self._finish_open_block()
        subtype = str(event.get("subtype") or "")
        is_error = bool(event.get("is_error"))
        bits = [subtype or ("失败" if is_error else "完成")]
        if event.get("num_turns"):
            bits.append(f"{event['num_turns']} 轮")
        if event.get("duration_ms"):
            bits.append(f"{as_int(event['duration_ms']) / 1000:.1f}s")
        self._write(f"\n[{_clock()}] ■ 本轮结束 · " + " · ".join(bits) + "\n")

        final = str(event.get("result") or "").strip()
        if is_error:
            self._write(f"[{_clock()}] [错误] {final[:600] or 'CLI 报告执行失败'}\n")
        elif final and not self.saw_text:
            # 没走流式（例如关掉了 live_partial）时兜底，保证正文至少出现一次
            self._write(f"[{_clock()}] 【最终回复】\n{final[:4000]}\n")
        self._flush()


# --------------------------------------------------------------------------
# 卡片读写
# --------------------------------------------------------------------------

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _quote(value: str) -> str:
    return '"' + str(value).replace('"', "'") + '"'


def parse_card(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    body = raw

    matched = FM_RE.match(raw)
    if matched:
        for line in matched.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = _unquote(value)
        body = matched.group(2)

    sections: dict = {}
    hits = list(SEC_RE.finditer(body))
    for i, hit in enumerate(hits):
        start = hit.end()
        end = hits[i + 1].start() if i + 1 < len(hits) else len(body)
        sections[hit.group(1).strip()] = body[start:end].strip()

    meta.setdefault("id", path.stem)
    meta.setdefault("title", path.stem)
    meta.setdefault("priority", "中")
    meta.setdefault("comment_round", "0")
    meta.setdefault("review_round", "0")
    meta["_sections"] = sections
    meta["_file"] = path.name
    meta["_dir"] = path.parent.name
    return meta


def write_card(path: Path, meta: dict, sections: dict) -> None:
    lines = ["---"]
    for key in META_ORDER:
        if key not in meta:
            continue
        value = str(meta[key] if meta[key] is not None else "")
        if key in QUOTED_META:
            lines.append(f"{key}: {_quote(value)}")
        elif key == "outcome":
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value or '0'}")
    lines.append("---")
    lines.append("")

    names = SECTION_ORDER + [n for n in sections if n not in SECTION_ORDER]
    for name in names:
        lines.append(f"## {name}")
        lines.append("")
        lines.append((sections.get(name) or "").strip())
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def load_cards() -> list:
    cards = []
    root = tasks_dir()
    for dirname in STATE_DIRS:
        folder = root / dirname
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                cards.append(parse_card(path))
            except Exception as exc:
                print(f"[warn] 跳过无法解析的卡片 {path.name}: {exc}")

    priority_rank = {name: idx for idx, name in enumerate(PRIORITIES)}

    def sort_key(card):
        return (priority_rank.get(card["priority"], len(PRIORITIES)), card["id"])

    cards.sort(key=sort_key)
    return cards


def next_id() -> str:
    biggest = 0
    root = tasks_dir()
    for dirname in STATE_DIRS:
        folder = root / dirname
        if not folder.is_dir():
            continue
        for path in folder.glob("*.md"):
            found = re.search(r"(\d+)", path.stem)
            if found:
                biggest = max(biggest, int(found.group(1)))
    return f"T-{biggest + 1:03d}"


def find_card(card_id: str):
    if not ID_RE.match(card_id or ""):
        return None
    root = tasks_dir()
    for dirname in STATE_DIRS:
        path = root / dirname / f"{card_id}.md"
        if path.exists():
            return path
    return None


def move_card(path: Path, target_dir: str, retries: int = 6):
    """同盘原子移动。目标已存在或源不存在时返回 None。"""
    dst = tasks_dir() / target_dir / path.name
    if not path.exists() or dst.exists():
        return None
    for _ in range(retries):
        try:
            os.rename(path, dst)
            return dst
        except PermissionError:
            time.sleep(0.3)
        except OSError as exc:
            print(f"[move] {path.name} → {target_dir} 失败: {exc}")
            return None
    print(f"[move] {path.name} → {target_dir} 失败：文件被占用")
    return None


# --------------------------------------------------------------------------
# 目录初始化与迁移
# --------------------------------------------------------------------------

def ensure_dirs() -> None:
    root = tasks_dir()
    for dirname in STATE_DIRS:
        (root / dirname).mkdir(parents=True, exist_ok=True)


def migrate_flat_cards() -> int:
    """把旧结构 tasks/*.md 按 status 字段归位，并删除该字段。"""
    moved = 0
    root = tasks_dir()
    for path in sorted(root.glob("*.md")):
        try:
            meta = parse_card(path)
        except Exception as exc:
            print(f"[migrate] 跳过无法解析的卡片 {path.name}: {exc}")
            continue
        target = LEGACY_STATUS_DIR.get(str(meta.get("status") or ""), DIR_PENDING)
        dst = root / target / path.name
        if dst.exists():
            continue
        try:
            os.rename(path, dst)
        except OSError as exc:
            print(f"[migrate] {path.name} 移动失败: {exc}")
            continue
        fresh = parse_card(dst)
        write_card(dst, fresh, fresh["_sections"])
        moved += 1
    return moved


def migrate_state_dirs() -> int:
    """把旧编号目录（0-待开始…4-需介入）里的卡片搬到新编号目录。

    引入「待确认」后所有状态目录整体后移一位。目录存在但为空说明已经迁过了，
    直接跳过，所以这个函数可以反复执行。
    """
    moved = 0
    root = tasks_dir()
    for old, new in sorted(LEGACY_DIR_RENAME.items(), reverse=True):
        src = root / old
        if not src.is_dir():
            continue
        dst = root / new
        dst.mkdir(parents=True, exist_ok=True)
        for path in sorted(src.glob("*.md")):
            target = dst / path.name
            if target.exists():
                continue
            try:
                os.rename(path, target)
                moved += 1
            except OSError as exc:
                print(f"[migrate] {path.name} {old} → {new} 失败: {exc}")
        try:
            if not any(src.iterdir()):
                src.rmdir()
        except OSError:
            pass
    return moved


def sweep_orphans() -> int:
    """启动时清扫：2-进行中/ 里的残留说明上次被中断。"""
    folder = tasks_dir() / DIR_RUNNING
    if not folder.is_dir():
        return 0
    count = 0
    for path in sorted(folder.glob("*.md")):
        dst = move_card(path, DIR_BLOCKED)
        if dst is None:
            continue
        fresh = parse_card(dst)
        sections = fresh["_sections"]
        note = "[调度器] 上次执行被中断（服务重启时该卡仍停留在「进行中」），已移入「需介入」。"
        prev = sections.get("执行结果", "").strip()
        sections["执行结果"] = f"{prev}\n\n{note}".strip() if prev else note
        fresh["updated_at"] = now_iso()
        write_card(dst, fresh, sections)
        count += 1
    return count


# --------------------------------------------------------------------------
# 调度器
# --------------------------------------------------------------------------

def normalize_model(value) -> str:
    """模型名合法性校验：不合法一律当「未指定」处理（回落到全局默认）。"""
    name = str(value or "").strip()
    return name if name and MODEL_RE.match(name) else ""


def card_model(meta: dict, config: dict) -> str:
    """卡片自带模型优先；没指定就用全局默认模型。"""
    return normalize_model(meta.get("model")) or normalize_model(config.get("model"))


def render_prompt(card_id: str, card_path: Path, project_root: Path) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    values = {
        "CARD_ID": card_id,
        "CARD_PATH": str(card_path),
        "PROJECT_ROOT": str(project_root),
    }
    return TPL_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def build_command(
    config: dict, model: str, card_id: str, card_path: Path, project_root: Path
) -> list:
    cli_path = str(config.get("cli_path") or "").strip()
    if not cli_path or not Path(cli_path).exists():
        raise RuntimeError(f"找不到 WorkBuddy CLI：{cli_path or '(未配置)'}")
    if not PROMPT_FILE.exists():
        raise RuntimeError(f"找不到执行 prompt：{PROMPT_FILE}")

    node = resolve_node(config)
    if not node:
        raise RuntimeError("找不到 node 可执行文件（检查 PATH 或 config.json 的 node_path）")

    prompt_text = render_prompt(card_id, card_path, project_root)
    if "{{" in prompt_text:
        leftovers = sorted(set(TPL_RE.findall(prompt_text)))
        raise RuntimeError(f"prompt 模板变量未替换：{leftovers}")

    command = [
        node, cli_path,
        "--print",
        "--output-format", "stream-json",
        "--permission-mode", str(config.get("permission_mode") or "bypassPermissions"),
    ]
    # 逐字流式：看板才能看到「思考中 / 正在写正文」的实时反馈
    if config.get("live_partial", True):
        command.append("--include-partial-messages")
    if model:
        command += ["--model", model]
        fallback = str(config.get("fallback_model") or "").strip()
        if fallback and fallback != model and MODEL_RE.match(fallback):
            command += ["--fallback-model", fallback]
    max_turns = as_int(config.get("max_turns"), 0)
    if max_turns > 0:
        command += ["--max-turns", str(max_turns)]
    command.append(prompt_text)
    return command


def pick_next():
    """从 1-待开始/ 取优先级最高的一张（同优先级按 ID 升序）。"""
    folder = tasks_dir() / DIR_PENDING
    if not folder.is_dir():
        return None
    priority_rank = {name: idx for idx, name in enumerate(PRIORITIES)}
    best = None
    for path in sorted(folder.glob("*.md")):
        try:
            meta = parse_card(path)
        except Exception as exc:
            print(f"[pick] 跳过无法解析的卡片 {path.name}: {exc}")
            continue
        key = (priority_rank.get(meta["priority"], len(PRIORITIES)), meta["id"])
        if best is None or key < best[0]:
            best = (key, meta)
    return best[1] if best else None


def claim(meta: dict):
    """占坑：把卡片从 1-待开始/ 原子移动到 2-进行中/，并写入元数据。"""
    root = tasks_dir()
    src = root / DIR_PENDING / meta["_file"]
    if not src.exists():
        return None
    dst = root / DIR_RUNNING / meta["_file"]
    if dst.exists():
        return None
    try:
        os.rename(src, dst)
    except OSError as exc:
        print(f"[claim] 领取失败 {meta['_file']}: {exc}")
        return None

    meta["claimed_at"] = now_iso()
    meta["updated_at"] = now_iso()
    meta["outcome"] = ""
    write_card(dst, meta, meta["_sections"])
    return dst


def decode_line(raw: bytes) -> str:
    """CLI 的 stdout 是 UTF-8，但夹带的 Windows 控制台提示可能是 GBK，需要回退解码。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def run_process(
    command: list, cwd: Path, log_path: Path, timeout: int, model: str, config: dict
) -> int:
    """拉起 CLI 并实时把 stream-json 事件翻译进转录文件。

    与旧版 subprocess.run 的关键差别：不阻塞等结束，而是起一条读取线程边读边写，
    看板轮询转录文件就能看到进行中的 AI 反馈。
    """
    raw_path = log_path.with_suffix(".jsonl")
    try:
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        raw_handle = open(raw_path, "w", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[exec] 无法创建日志文件 {log_path}: {exc}")
        return -2

    log_handle.write(f"# 触发时间 : {now_iso()}\n")
    log_handle.write(f"# 执行模型 : {model or '(默认)'}\n")
    log_handle.write(f"# 工作目录 : {cwd}\n")
    log_handle.write(f"# 命令     : {' '.join(command[:-1])} <PROMPT>\n")
    log_handle.write("-" * 60 + "\n")
    log_handle.flush()

    translator = StreamTranslator(
        log_handle,
        raw_handle,
        partial=bool(config.get("live_partial", True)),
        show_thinking=bool(config.get("live_show_thinking", True)),
    )

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except Exception as exc:
        append_log(log_path, f"\n[调度器] 进程启动失败: {exc}\n")
        translator.close()
        log_handle.close()
        raw_handle.close()
        return -2

    def pump() -> None:
        try:
            for chunk in iter(proc.stdout.readline, b""):
                translator.feed(decode_line(chunk))
        except Exception as exc:                       # 读取线程绝不能把主流程带走
            translator.note(f"[调度器] 读取输出异常: {exc}")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            translator._flush()

    reader = threading.Thread(target=pump, name="kanban-stdout", daemon=True)
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            pass

    reader.join(timeout=10)
    translator.close()
    log_handle.close()
    raw_handle.close()

    if timed_out:
        append_log(log_path, f"\n[{_clock()}] [调度器] 执行超过 {timeout} 秒，已强制终止。\n")
        return -1
    if proc.returncode is None:
        append_log(log_path, f"\n[{_clock()}] [调度器] 进程未正常返回退出码。\n")
        return -2
    return proc.returncode


def finalize(card_id: str, card_path: Path, exit_code: int, log_path: Path, timeout: int) -> str:
    """按退出码 + outcome 决定卡片去向，并把该任务从运行表中摘掉。"""
    with _state_lock:
        tasks = _state.get("tasks")
        if isinstance(tasks, dict):
            tasks.pop(card_id, None)
            remaining = list(tasks)
        else:
            remaining = []
        _state.update(
            running=bool(remaining),
            current_card=remaining[0] if remaining else "",
            last_finished_at=now_iso(),
            last_exit_code=exit_code,
        )
    save_state()

    try:
        fresh = parse_card(card_path)
    except Exception as exc:
        print(f"[finalize] 解析失败 {card_path}: {exc}")
        _scan_wake.set()
        return "error"

    card_id = fresh["id"]
    outcome = str(fresh.get("outcome") or "").strip().lower()

    if exit_code == 0 and outcome == "done":
        dst = move_card(card_path, DIR_REVIEW)
        if dst is None:
            print(f"[finalize] {card_id} 无法移入「待审核」")
            return "move_failed"
        fresh = parse_card(dst)
        fresh["review_round"] = str(as_int(fresh.get("comment_round")))
        fresh["updated_at"] = now_iso()
        write_card(dst, fresh, fresh["_sections"])
        print(f"[scan] {card_id} → {DIR_REVIEW}")
        return "review"

    dst = move_card(card_path, DIR_BLOCKED) or card_path
    fresh = parse_card(dst)

    notes = []
    if exit_code == -1:
        notes.append(f"[调度器] 执行超时（超过 {timeout} 秒），已强制终止。")
    elif exit_code != 0:
        notes.append(f"[调度器] 执行进程异常退出，退出码 {exit_code}。")
    if outcome == "blocked":
        notes.append("[调度器] Agent 报告任务受阻，需人工介入。")
    elif outcome != "done":
        notes.append(f"[调度器] Agent 未写入有效 outcome（读到：{outcome or '空'}），视为未完成。")

    if exit_code != 0 or outcome not in ("done", "blocked"):
        reason = last_log_error(log_path)
        if reason:
            notes.append(f"[调度器] 日志末行：{reason}")

    sections = fresh["_sections"]
    previous = sections.get("执行结果", "").strip()
    block = "\n".join(notes)
    # 同一份调度器备注若已在末尾就不重复追加（防止重试/多次失败时堆叠）
    if block and not previous.endswith(block):
        sections["执行结果"] = f"{previous}\n\n{block}".strip() if previous else block
    fresh["updated_at"] = now_iso()
    write_card(dst, fresh, sections)
    print(f"[scan] {card_id} → {DIR_BLOCKED}")
    return "blocked"


def execute_one(meta: dict) -> str:
    """领取一张卡片并**立即起一条后台线程**执行，扫描循环不等它跑完。

    这样同一轮扫描可以连续领多张卡（并发数由 config.max_concurrent 控制），
    每张卡的进度各自写进自己的转录文件，互不干扰。
    """
    config = load_config()
    card_id = meta["id"]
    card_path = claim(meta)
    if card_path is None:
        return "lost"

    # 卡片自带模型优先，没指定就用全局默认
    model = card_model(meta, config)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}-{card_id}.log"

    with _state_lock:
        tasks = _state.setdefault("tasks", {})
        tasks[card_id] = {
            "log": str(log_path),
            "model": model,
            "started_at": now_iso(),
            "title": str(meta.get("title") or ""),
        }
        _state.update(
            running=True,
            current_card=card_id,
            started_at=now_iso(),
            last_log=str(log_path),
            last_model=model,
        )
    save_state()
    print(f"[scan] 领取 {card_id}（并发 {len(active_tasks())}），模型 {model or '(默认)'}，日志 {log_path.name}")

    worker = threading.Thread(
        target=run_task,
        args=(card_id, card_path, model, log_path, config),
        name=f"kanban-task-{card_id}",
        daemon=True,
    )
    worker.start()
    return "started"


def run_task(card_id: str, card_path: Path, model: str, log_path: Path, config: dict) -> None:
    """单个任务的执行体（跑在自己的线程里）。"""
    timeout = max(60, as_int(config.get("timeout_sec"), 1800))
    project_root = resolve_project_root(config)
    try:
        command = build_command(config, model, card_id, card_path, project_root)
    except RuntimeError as exc:
        append_log(log_path, f"[调度器] 组装命令失败: {exc}\n")
        result = finalize(card_id, card_path, -2, log_path, timeout)
        print(f"[scan] {card_id} → {result}（命令组装失败）")
        _scan_wake.set()
        return

    try:
        exit_code = run_process(command, project_root, log_path, timeout, model, config)
    except Exception as exc:                 # 线程里任何异常都不能静默吞掉
        append_log(log_path, f"[调度器] 执行异常: {exc}\n")
        exit_code = -2

    result = finalize(card_id, card_path, exit_code, log_path, timeout)
    print(f"[scan] {card_id} → {result}（退出码 {exit_code}）")
    # 腾出并发槽位，立刻唤醒调度器去领下一张
    _scan_wake.set()


def scan_once(limit: int) -> str:
    """一次扫描：把并发槽位填满，最多再起 (limit - 运行中) 张卡。"""
    started = 0
    last = "idle"
    while True:
        if len(active_tasks()) >= limit:
            last = "full" if not started else last
            break
        meta = pick_next()
        if meta is None:
            last = "idle" if not started else last
            break
        result = execute_one(meta)
        last = result
        if result == "started":
            started += 1
            continue
        break                               # lost 等异常：不要空转
    return f"started:{started}" if started else last


def scanner_loop() -> None:
    while not _stop.is_set():
        config = load_config()
        manual_only = not config.get("auto_scan", True)
        interval = max(10, as_int(config.get("scan_interval_sec"), 60))
        limit = concurrency_limit(config)

        if manual_only:
            # 轮询关闭：只有人点「立即执行一轮」或新建/打回卡片时才动一次
            woke = _scan_wake.wait(3)
            _scan_wake.clear()
            if not woke:
                _state["last_scan_action"] = "paused"
                continue

        try:
            action = scan_once(limit)
            _state["last_scan_at"] = now_iso()
            _state["last_scan_action"] = action
        except Exception as exc:
            print(f"[scan] 扫描异常: {exc}")
            _state["last_scan_action"] = f"error: {exc}"

        if manual_only:
            continue

        _scan_wake.wait(interval)
        _scan_wake.clear()


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def esc(text) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


IMG_ICON = (
    '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">'
    '<path fill="currentColor" d="M2 3.5A1.5 1.5 0 0 1 3.5 2h9A1.5 1.5 0 0 1 14 3.5v9A1.5 1.5 0 0 1 '
    '12.5 14h-9A1.5 1.5 0 0 1 2 12.5v-9Zm1.5-.5a.5.5 0 0 0-.5.5v6.2l2.7-2.4a1 1 0 0 1 1.33.03L10.6 '
    '10.5l1.5-1.3a1 1 0 0 1 1.4.06V3.5a.5.5 0 0 0-.5-.5h-9Zm1.75 1a1.25 1.25 0 1 0 0 2.5 1.25 1.25 0 0 0 0-2.5Z"/>'
    "</svg>"
)


def image_src(path: str) -> str:
    return "/api/image?path=" + quote(str(path), safe="")


def image_chip_html(path: str) -> str:
    """把 Markdown 图片引用渲染成聊天框那样的「附件标签」。

    悬停出浮层预览、点击看大图，都由前端统一处理；这里只输出标签本体。
    图片文件不存在时标成失效态，避免点了没反应还不知道为什么。
    """
    path = str(path or "").strip()
    name = path.replace("\\", "/").rsplit("/", 1)[-1] or "图片"
    exists = False
    try:
        target = resolve_image_ref(path).resolve()
        exists = target.is_file() and under_assets_dir(target)
    except OSError:
        exists = False

    if not exists:
        return (
            f'<span class="imgtag missing" title="图片文件不存在">{IMG_ICON}'
            f'<span class="nm">{esc(name)}</span><span class="badge">已失效</span></span>'
        )
    return (
        f'<span class="imgtag" role="button" tabindex="0" title="点击查看大图"'
        f' data-img="{esc(image_src(target))}" data-name="{esc(name)}">'
        f"{IMG_ICON}<span class=\"nm\">{esc(name)}</span></span>"
    )


def render_rich(text) -> str:
    """正文渲染：普通文本原样转义，Markdown 图片引用换成附件标签。"""
    raw = str(text or "")
    if not raw:
        return ""
    out = []
    pos = 0
    for match in IMG_RE.finditer(raw):
        out.append(esc(raw[pos:match.start()]))
        out.append(image_chip_html(match.group(1)))
        pos = match.end()
    out.append(esc(raw[pos:]))
    return "".join(out)


def render_card(card: dict) -> str:
    sections = card["_sections"]
    card_id = card["id"]
    dirname = card["_dir"]
    need = sections.get("需求", "").strip()
    comments = sections.get("处理意见", "").strip()
    result = sections.get("执行结果", "").strip()
    comment_round = as_int(card.get("comment_round"))
    review_round = as_int(card.get("review_round"))
    outcome = str(card.get("outcome") or "").strip().lower()
    model = normalize_model(card.get("model"))

    out = ['<article class="card">']
    out.append('<div class="row1">')
    out.append(f'<span class="cid">{esc(card_id)}</span>')
    out.append('<span class="row1r">')
    out.append(f'<span class="pri {PRI_CLASS.get(card["priority"], "pri-mid")}">{esc(card["priority"])}</span>')
    if model:
        out.append(f'<span class="mdl" title="本卡指定模型（其他卡用默认模型）">{esc(model)}</span>')
    out.append("</span>")
    out.append("</div>")
    out.append(f"<h3>{esc(card['title'])}</h3>")

    if need:
        out.append(f'<details><summary>需求</summary><div class="rich">{render_rich(need)}</div></details>')
    if comments:
        out.append(
            f'<details><summary>处理意见 · 共 {comment_round} 轮</summary>'
            f'<div class="rich">{render_rich(comments)}</div></details>'
        )
    if result:
        out.append(
            f'<details><summary>执行结果 · 已处理 {review_round} 轮</summary>'
            f'<div class="rich">{render_rich(result)}</div></details>'
        )

    # 有历史转录的卡片都给一个入口，方便事后看 AI 到底做了什么
    if newest_run_log(card_id):
        out.append(
            f'<div class="acts"><button class="ghost" '
            f'onclick="openRunLog(\'{esc(card_id)}\')">AI 执行日志</button></div>'
        )

    if dirname == DIR_CONFIRM:
        # 人工闸门：不进队列、不自动执行，人点「开始」才搬到「待开始」
        out.append('<div class="flag">等待确认 · 调度器不会拾取</div>')
        out.append('<div class="acts">')
        out.append(f'<button class="primary" onclick="startCard(\'{esc(card_id)}\')">开始</button>')
        out.append(f'<button onclick="openEditDialog(\'{esc(card_id)}\')">编辑</button>')
        out.append("</div>")
    elif dirname == DIR_REVIEW:
        out.append(
            f'<textarea id="c-{esc(card_id)}"'
            f' placeholder="填写处理意见后可打回；满意就直接通过。可直接粘贴或拖入截图"'
            f' onpaste="onImagePaste(event, this, \'{esc(card_id)}\')"'
            f' ondragover="allowDrop(event)"'
            f' ondrop="onImageDrop(event, this, \'{esc(card_id)}\')"></textarea>'
        )
        out.append('<div class="acts">')
        out.append(f'<button class="primary" onclick="approve(\'{esc(card_id)}\')">审核通过</button>')
        out.append(f'<button class="danger" onclick="reject(\'{esc(card_id)}\')">打回重做</button>')
        out.append("</div>")
    elif dirname == DIR_RUNNING:
        note = "已打回，等待返工" if comment_round > review_round else "Agent 处理中"
        out.append(f'<div class="flag">{esc(note)}</div>')
        out.append(
            '<div class="livebox">'
            '<div class="lvhead"><span class="lvdot"></span><span>AI 实时反馈</span>'
            f'<span class="lvmeta" id="lvm-{esc(card_id)}">连接中…</span></div>'
            f'<pre class="live" id="live-{esc(card_id)}"></pre>'
            "</div>"
        )
    elif dirname == DIR_PENDING:
        note = "返工排队中" if comment_round > review_round else "排队等待领取"
        out.append(f'<div class="flag">{esc(note)}</div>')
    elif dirname == DIR_DONE:
        out.append('<div class="flag done">已完成归档</div>')
    elif dirname == DIR_BLOCKED:
        label = "Agent 报告受阻" if outcome == "blocked" else "执行失败 / 被中断"
        out.append(f'<div class="flag danger">{esc(label)}</div>')
        out.append('<div class="acts">')
        out.append(f'<button onclick="requeue(\'{esc(card_id)}\')">退回待开始</button>')
        out.append("</div>")

    out.append("</article>")
    return "".join(out)


def render_columns(cards: list) -> str:
    out = []
    for dirname in STATE_DIRS:
        items = [c for c in cards if c["_dir"] == dirname]
        inner = "".join(render_card(c) for c in items) or '<div class="empty">暂无卡片</div>'
        out.append(
            f'<section class="col" data-dir="{esc(dirname)}">'
            f'<h2><span>{esc(dirname)}</span><span class="n">{len(items)}</span></h2>'
            f"{inner}</section>"
        )
    return "".join(out)


def columns_payload() -> dict:
    """看板列的 HTML + 签名，供前端做增量刷新。

    签名只覆盖影响渲染的字段；不变就不重绘，避免整页 reload 吞掉用户正在输入的内容。
    """
    cards = load_cards()
    signature = "|".join(
        f"{c['id']}@{c['_dir']}:{c['comment_round']}/{c['review_round']}:{c.get('updated_at') or ''}"
        for c in cards
    )
    return {"sig": signature, "html": render_columns(cards)}


def newest_run_log(card_id: str) -> str:
    """找某张卡最近一次的转录文件（用于事后查看）。"""
    if not ID_RE.match(card_id or "") or not RUNS_DIR.is_dir():
        return ""
    matches = sorted(RUNS_DIR.glob(f"*-{card_id}.log"))
    return str(matches[-1]) if matches else ""


def live_payload(offset: int, card_id: str = "") -> dict:
    """实时反馈：返回转录的新增片段。

    offset 由前端持有（按字符计），服务端只回传增量，避免整段重发。
    并发执行时每张卡各有一条转录，前端按 card 分别拉取。
    """
    tasks = active_tasks()
    with _state_lock:
        fallback_card = str(_state.get("current_card") or "")

    if card_id:
        target = card_id
    elif tasks:
        target = sorted(tasks, key=lambda c: str(tasks[c].get("started_at") or ""))[0]
    else:
        target = fallback_card
    if not target:
        return {
            "card": "", "running": False, "size": 0, "offset": 0,
            "text": "", "reset": False, "started_at": "", "log": "",
        }

    info = tasks.get(target) or {}
    if info.get("log"):
        path: object = str(info["log"])
        is_live = True
        started_at = str(info.get("started_at") or "")
    else:
        path = newest_run_log(target)
        is_live = False
        started_at = ""

    full = read_log_text(path)
    size = len(full)
    start = max(0, offset)
    reset = False
    if start > size or size - start > LIVE_TAIL_CHARS:
        start = max(0, size - LIVE_TAIL_CHARS)
        reset = True

    return {
        "card": target,
        "running": is_live,
        "size": size,
        "offset": size,
        "text": full[start:],
        "reset": reset,
        "started_at": started_at,
        "log": Path(path).name if path else "",
    }


def validate_project_name(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        raise ValueError("项目名称不能为空")
    if len(name) > 64:
        raise ValueError("项目名称过长（最多 64 个字符）")
    if re.search(r'[\\/:*?"<>|]', name):
        raise ValueError('项目名称不能包含 \\/:*?"<>| 等字符')
    return name


def _normkey(path) -> str:
    """路径比较用的归一化键：Windows 下大小写 / 分隔符不敏感。"""
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = str(path)
    return os.path.normcase(resolved)


def pick_folder_dialog() -> str:
    """弹 Windows 原生文件夹选择框；返回所选路径，取消返回空串。

    浏览器拿不到本地绝对路径，所以由服务端代弹——看板本来就是本机工具。
    PowerShell 必须 -STA：FolderBrowserDialog 在 MTA 线程里弹不出来。
    """
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '选择项目根目录';"
        "$d.ShowNewFolderButton = $true;"
        "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){$d.SelectedPath}"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as exc:
        print(f"[warn] 文件夹选择器启动失败: {exc}")
        return None
    return (proc.stdout or "").strip()


def api_projects_payload() -> dict:
    data = load_projects()
    active = active_project()
    items = []
    for name, proj in data["projects"].items():
        tdir = Path(str(proj.get("tasks_dir") or ""))
        cards = 0
        if str(proj.get("tasks_dir") or "") and tdir.is_dir():
            cards = sum(len(list((tdir / d).glob("*.md"))) for d in STATE_DIRS)
        items.append({
            "name": name,
            "root": proj.get("root") or "",
            "tasks_dir": proj.get("tasks_dir") or "",
            "cards": cards,
        })
    return {"active": active.get("name", ""), "projects": items}


def create_project(data: dict) -> dict:
    try:
        name = validate_project_name(data.get("name"))
    except ValueError as exc:
        return {"error": str(exc)}

    root_raw = str(data.get("root") or "").strip().strip('"')
    root = Path(root_raw) if root_raw else None
    if root is None or not root.is_dir():
        return {"error": f"项目根目录无效：{root_raw or '(空)'}，请重新选择"}

    loc = str(data.get("task_loc") or "in-project")
    if loc == "central":
        tasks_dir = CENTRAL_PROJECTS_DIR / name / "task"
    else:
        tasks_dir = root / "task"
    assets_dir = tasks_dir / "assets"

    projects = load_projects()
    # 重名：Windows 目录本来不区分大小写，这里保持一致
    lowered = {k.lower(): k for k in projects["projects"]}
    if name.lower() in lowered:
        return {"error": f"项目「{lowered[name.lower()]}」已存在，请换一个名字"}

    # task 目录撞车：被别的项目占用，或已存在且装了别的东西
    occupied = next(
        (n for n, p in projects["projects"].items()
         if p.get("tasks_dir") and _normkey(p["tasks_dir"]) == _normkey(tasks_dir)),
        None,
    )
    if occupied:
        return {"error": f"task 目录已被项目「{occupied}」占用：{tasks_dir}"}
    if tasks_dir.exists():
        leftovers = [
            p.name for p in tasks_dir.iterdir()
            if p.name not in STATE_DIRS and p.name not in ("assets", ".gitkeep")
        ]
        if leftovers:
            return {
                "error": "task 目录已存在且不为空（含 "
                + "、".join(leftovers[:3])
                + "），请换一个位置或先手动清理"
            }

    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        for d in STATE_DIRS:
            (tasks_dir / d).mkdir(exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": f"目录创建失败：{exc}"}

    projects["projects"][name] = {
        "root": str(root),
        "tasks_dir": str(tasks_dir),
        "assets_dir": str(assets_dir),
    }
    projects["active"] = name
    save_projects(projects)
    config = _read_config_raw()
    config["project_root"] = str(root)
    config["tasks_dir"] = str(tasks_dir)
    config["assets_dir"] = str(assets_dir)
    save_config(config)
    print(f"[project] 新建项目「{name}」 root={root} tasks={tasks_dir}")
    return {"ok": True, "name": name, "tasks_dir": str(tasks_dir)}


def render_page() -> str:
    config = load_config()
    models = config.get("available_models") or []
    selected = str(config.get("model") or "")
    options = []
    for name in models:
        mark = " selected" if name == selected else ""
        options.append(f'<option value="{esc(name)}"{mark}>{esc(name)}</option>')
    if not options:
        options.append('<option value="">(默认模型)</option>')

    payload = columns_payload()
    limit = concurrency_limit(config)

    # 卡片上的模型下拉：多一个「默认模型」选项，选中就用全局配置
    default_label = selected or "未指定"
    card_model_options = [f'<option value="">默认模型（{esc(default_label)}）</option>']
    for name in models:
        card_model_options.append(f'<option value="{esc(name)}">{esc(name)}</option>')
    conc_options = "".join(
        f'<option value="{n}"{" selected" if n == limit else ""}>{n} 个</option>'
        for n in range(1, MAX_CONCURRENT_LIMIT + 1)
    )
    proj = api_projects_payload()
    proj_options = []
    for item in proj["projects"]:
        mark = " selected" if item["name"] == proj["active"] else ""
        label = f'{item["name"]}（{item["cards"]} 张）'
        proj_options.append(f'<option value="{esc(item["name"])}"{mark}>{esc(label)}</option>')
    proj_options.append('<option value="__new__">＋ 新建项目…</option>')
    return (
        HTML_TEMPLATE
        .replace("__TASKS_PATH__", esc(str(tasks_dir())))
        .replace("__ACTIVE_PROJ__", esc(proj["active"]))
        .replace("__COLUMNS__", payload["html"])
        .replace("__COL_SIG__", esc(payload["sig"]))
        .replace("__PRI_OPTIONS__", "".join(f'<option value="{esc(p)}">{esc(p)}</option>' for p in PRIORITIES))
        .replace("__MODEL_OPTIONS__", "".join(options))
        .replace("__PROJ_OPTIONS__", "".join(proj_options))
        .replace("__CARD_MODEL_OPTIONS__", "".join(card_model_options))
        .replace("__CONC_OPTIONS__", conc_options)
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "KanbanBoard/2.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _read_json(self) -> dict:
        length = as_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send(200, render_page(), "text/html; charset=utf-8")
        elif route == "/api/cards":
            cards = [{k: v for k, v in c.items() if not k.startswith("_")} for c in load_cards()]
            self._json({"cards": cards})
        elif route == "/api/run-status":
            self._json(self.api_status())
        elif route == "/api/columns":
            self._json(columns_payload())
        elif route == "/api/card":
            self.serve_card_payload()
        elif route == "/api/live":
            query = parse_qs(urlparse(self.path).query)
            self._json(
                live_payload(
                    as_int((query.get("offset") or ["0"])[0], 0),
                    (query.get("card") or [""])[0].strip(),
                )
            )
        elif route == "/api/projects":
            self._json(api_projects_payload())
        elif route == "/api/image":
            self.serve_image()
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        route = urlparse(self.path).path
        data = self._read_json()
        handlers = {
            "/api/new": self.api_new,
            "/api/start": self.api_start,
            "/api/update": self.api_update,
            "/api/reject": self.api_reject,
            "/api/approve": self.api_approve,
            "/api/requeue": self.api_requeue,
            "/api/upload-image": self.api_upload_image,
            "/api/run": self.api_run,
            "/api/scanner": self.api_scanner,
            "/api/concurrency": self.api_concurrency,
            "/api/model": self.api_model,
            "/api/projects/create": self.api_projects_create,
            "/api/projects/switch": self.api_projects_switch,
            "/api/projects/pick-folder": self.api_projects_pick_folder,
        }
        handler = handlers.get(route)
        if handler is None:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            self._json(handler(data))
        except Exception as exc:
            print(f"[error] {route}: {exc}")
            self._json({"error": "服务端处理失败，详见控制台日志"}, 500)

    # ---- 卡片操作 ----

    def api_new(self, data: dict) -> dict:
        title = str(data.get("title") or "").strip()
        if not title:
            return {"error": "标题不能为空"}
        priority = str(data.get("priority") or "中").strip()
        if priority not in PRIORITIES:
            priority = "中"
        need = str(data.get("need") or "").strip()
        # 可选：单卡指定模型，留空表示跟随全局默认
        model = normalize_model(data.get("model"))

        ensure_dirs()
        card_id = next_id()

        # 新建时粘贴的截图先落在草稿区，这里随卡片 ID 归位
        draft = str(data.get("draft") or "").strip()
        if draft:
            need = commit_draft_assets(draft, card_id, need)

        # mode=start → 直接进「待开始」排队；mode=draft → 落进「待确认」，等人点开始
        mode = str(data.get("mode") or "start").strip().lower()
        target_dir = DIR_CONFIRM if mode == "draft" else DIR_PENDING

        meta = {
            "id": card_id,
            "title": title,
            "priority": priority,
            "comment_round": "0",
            "review_round": "0",
            "claimed_at": "",
            "updated_at": now_iso(),
            "outcome": "",
        }
        if model:
            meta["model"] = model
        write_card(
            tasks_dir() / target_dir / f"{card_id}.md",
            meta,
            {"需求": need, "处理意见": "", "执行结果": ""},
        )
        # 只有真正入队才唤醒调度器；存草稿不该把 Agent 叫起来
        if target_dir == DIR_PENDING:
            wake_scanner()
        return {"ok": True, "id": card_id, "dir": target_dir}

    def api_start(self, data: dict) -> dict:
        """「待确认」→「待开始」：人工闸门，移动由本服务完成，之后照常排队等待领取。"""
        path = find_card(str(data.get("id") or ""))
        if path is None:
            return {"error": "卡片不存在"}
        if path.parent.name != DIR_CONFIRM:
            return {"error": "只有「待确认」的卡片可以开始"}
        card = parse_card(path)
        card["updated_at"] = now_iso()
        write_card(path, card, card["_sections"])
        if move_card(path, DIR_PENDING) is None:
            return {"error": "卡片无法移入「待开始」，请检查文件是否被占用"}
        wake_scanner()
        return {"ok": True, "dir": DIR_PENDING}

    def api_update(self, data: dict) -> dict:
        """改卡片内容。只放行「待确认」，避免改到正在跑或已完成的卡。"""
        path = find_card(str(data.get("id") or ""))
        if path is None:
            return {"error": "卡片不存在"}
        if path.parent.name != DIR_CONFIRM:
            return {"error": "只有「待确认」的卡片可以编辑"}
        title = str(data.get("title") or "").strip()
        if not title:
            return {"error": "标题不能为空"}
        priority = str(data.get("priority") or "中").strip()
        if priority not in PRIORITIES:
            priority = "中"

        card = parse_card(path)
        sections = card["_sections"]
        sections["需求"] = str(data.get("need") or "").strip()
        card["title"] = title
        card["priority"] = priority
        # 留空 = 跟随全局默认模型，此时把字段整个去掉而不是写空串
        model = normalize_model(data.get("model"))
        if model:
            card["model"] = model
        else:
            card.pop("model", None)
        card["updated_at"] = now_iso()
        write_card(path, card, sections)
        return {"ok": True, "id": card["id"]}

    def api_reject(self, data: dict) -> dict:
        path = find_card(str(data.get("id") or ""))
        if path is None:
            return {"error": "卡片不存在"}
        comment = str(data.get("comment") or "").strip()
        if not comment:
            return {"error": "打回必须填写处理意见，否则 Agent 不知道要改什么"}

        card = parse_card(path)
        sections = card["_sections"]
        comment_round = as_int(card.get("comment_round")) + 1
        previous = sections.get("处理意见", "").strip()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        block = f"[第 {comment_round} 轮 · {stamp}]\n{comment}"
        sections["处理意见"] = f"{previous}\n\n{block}".strip() if previous else block

        card["comment_round"] = str(comment_round)
        card["updated_at"] = now_iso()
        card["outcome"] = ""
        write_card(path, card, sections)

        dst = move_card(path, DIR_PENDING)
        if dst is None:
            return {"error": "卡片无法退回「待开始」，请检查文件是否被占用"}
        wake_scanner()
        return {"ok": True, "comment_round": comment_round, "dir": DIR_PENDING}

    def api_approve(self, data: dict) -> dict:
        path = find_card(str(data.get("id") or ""))
        if path is None:
            return {"error": "卡片不存在"}
        card = parse_card(path)
        card["updated_at"] = now_iso()
        write_card(path, card, card["_sections"])
        if move_card(path, DIR_DONE) is None:
            return {"error": "卡片无法移入「已发布」，请检查文件是否被占用"}
        return {"ok": True, "dir": DIR_DONE}

    def api_requeue(self, data: dict) -> dict:
        """把「需介入」的卡片退回待开始，重新排队。"""
        path = find_card(str(data.get("id") or ""))
        if path is None:
            return {"error": "卡片不存在"}
        card = parse_card(path)
        card["outcome"] = ""
        card["updated_at"] = now_iso()
        # 重试视为全新开始：清空旧的「执行结果」（完整历史在 runtime/.runs/ 日志里）
        card["_sections"]["执行结果"] = ""
        write_card(path, card, card["_sections"])
        if move_card(path, DIR_PENDING) is None:
            return {"error": "卡片无法退回「待开始」，请检查文件是否被占用"}
        wake_scanner()
        return {"ok": True, "dir": DIR_PENDING}

    # ---- 贴图 ----

    def serve_image(self) -> None:
        """只提供 assets 目录内的图片，防止任意文件读取。"""
        raw = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
        if not raw:
            self._send(400, "missing path", "text/plain; charset=utf-8")
            return
        try:
            target = resolve_image_ref(raw).resolve()
        except OSError:
            self._send(400, "bad path", "text/plain; charset=utf-8")
            return

        if not target.is_file() or not under_assets_dir(target):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return

        ctype = IMAGE_MIME.get(target.suffix.lower().lstrip("."))
        if not ctype:
            self._send(415, "unsupported type", "text/plain; charset=utf-8")
            return

        try:
            blob = target.read_bytes()
        except OSError:
            self._send(500, "read failed", "text/plain; charset=utf-8")
            return
        self._send(200, blob, ctype)

    def serve_card_payload(self) -> None:
        """给编辑弹窗用的原始卡片内容（渲染后的 HTML 还原不回 Markdown）。"""
        card_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0].strip()
        path = find_card(card_id)
        if path is None:
            self._json({"error": "卡片不存在"}, 404)
            return
        card = parse_card(path)
        self._json({
            "id": card["id"],
            "title": card.get("title") or "",
            "priority": card.get("priority") or "中",
            "model": normalize_model(card.get("model")),
            "dir": path.parent.name,
            "need": (card["_sections"].get("需求") or "").strip(),
        })

    def api_upload_image(self, data: dict) -> dict:
        raw = str(data.get("data") or "")
        ext = str(data.get("ext") or "png").strip().lower().lstrip(".")
        if ext not in ALLOWED_IMAGE_EXT:
            return {"error": f"不支持的图片格式：{ext}（可用：{', '.join(sorted(ALLOWED_IMAGE_EXT))}）"}
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            blob = base64.b64decode(raw, validate=True)
        except Exception:
            return {"error": "图片数据解析失败"}
        if not blob:
            return {"error": "图片内容为空"}
        if len(blob) > MAX_IMAGE_BYTES:
            return {"error": f"图片超过了 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限"}

        card_id = str(data.get("card_id") or "").strip()
        draft = str(data.get("draft") or "").strip()
        if card_id:
            if not ID_RE.match(card_id):
                return {"error": "卡片 ID 不合法"}
            folder = assets_dir() / card_id
        elif draft:
            if not DRAFT_RE.match(draft):
                return {"error": "草稿标识不合法"}
            folder = draft_dir() / draft
        else:
            return {"error": "缺少 card_id 或 draft"}

        folder.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.{ext}"
        path = folder / name
        path.write_bytes(blob)

        ref = card_image_ref(path)
        print(f"[image] 保存 {path.name}  →  {folder.name}  ({len(blob)} 字节)")
        return {"ok": True, "path": ref, "markdown": f"![]({ref})", "bytes": len(blob)}

    # ---- 项目管理 ----

    def api_projects_pick_folder(self, data: dict) -> dict:
        path = pick_folder_dialog()
        if path is None:
            return {"error": "无法打开文件夹选择器（需要 Windows PowerShell）"}
        if not path:
            return {"canceled": True}
        return {"ok": True, "path": path}

    def api_projects_create(self, data: dict) -> dict:
        return create_project(data)

    def api_projects_switch(self, data: dict) -> dict:
        name = str(data.get("name") or "").strip()
        try:
            set_active_project(name)
        except KeyError as exc:
            return {"error": str(exc.args[0]) if exc.args else f"项目「{name}」不存在"}
        print(f"[project] 切换到「{name}」")
        return {"ok": True, "active": name}

    # ---- 调度器控制 ----

    def api_status(self) -> dict:
        config = load_config()
        with _state_lock:
            state = {k: v for k, v in _state.items() if k != "tasks"}
            tasks = dict(_state.get("tasks") or {})

        running = []
        for cid, info in tasks.items():
            running.append({
                "id": cid,
                "title": str(info.get("title") or ""),
                "model": str(info.get("model") or ""),
                "started_at": str(info.get("started_at") or ""),
                "log": Path(str(info.get("log") or "")).name,
            })
        running.sort(key=lambda t: t["started_at"])

        # 顶部转录框跟随「最近启动、且仍在跑」的那条；都跑完了就留着最后一条
        focus_log = str(state.get("last_log") or "")
        if running:
            newest = max(running, key=lambda t: t["started_at"])
            focus_log = str((tasks.get(newest["id"]) or {}).get("log") or focus_log)

        state["tasks"] = running
        state["running"] = bool(running)
        state["running_count"] = len(running)
        state["max_concurrent"] = concurrency_limit(config)
        state["log_tail"] = tail_log(focus_log)
        state["current_log"] = Path(focus_log).name if focus_log else ""
        state["prompt_ready"] = PROMPT_FILE.exists()
        state["auto_scan"] = bool(config.get("auto_scan", True))
        state["scan_interval_sec"] = as_int(config.get("scan_interval_sec"), 60)
        state["model"] = str(config.get("model") or "")
        state["live_partial"] = bool(config.get("live_partial", True))
        state["cli_ready"] = Path(str(config.get("cli_path") or "")).exists()
        return state

    def api_run(self, data: dict) -> dict:
        """立即唤醒调度器执行一轮（不等下一个扫描周期）。"""
        config = load_config()
        if not PROMPT_FILE.exists():
            return {"error": f"找不到执行 prompt：{PROMPT_FILE}"}
        if not Path(str(config.get("cli_path") or "")).exists():
            return {"error": "找不到 WorkBuddy CLI，请检查 config.json 的 cli_path"}
        limit = concurrency_limit(config)
        active = len(active_tasks())
        if active >= limit:
            return {"error": f"并发已满（{active}/{limit}），等有任务跑完再点，或调高「同时任务数」"}
        _scan_wake.set()
        return {"ok": True, "message": f"已唤醒调度器（{active}/{limit}）"}

    def api_concurrency(self, data: dict) -> dict:
        value = as_int(data.get("value"), DEFAULT_CONFIG["max_concurrent"])
        value = max(1, min(MAX_CONCURRENT_LIMIT, value))
        config = load_config()
        config["max_concurrent"] = value
        save_config(config)
        # 调大就立刻补位
        _scan_wake.set()
        return {"ok": True, "max_concurrent": value}

    def api_scanner(self, data: dict) -> dict:
        config = load_config()
        enabled = bool(data.get("enabled", True))
        config["auto_scan"] = enabled
        save_config(config)
        if enabled:
            _scan_wake.set()
        return {"ok": True, "auto_scan": enabled}

    def api_model(self, data: dict) -> dict:
        model = str(data.get("model") or "").strip()
        if model and not MODEL_RE.match(model):
            return {"error": "模型名不合法"}
        config = load_config()
        config["model"] = model
        save_config(config)
        return {"ok": True, "model": model}


# 注意：必须是 raw 字符串。模板里的 JS 有 '\n' 这类转义，
# 普通三引号会被 Python 提前吃掉转义符，导致页面脚本整段语法错误、前端全死。
HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>看板任务池</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#ffffff; --line:#e3e5e9; --text:#1f2329; --muted:#6b7280;
    --accent:#2563eb; --done:#15803d; --warn:#b45309; --danger:#dc2626;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#16181c; --panel:#1e2126; --line:#2f333a; --text:#e6e8eb; --muted:#9aa1ab;
           --accent:#60a5fa; --done:#4ade80; --warn:#fbbf24; --danger:#f87171; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:15px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  header.top{padding:14px 20px;display:flex;align-items:center;gap:10px;
             border-bottom:1px solid var(--line);flex-wrap:wrap}
  header.top h1{font-size:17px;font-weight:600;margin:0}
  header.top .path{color:var(--muted);font-size:13px;font-family:ui-monospace,Consolas,monospace}
  .spacer{flex:1}
  button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);
         background:var(--panel);color:var(--text);padding:6px 13px;font-size:14px}
  button:hover{border-color:var(--muted)}
  button:disabled{opacity:.5;cursor:not-allowed}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.danger{border-color:var(--danger);color:var(--danger);background:transparent}
  button.on{border-color:var(--done);color:var(--done)}
  select{font:inherit;font-size:14px;padding:6px 9px;border-radius:8px;
         border:1px solid var(--line);background:var(--panel);color:var(--text)}
  .statusbar{padding:8px 20px;border-bottom:1px solid var(--line);font-size:14px;
             color:var(--muted);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .statusbar .dot{width:7px;height:7px;border-radius:50%;background:var(--muted);
                  display:inline-block;flex:none}
  .statusbar.running .dot{background:var(--warn);animation:pulse 1.2s ease-in-out infinite}
  @keyframes pulse{50%{opacity:.2}}
  .sep{color:var(--line)}
  .logwrap{padding:0 20px 10px}
  .logwrap summary{font-size:13px;color:var(--muted);cursor:pointer;padding:3px 0}
  .logwrap pre{max-height:210px;overflow:auto;background:var(--panel);border:1px solid var(--line);
               border-radius:8px;padding:10px;font-size:13px;white-space:pre-wrap;
               word-break:break-word;color:var(--muted)}
  .board{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;
         padding:16px 20px;align-items:start}
  @media (max-width:1700px){ .board{grid-template-columns:repeat(3,minmax(0,1fr))} }
  @media (max-width:980px){ .board{grid-template-columns:repeat(2,minmax(0,1fr))} }
  @media (max-width:700px){ .board{grid-template-columns:1fr} }
  .col{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
  .col>h2{font-size:14px;font-weight:600;margin:0 0 10px;display:flex;
          justify-content:space-between;align-items:center;color:var(--muted)}
  .col>h2 .n{font-weight:400}
  .card{border:1px solid var(--line);border-radius:10px;padding:12px;
        margin-bottom:10px;background:var(--bg)}
  .row1{display:flex;justify-content:space-between;align-items:center;gap:6px}
  .cid{font-family:ui-monospace,Consolas,monospace;font-size:13px;color:var(--muted)}
  .pri{font-size:13px;padding:1px 9px;border-radius:999px;border:1px solid currentColor}
  .pri-high{color:var(--danger)}
  .pri-mid{color:var(--warn)}
  .pri-low{color:var(--muted)}
  /* 单卡指定模型时才挂这个标签，一眼看出哪些卡没走默认模型 */
  .row1r{display:inline-flex;align-items:center;gap:6px}
  .mdl{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--accent);
       padding:1px 7px;border-radius:999px;border:1px solid var(--line);background:var(--panel);
       max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .card h3{font-size:15px;font-weight:600;margin:6px 0 4px;line-height:1.4}
  .card details{margin:4px 0}
  .card summary{font-size:13px;color:var(--muted);cursor:pointer}
  .card pre{white-space:pre-wrap;word-break:break-word;font:inherit;font-size:13px;
            margin:4px 0;padding:3px 0 3px 8px;border-left:2px solid var(--line)}
  .rich{white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.75;
        margin:4px 0;padding:3px 0 3px 8px;border-left:2px solid var(--line)}
  /* 图片附件标签：和聊天框里的附件一样，悬停看浮层、点击看大图 */
  .imgtag{display:inline-flex;align-items:center;gap:5px;vertical-align:-3px;
          max-width:100%;margin:2px 2px;padding:2px 9px 2px 7px;border-radius:999px;
          border:1px solid var(--line);background:var(--panel);color:var(--text);
          font-size:13px;line-height:1.7;cursor:pointer;user-select:none;
          white-space:nowrap;overflow:hidden}
  .imgtag:hover{border-color:var(--accent);color:var(--accent)}
  .imgtag svg{flex:none;opacity:.75}
  .imgtag .nm{max-width:190px;overflow:hidden;text-overflow:ellipsis;
              font-family:ui-monospace,Consolas,monospace}
  .imgtag .badge{margin-left:2px;padding:0 5px;border-radius:999px;font-size:11px;
                 background:var(--line);color:var(--muted)}
  .imgtag.missing{cursor:not-allowed;opacity:.6}
  .imgtag.missing:hover{border-color:var(--line);color:var(--text)}
  #imgpeek{position:fixed;z-index:60;display:none;padding:6px;border-radius:10px;
           border:1px solid var(--line);background:var(--panel);
           box-shadow:0 12px 32px rgba(0,0,0,.28)}
  #imgpeek.show{display:block}
  #imgpeek img{display:block;width:288px;max-height:216px;object-fit:contain;
               border-radius:6px;background:var(--bg)}
  #dlg-img{width:min(1080px,94vw);padding:14px}
  #dlg-img .imgbar{display:flex;align-items:center;gap:8px;font-size:14px;
                   color:var(--muted);margin-bottom:10px}
  #dlg-img .imgbar .nm{font-family:ui-monospace,Consolas,monospace;color:var(--text);
                       overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #dlg-img .imgbody{display:flex;align-items:center;justify-content:center;
                    background:var(--bg);border:1px solid var(--line);border-radius:10px;
                    padding:8px;max-height:76vh;overflow:auto}
  #dlg-img .imgbody img{max-width:100%;max-height:74vh;object-fit:contain;border-radius:6px}
  .flag{font-size:13px;color:var(--warn);margin-top:6px}
  .hint{font-size:13px;color:var(--muted);margin-top:10px;line-height:1.65}
  .flag.done{color:var(--done)}
  .flag.danger{color:var(--danger)}
  textarea{width:100%;min-height:64px;font:inherit;font-size:14px;padding:8px;
           border-radius:8px;border:1px solid var(--line);background:var(--panel);
           color:var(--text);resize:vertical;margin-top:6px}
  .acts{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
  .acts button{font-size:14px;padding:5px 12px}
  button.ghost{font-size:13px;padding:4px 10px;color:var(--muted)}
  .hlabel{font-size:13px;color:var(--muted)}
  .livebox{margin-top:7px;border:1px solid var(--line);border-radius:8px;
           background:var(--panel);overflow:hidden}
  .lvhead{display:flex;align-items:center;gap:6px;padding:6px 9px;font-size:13px;
          color:var(--muted);border-bottom:1px solid var(--line);background:var(--bg)}
  .lvdot{width:7px;height:7px;border-radius:50%;background:var(--warn);
         animation:pulse 1.2s ease-in-out infinite;flex:none}
  .lvhead.off .lvdot{background:var(--done);animation:none}
  .lvmeta{margin-left:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  pre.live{margin:0;max-height:300px;overflow:auto;padding:9px;font-size:13px;line-height:1.6;
           white-space:pre-wrap;word-break:break-word;color:var(--text);
           font-family:ui-monospace,Consolas,monospace}
  pre.live:empty::before{content:"等待模型输出…";color:var(--muted)}
  .lvfollow{display:flex;align-items:center;gap:5px;padding:4px 8px;border-top:1px solid var(--line);
            font-size:13px;color:var(--muted)}
  .empty{color:var(--muted);font-size:13px;padding:4px 0}
  dialog{border:1px solid var(--line);border-radius:12px;background:var(--panel);
         color:var(--text);padding:20px;width:min(620px,92vw)}
  dialog::backdrop{background:rgba(0,0,0,.45)}
  dialog label{display:block;margin:12px 0 4px;font-size:14px;color:var(--muted)}
  dialog input,dialog select,dialog textarea{width:100%;font:inherit;font-size:14px;padding:9px;
         border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text)}
  dialog input[readonly]{color:var(--muted);font-family:ui-monospace,Consolas,monospace;font-size:13px}
  dialog label.radio{display:flex;align-items:center;gap:8px;margin:6px 0;color:var(--text);cursor:pointer}
  dialog label.radio input[type=radio]{width:auto;padding:0}
</style>
</head>
<body>
<header class="top">
  <h1>看板任务池</h1>
  <span class="path">__TASKS_PATH__</span>
  <span class="spacer"></span>
  <select id="f-proj" title="当前项目：卡片、贴图、Agent 读写范围都跟随切换"
          data-active="__ACTIVE_PROJ__" onchange="onProjectChange(this.value)">__PROJ_OPTIONS__</select>
  <button id="btn-scan" onclick="toggleScan()">轮询：--</button>
  <button id="btn-refresh" onclick="refreshBoard(true)">刷新看板</button>
  <select id="f-model" onchange="saveModel(this.value)">__MODEL_OPTIONS__</select>
  <select id="f-conc" title="同时执行的任务数" onchange="saveConcurrency(this.value)">__CONC_OPTIONS__</select>
  <button class="primary" id="btn-run" onclick="startRun()">立即执行一轮</button>
  <button onclick="openNewCardDialog()">+ 新建卡片</button>
</header>

<div class="statusbar" id="statusbar">
  <span class="dot"></span>
  <span class="txt">状态加载中…</span>
</div>

<div class="logwrap">
  <details id="logdetails" open>
    <summary>最近一次执行转录（可读版，原始事件流见 .runs/*.jsonl）</summary>
    <pre id="logbox">（还没有执行记录）</pre>
  </details>
</div>

<main class="board" data-sig="__COL_SIG__">__COLUMNS__</main>

<dialog id="dlg">
  <h2 style="margin:0;font-size:17px">新建任务卡</h2>
  <label>标题</label>
  <input id="f-title" placeholder="一句话说明要做什么">
  <label>优先级</label>
  <select id="f-pri">__PRI_OPTIONS__</select>
  <label>模型（留空 = 跟随全局默认模型）</label>
  <select id="f-cmodel">__CARD_MODEL_OPTIONS__</select>
  <label>需求（可直接 Ctrl+V 粘贴或拖入截图）</label>
  <textarea id="f-need" placeholder="详细要求，这是 Agent 的唯一执行依据"
            onpaste="onImagePaste(event, this, '')"
            ondragover="allowDrop(event)"
            ondrop="onImageDrop(event, this, '')"></textarea>
  <div class="acts" style="margin-top:16px">
    <button class="primary" onclick="createCard('start')">开始</button>
    <button onclick="createCard('draft')">保存草稿</button>
    <button onclick="document.getElementById('dlg').close()">取消</button>
  </div>
  <div class="hint">「开始」= 直接进「待开始」排队等调度器领取；「保存草稿」= 进「待确认」，永远不会被自动执行，可随时编辑后再点开始。</div>
</dialog>

<dialog id="dlg-edit">
  <h2 style="margin:0;font-size:17px">编辑任务卡 · <span id="e-id"></span></h2>
  <label>标题</label>
  <input id="e-title" placeholder="一句话说明要做什么">
  <label>优先级</label>
  <select id="e-pri">__PRI_OPTIONS__</select>
  <label>模型（留空 = 跟随全局默认模型）</label>
  <select id="e-model">__CARD_MODEL_OPTIONS__</select>
  <label>需求（可直接 Ctrl+V 粘贴或拖入截图）</label>
  <textarea id="e-need" placeholder="详细要求，这是 Agent 的唯一执行依据"
            onpaste="onImagePaste(event, this, editingId)"
            ondragover="allowDrop(event)"
            ondrop="onImageDrop(event, this, editingId)"></textarea>
  <div class="acts" style="margin-top:16px">
    <button class="primary" onclick="saveEdit()">保存</button>
    <button onclick="document.getElementById('dlg-edit').close()">取消</button>
  </div>
</dialog>

<dialog id="dlg-log">
  <h2 style="margin:0;font-size:17px" id="log-title">AI 执行日志</h2>
  <pre id="log-body" class="live" style="max-height:60vh;margin-top:10px">加载中…</pre>
  <div class="acts" style="margin-top:12px">
    <button class="primary" onclick="followLog()">跳到末尾</button>
    <button onclick="document.getElementById('dlg-log').close()">关闭</button>
  </div>
</dialog>

<dialog id="dlg-img">
  <div class="imgbar">
    <span class="nm" id="img-name">图片</span>
    <span class="spacer" style="flex:1"></span>
    <a id="img-open" href="#" target="_blank" rel="noopener">在新标签打开</a>
    <button onclick="document.getElementById('dlg-img').close()">关闭</button>
  </div>
  <div class="imgbody"><img id="img-big" alt="附图"></div>
</dialog>

<dialog id="dlg-proj">
  <h2 style="margin:0;font-size:17px">新建项目</h2>
  <label>项目名称</label>
  <input id="p-name" placeholder="例如：eshop">
  <label>项目根目录（Agent 的读写范围）</label>
  <input id="p-root" readonly>
  <label>task 目录放在哪</label>
  <label class="radio"><input type="radio" name="p-loc" value="in-project" checked>
    项目目录下（&lt;项目根&gt;/task）</label>
  <label class="radio"><input type="radio" name="p-loc" value="central">
    统一放看板下（KanbanForWorkbuddy/projects/&lt;项目名&gt;/task）</label>
  <div class="acts" style="margin-top:16px">
    <button onclick="cancelCreateProject()">取消</button>
    <button class="primary" id="btn-proj-create" onclick="confirmCreateProject()">创建并切换</button>
  </div>
</dialog>

<div id="imgpeek"><img alt="预览"></div>

<script>
async function post(path, body){
  try{
    const res = await fetch(path, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body || {})
    });
    const data = await res.json().catch(function(){ return {}; });
    if(!res.ok || data.error){ alert(data.error || ('操作失败 HTTP ' + res.status)); return false; }
    // 整页不再 reload，改用增量重绘，避免冲掉用户正在输入的内容
    await refreshBoard(true);
    refreshStatus();
    return true;
  }catch(err){
    alert('请求失败：' + err.message);
    return false;
  }
}
// ---- 贴图：粘贴 / 拖入 → 落盘 → 需求框插入绝对路径 ----

let currentDraft = null;

function openNewCardDialog(){
  currentDraft = 'd' + Date.now();
  document.getElementById('f-title').value = '';
  document.getElementById('f-need').value = '';
  document.getElementById('f-cmodel').value = '';
  document.getElementById('dlg').showModal();
}

function allowDrop(event){ event.preventDefault(); }

function insertAtCursor(el, text){
  if(!el) return;
  const s = (el.selectionStart == null) ? el.value.length : el.selectionStart;
  const e = (el.selectionEnd == null) ? el.value.length : el.selectionEnd;
  el.value = el.value.slice(0, s) + text + el.value.slice(e);
  const pos = s + text.length;
  try{ el.setSelectionRange(pos, pos); }catch(err){}
  el.focus();
}

async function uploadImagesFromEvent(event, el, target){
  const src = event.clipboardData || event.dataTransfer;
  if(!src) return;

  const files = [];
  if(src.files && src.files.length){
    for(const f of src.files) files.push(f);
  }else if(src.items){
    for(const it of src.items){
      if(it.kind === 'file'){
        const f = (it.getAsFile && it.getAsFile());
        if(f) files.push(f);
      }
    }
  }

  const images = files.filter(function(f){
    return f && f.type && f.type.indexOf('image/') === 0;
  });
  if(!images.length) return;
  event.preventDefault();

  for(const f of images){
    const name = f.name || ('clip.' + ((f.type.split('/')[1]) || 'png'));
    const ext = (name.split('.').pop() || 'png').toLowerCase();

    let dataUrl;
    try{
      dataUrl = await new Promise(function(res, rej){
        const r = new FileReader();
        r.onload = function(){ res(r.result); };
        r.onerror = function(){ rej(new Error('读取剪贴板图片失败')); };
        r.readAsDataURL(f);
      });
    }catch(err){
      alert(err.message);
      continue;
    }

    try{
      const res = await fetch('/api/upload-image', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(Object.assign({ data: dataUrl, ext: ext }, target))
      });
      const j = await res.json();
      if(!res.ok || j.error){ alert(j.error || '图片上传失败'); continue; }
      insertAtCursor(el, j.markdown + '\n');
    }catch(err){
      alert('图片上传失败：' + err.message);
    }
  }
}

function imageTarget(cardId){
  return cardId ? { card_id: cardId } : { draft: currentDraft || ('d' + Date.now()) };
}

async function onImagePaste(event, el, cardId){
  await uploadImagesFromEvent(event, el, imageTarget(cardId));
}

async function onImageDrop(event, el, cardId){
  await uploadImagesFromEvent(event, el, imageTarget(cardId));
}

async function createCard(mode){
  const title = document.getElementById('f-title').value.trim();
  if(!title){ alert('请填写标题'); return; }
  const ok = await post('/api/new', {
    title: title,
    priority: document.getElementById('f-pri').value,
    model: document.getElementById('f-cmodel').value,
    need: document.getElementById('f-need').value.trim(),
    draft: currentDraft || '',
    mode: mode || 'start'
  });
  if(ok){
    document.getElementById('dlg').close();
    currentDraft = null;
  }
}
function reject(id){
  const box = document.getElementById('c-' + id);
  if(!box || !box.value.trim()){
    alert('打回必须填写处理意见，否则 Agent 不知道要改什么');
    return;
  }
  post('/api/reject', { id: id, comment: box.value });
}
function approve(id){ post('/api/approve', { id: id }); }
function requeue(id){ post('/api/requeue', { id: id }); }
// 「待确认」→「待开始」：人点开始才入队，移动由 Python 端完成
function startCard(id){ post('/api/start', { id: id }); }

let editingId = null;
async function openEditDialog(id){
  let data;
  try{
    data = await (await fetch('/api/card?id=' + encodeURIComponent(id))).json();
  }catch(err){ alert('读取卡片失败：' + err.message); return; }
  if(data.error){ alert(data.error); return; }
  editingId = id;
  document.getElementById('e-id').textContent = id;
  document.getElementById('e-title').value = data.title || '';
  document.getElementById('e-pri').value = data.priority || '中';
  document.getElementById('e-model').value = data.model || '';
  document.getElementById('e-need').value = data.need || '';
  document.getElementById('dlg-edit').showModal();
}
async function saveEdit(){
  if(!editingId) return;
  const title = document.getElementById('e-title').value.trim();
  if(!title){ alert('请填写标题'); return; }
  const ok = await post('/api/update', {
    id: editingId,
    title: title,
    priority: document.getElementById('e-pri').value,
    model: document.getElementById('e-model').value,
    need: document.getElementById('e-need').value.trim()
  });
  if(ok) document.getElementById('dlg-edit').close();
}
function saveModel(model){ post('/api/model', { model: model }); }
function saveConcurrency(value){ post('/api/concurrency', { value: parseInt(value, 10) || 1 }); }

// ---- 图片标签：悬停浮层预览 + 点击看大图 ----
const imgPeek = { box:null, timer:null };

function imgTagOf(target){
  return (target instanceof Element) ? target.closest('.imgtag') : null;
}

function showPeek(tag){
  if(!imgPeek.box){ imgPeek.box = document.getElementById('imgpeek'); }
  const box = imgPeek.box;
  if(!box) return;
  const url = tag.getAttribute('data-img');
  if(!url) return;
  if(imgPeek.timer){ clearTimeout(imgPeek.timer); imgPeek.timer = null; }

  const img = box.querySelector('img');
  if(img.getAttribute('src') !== url) img.setAttribute('src', url);
  box.classList.add('show');

  const r = tag.getBoundingClientRect();
  const w = box.offsetWidth || 300;
  const h = box.offsetHeight || 228;
  let left = r.left;
  let top = r.bottom + 8;
  if(left + w > window.innerWidth - 10) left = Math.max(10, window.innerWidth - w - 10);
  if(left < 10) left = 10;
  if(top + h > window.innerHeight - 10) top = Math.max(10, r.top - h - 8);
  box.style.left = left + 'px';
  box.style.top = top + 'px';
}

function hidePeekSoon(){
  if(imgPeek.timer) clearTimeout(imgPeek.timer);
  imgPeek.timer = setTimeout(function(){
    if(imgPeek.box) imgPeek.box.classList.remove('show');
  }, 150);
}

function openImage(url, name){
  if(!url) return;
  if(imgPeek.box) imgPeek.box.classList.remove('show');
  const dlg = document.getElementById('dlg-img');
  document.getElementById('img-big').setAttribute('src', url);
  document.getElementById('img-open').setAttribute('href', url);
  document.getElementById('img-name').textContent = name || '图片';
  if(!dlg.open) dlg.showModal();
}

document.addEventListener('mouseover', function(event){
  const tag = imgTagOf(event.target);
  if(tag && !tag.classList.contains('missing')) showPeek(tag);
});
document.addEventListener('mouseout', function(event){
  if(imgTagOf(event.target)) hidePeekSoon();
});
document.addEventListener('click', function(event){
  const tag = imgTagOf(event.target);
  if(!tag || tag.classList.contains('missing')) return;
  event.preventDefault();
  hidePeekSoon();
  openImage(tag.getAttribute('data-img'), tag.getAttribute('data-name'));
});
document.addEventListener('keydown', function(event){
  if(event.key !== 'Enter' && event.key !== ' ') return;
  const tag = imgTagOf(event.target);
  if(!tag || tag.classList.contains('missing')) return;
  event.preventDefault();
  openImage(tag.getAttribute('data-img'), tag.getAttribute('data-name'));
});
// 浮层是 fixed 定位，页面/容器一滚动位置就失效，直接收起
window.addEventListener('scroll', function(){
  if(imgPeek.box) imgPeek.box.classList.remove('show');
}, true);
(function initPeek(){
  const box = document.getElementById('imgpeek');
  if(!box) return;
  imgPeek.box = box;
  box.addEventListener('mouseenter', function(){
    if(imgPeek.timer){ clearTimeout(imgPeek.timer); imgPeek.timer = null; }
  });
  box.addEventListener('mouseleave', function(){ box.classList.remove('show'); });
})();

let runningNow = false;
function setScanButton(on, interval){
  const btn = document.getElementById('btn-scan');
  btn.textContent = '轮询：' + (on ? '开' : '关') + (on ? '（' + interval + 's）' : '');
  btn.className = on ? 'on' : '';
}
async function toggleScan(){
  await post('/api/scanner', { enabled: !window.__autoScan });
}
async function startRun(){
  const btn = document.getElementById('btn-run');
  btn.disabled = true;
  btn.textContent = '触发中…';
  try{
    const res = await fetch('/api/run', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({})
    });
    const data = await res.json();
    if(!res.ok || data.error){
      alert(data.error || ('触发失败 HTTP ' + res.status));
    }
    runningNow = true;
  }catch(err){
    alert('请求失败：' + err.message);
  }
  btn.disabled = false;
  btn.textContent = '立即执行一轮';
  refreshStatus();
}
async function refreshStatus(){
  let s;
  try{
    s = await (await fetch('/api/run-status')).json();
  }catch(err){
    return;
  }
  window.__autoScan = s.auto_scan;
  setScanButton(s.auto_scan, s.scan_interval_sec);
  syncConcurrency(s.max_concurrent, s.running_count);

  const bar = document.getElementById('statusbar');
  const btn = document.getElementById('btn-run');
  const logbox = document.getElementById('logbox');
  const parts = [];
  const tasks = s.tasks || [];
  const limit = s.max_concurrent || 1;
  const busy = tasks.length > 0;

  if(busy){
    const who = tasks.map(function(t){
      return t.id + '（已跑 ' + elapsedText(t.started_at) + '）';
    }).join('、');
    parts.push('执行中 ' + tasks.length + '/' + limit + '：' + who);
    parts.push('模型 ' + ((tasks[0] && tasks[0].model) || s.last_model || '默认'));
    if(!s.live_partial){ parts.push('逐字流式已关闭'); }
  }else{
    parts.push('空闲');
    if(s.last_finished_at){
      parts.push('上次完成 ' + s.last_finished_at + '（退出码 ' + s.last_exit_code + '）');
    }
    if(s.last_scan_at){
      parts.push('上次扫描 ' + s.last_scan_at.slice(11) + ' · ' + scanText(s.last_scan_action));
    }
  }
  if(!s.prompt_ready){ parts.push('⚠ prompt 缺失'); }
  if(!s.cli_ready){ parts.push('⚠ CLI 未找到'); }

  bar.className = busy ? 'statusbar running' : 'statusbar';
  bar.querySelector('.txt').textContent = parts.join('  ·  ');
  // 有空槽才允许再触发；满并发时按钮只做提示
  btn.disabled = tasks.length >= limit;
  btn.textContent = (tasks.length >= limit) ? ('并发已满 ' + tasks.length + '/' + limit) : '立即执行一轮';

  const box = document.getElementById('logbox');
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
  box.textContent = s.log_tail || '（还没有执行记录）';
  if(stick) box.scrollTop = box.scrollHeight;
  const details = document.getElementById('logdetails');
  if(details && busy) details.open = true;
  if(details && details.querySelector('summary')){
    const head = busy ? ('AI 实时转录 · ' + tasks.length + '/' + limit + ' 执行中 · ') : '最近一次执行转录 · ';
    details.querySelector('summary').textContent = head + (s.current_log || '') +
      '（原始事件流见 .runs/*.jsonl）';
  }

  if(busy !== runningNow){
    runningNow = busy;
    if(!busy){ setTimeout(function(){ refreshBoard(true); }, 900); }
  }
}

function syncConcurrency(limit, runningCount){
  const sel = document.getElementById('f-conc');
  if(!sel) return;
  const value = String(limit || 1);
  if(sel.value !== value) sel.value = value;
  for(const opt of sel.options){
    const n = parseInt(opt.value, 10);
    // 不能把上限降到比正在跑的任务数还低
    opt.disabled = n < (runningCount || 0);
  }
}

let lastSig = null;
function boardElement(){
  return document.querySelector('main.board');
}

async function refreshBoard(force){
  const board = boardElement();
  if(!board) return;
  if(lastSig === null){ lastSig = board.getAttribute('data-sig') || ''; }

  let data;
  try{
    data = await (await fetch('/api/columns')).json();
  }catch(err){
    return;
  }
  if(!force && data.sig === lastSig) return;

  const changed = (lastSig !== data.sig);
  lastSig = data.sig;
  board.setAttribute('data-sig', data.sig);
  if(!changed && !force) return;

  // 重绘前保存用户正在输入的内容、焦点与光标位置，重绘后原样恢复
  const drafts = {};
  board.querySelectorAll('textarea').forEach(function(t){
    if(t.id && t.value) drafts[t.id] = t.value;
  });
  const active = document.activeElement;
  const activeId = (active && active.id) ? active.id : '';
  let caret = 0;
  try{ caret = active ? (active.selectionStart || 0) : 0; }catch(err){ caret = 0; }
  const scroll = window.scrollY;

  board.innerHTML = data.html;
  // 重绘后 live 面板是全新的空节点，重置游标让它重新从头拉一次
  resetLiveCursors();

  Object.keys(drafts).forEach(function(id){
    const el = document.getElementById(id);
    if(el && !el.value) el.value = drafts[id];
  });
  if(activeId){
    const el = document.getElementById(activeId);
    if(el){
      el.focus();
      try{ el.setSelectionRange(caret, caret); }catch(err){}
    }
  }
  window.scrollTo(0, scroll);
}
// ---- AI 实时反馈：按 offset 增量拉取「进行中」卡片的转录（并发时每张卡各一路） ----

const liveOffsets = {};

function elapsedText(startedAt){
  if(!startedAt) return '—';
  const t = new Date(startedAt).getTime();
  if(isNaN(t)) return '—';
  let sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
  if(h) return h + ' 小时 ' + m + ' 分';
  if(m) return m + ' 分 ' + s2 + ' 秒';
  return s2 + ' 秒';
}

function resetLiveCursors(){
  Object.keys(liveOffsets).forEach(function(k){ delete liveOffsets[k]; });
}

async function pollOneLive(pre){
  const card = pre.id.slice(5);
  if(liveOffsets[card] === undefined) liveOffsets[card] = 0;

  let data;
  try{
    data = await (await fetch('/api/live?offset=' + liveOffsets[card] +
                              '&card=' + encodeURIComponent(card))).json();
  }catch(err){
    return;
  }

  if(data.reset){ pre.textContent = ''; liveOffsets[card] = 0; }
  if(data.text){
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
    pre.textContent += data.text;
    if(atBottom) pre.scrollTop = pre.scrollHeight;
  }
  liveOffsets[card] = data.offset || liveOffsets[card];

  const head = pre.parentElement ? pre.parentElement.querySelector('.lvhead') : null;
  if(head){
    head.className = data.running ? 'lvhead' : 'lvhead off';
    const meta = document.getElementById('lvm-' + card);
    if(meta){
      meta.textContent = (data.running ? '进行中 · ' + elapsedText(data.started_at) : '已结束')
                         + ' · ' + (data.size || 0) + ' 字';
    }
  }
}

async function pollLive(){
  const board = boardElement();
  if(!board) return;
  const pres = board.querySelectorAll('pre.live[id^="live-"]');
  if(!pres.length) return;
  for(const pre of pres){
    await pollOneLive(pre);
  }
}

async function openRunLog(id){
  const dlg = document.getElementById('dlg-log');
  const body = document.getElementById('log-body');
  document.getElementById('log-title').textContent = 'AI 执行日志 · ' + id;
  body.textContent = '加载中…';
  dlg.showModal();
  try{
    const data = await (await fetch('/api/live?offset=0&card=' + encodeURIComponent(id))).json();
    body.textContent = data.text || '（没有找到执行日志，可能这张卡还没执行过）';
    body.scrollTop = body.scrollHeight;
  }catch(err){
    body.textContent = '加载失败：' + err.message;
  }
}

function followLog(){
  const body = document.getElementById('log-body');
  body.scrollTop = body.scrollHeight;
}

function scanText(action){
  const map = {
    'idle': '无待处理卡片',
    'busy': '上一轮仍在执行',
    'full': '并发已满',
    'paused': '轮询已关闭',
    'review': '已完成 → 待审核',
    'blocked': '受阻 → 需介入',
    'lost': '卡片已被处理',
    'move_failed': '状态流转失败',
    'error': '异常'
  };
  if(action && action.indexOf('started:') === 0){
    return '已启动 ' + action.slice(8) + ' 张';
  }
  return map[action] || (action || '');
}
// ---- 项目：切换 / 新建 ----

function resetProjectSelect(){
  const sel = document.getElementById('f-proj');
  if(sel) sel.value = sel.getAttribute('data-active') || '';
}

async function onProjectChange(value){
  if(value === '__new__'){
    await createProjectFlow();
    resetProjectSelect();
    return;
  }
  if(!value || value === document.getElementById('f-proj').getAttribute('data-active')) return;
  const ok = await post('/api/projects/switch', { name: value });
  if(ok) location.reload();   // 切项目是全局动作：路径、列、转录整页换新
}

async function createProjectFlow(){
  let data;
  try{
    // 服务端弹原生文件夹选择框，浏览器拿不到本地绝对路径；选目录期间请求会挂起，属正常
    const res = await fetch('/api/projects/pick-folder', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
    });
    data = await res.json().catch(function(){ return {}; });
  }catch(err){ alert('无法打开文件夹选择器：' + err.message); return; }
  if(data.error){ alert(data.error); return; }
  if(data.canceled || !data.path) return;   // 用户取消，静默收回
  document.getElementById('p-root').value = data.path;
  document.getElementById('p-name').value =
    data.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '';
  const inproj = document.querySelector('input[name="p-loc"][value="in-project"]');
  if(inproj) inproj.checked = true;
  document.getElementById('dlg-proj').showModal();
}

function cancelCreateProject(){
  document.getElementById('dlg-proj').close();
  resetProjectSelect();
}

async function confirmCreateProject(){
  const name = document.getElementById('p-name').value.trim();
  const root = document.getElementById('p-root').value.trim();
  const locEl = document.querySelector('input[name="p-loc"]:checked');
  if(!name){ alert('请填写项目名称'); return; }
  const btn = document.getElementById('btn-proj-create');
  btn.disabled = true;
  try{
    const res = await fetch('/api/projects/create', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ name: name, root: root, task_loc: locEl ? locEl.value : 'in-project' })
    });
    const data = await res.json().catch(function(){ return {}; });
    if(!res.ok || data.error){ alert(data.error || ('创建失败 HTTP ' + res.status)); return; }
    document.getElementById('dlg-proj').close();
    location.reload();
  }catch(err){
    alert('请求失败：' + err.message);
  }finally{
    btn.disabled = false;
  }
}

// 注意：这里绝不使用 location.reload()，否则会吞掉用户正在填写的处理意见
setInterval(refreshStatus, 3000);
setInterval(refreshBoard, 4000);
setInterval(pollLive, 1200);
refreshStatus();
pollLive();
</script>
</body>
</html>
"""


def main() -> int:
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"端口参数无效: {sys.argv[1]}")
            return 1

    ensure_dirs()
    load_state()

    moved = migrate_state_dirs()
    if moved:
        print(f"[migrate] 已把 {moved} 张卡片移入新的状态目录（新增「{DIR_CONFIRM}」）")

    migrated = migrate_flat_cards()
    if migrated:
        print(f"[migrate] 已把 {migrated} 张旧结构卡片归位到状态目录")

    orphans = sweep_orphans()
    if orphans:
        print(f"[sweep] 已把 {orphans} 张中断的卡片移入「{DIR_BLOCKED}」")

    config = load_config()
    cards_root = tasks_dir_from(config)
    total = sum(len(list((cards_root / d).glob("*.md"))) for d in STATE_DIRS)

    print(f"看板已启动   : http://127.0.0.1:{port}")
    print(f"卡片目录     : {cards_root}  (共 {total} 张)")
    print(f"贴图目录     : {assets_dir_from(config)}")
    print(f"看板目录     : {ROOT}")
    print(f"项目根目录   : {resolve_project_root(config)}   (Agent 读写范围)")
    print(f"执行 prompt  : {PROMPT_FILE}  {'[就绪]' if PROMPT_FILE.exists() else '[缺失]'}")
    print(f"CLI 路径     : {config.get('cli_path') or '(未配置)'}")
    print(f"node         : {resolve_node(config) or '(未找到)'}")
    print(f"内嵌调度器   : {'开启' if config.get('auto_scan', True) else '关闭'}"
          f" · 间隔 {as_int(config.get('scan_interval_sec'), 60)} 秒"
          f" · 同时任务 {concurrency_limit(config)} 个")
    print("按 Ctrl+C 停止")

    threading.Thread(target=scanner_loop, name="scanner", daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        _stop.set()
        _scan_wake.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
