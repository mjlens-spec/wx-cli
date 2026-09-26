#!/usr/bin/env python3
"""Bounded, local WeChat reads for AI work. No Dashboard or model API dependency."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

VERSION = "0.1.0"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TEXT = 16000
TYPE_NAMES = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji", 49: "app", 10000: "system", 10002: "revoke"}
HINTS = {
    "ACCESS_DENIED": "请允许宿主访问微信本地数据和读取器缓存；这不等于密钥失效。",
    "DATABASE_READ_FAILED": "数据库读取失败；核对当前账号、文件权限和该数据库密钥。",
    "KEY_UNAVAILABLE": "当前读取路径缺少可用密钥；先保留现有配置并定位具体数据库。",
    "MEDIA_NOT_CACHED": "未找到该消息的本地图片缓存；可在微信打开图片并加载后重试。",
    "IMAGE_FORMAT_UNSUPPORTED": "图片已定位，但当前工具无法转换为可查看格式。",
    "ACCOUNT_MISMATCH": "图片引用与当前绑定的微信账号不一致，请重新列出图片。",
    "REFERENCE_NOT_FOUND": "未找到完全一致的图片消息，请刷新图片列表。",
    "OUTPUT_EXISTS": "目标文件已存在，请使用新的文件名。",
    "CHAT_AMBIGUOUS": "会话名称匹配多个结果，请从 sessions 中选择准确的 chat_id。",
    "IMAGE_METADATA_MISSING": "消息中缺少图片定位信息，无法安全关联本地文件。",
    "BACKEND_NOT_INSTALLED": "找不到配置的原生 wx-cli，请核对安装位置。",
    "BACKEND_TIMEOUT": "本次读取超时；可缩小会话或日期范围后重试。",
    "BACKEND_FORMAT_CHANGED": "原生 wx-cli 返回格式不兼容，请核对工具版本。",
    "PRIVATE_FILE_REQUIRED": "配置和临时材料须属于当前用户，文件权限设为 0600。",
    "PRIVATE_DIRECTORY_REQUIRED": "输出目录须属于当前用户，目录权限设为 0700。",
    "IMAGE_TOO_LARGE": "图片超过本工具的 12 MB 上限。",
}


class Failure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def text_safe(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<\?xml.*|<msg(?:\s|>).*", "[图片或消息底层元数据已隐藏]", text)
    text = re.sub(r"(?i)\b(aeskey|authkey|access_token|refresh_token|token|password|passwd|secret|enc_key|data_key|image_aes_key)\b[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'<>;&,}]+)", r"\1=[已隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/=-]+", "Bearer [已隐藏]", text)
    return text[:MAX_TEXT]


def private_read(path, max_bytes=4 * 1024 * 1024):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Failure("PRIVATE_FILE_REQUIRED")
        if info.st_size > max_bytes:
            raise Failure("FILE_TOO_LARGE")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(max_bytes + 1)
    finally:
        os.close(fd)


def private_dir(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise Failure("SYMLINK_REFUSED")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Canonicalize system aliases such as /tmp -> /private/tmp.
    path = path.resolve(strict=True)
    info = path.stat()
    if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Failure("PRIVATE_DIRECTORY_REQUIRED")
    return path


def write_new(path, data):
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Failure("OUTPUT_EXISTS") from None
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
        os.fchmod(fd, 0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def write_json(path, value):
    write_new(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def date_bounds(since=None, until=None):
    today = dt.date.today()
    try:
        first = dt.date.fromisoformat(since) if since else today - dt.timedelta(days=2)
        last = dt.date.fromisoformat(until) if until else today
    except ValueError:
        raise Failure("INVALID_DATE") from None
    if first > last:
        raise Failure("INVALID_DATE_RANGE")
    start = int(dt.datetime.combine(first, dt.time.min).timestamp())
    # Native wx-cli uses an inclusive upper bound.
    end = int(dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min).timestamp()) - 1
    return start, end, {"since": first.isoformat(), "until": last.isoformat(), "timezone": str(dt.datetime.now().astimezone().tzinfo)}


def error_code(stderr):
    value = stderr.lower()
    if any(s in value for s in ["permission denied", "operation not permitted", "readonly database", "unable to open database file"]):
        return "ACCESS_DENIED"
    if "ambiguous" in value or "multiple contacts" in value:
        return "CHAT_AMBIGUOUS"
    if "key" in value and any(s in value for s in ["not found", "no key", "missing", "unavailable", "derivation"]):
        return "KEY_UNAVAILABLE"
    if "decrypt" in value or "database" in value:
        return "DATABASE_READ_FAILED"
    return "BACKEND_FAILED"


def invoke(argv, timeout=60):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, umask=0o077)
    except subprocess.TimeoutExpired:
        raise Failure("BACKEND_TIMEOUT") from None
    except PermissionError:
        raise Failure("ACCESS_DENIED") from None
    except FileNotFoundError:
        raise Failure("BACKEND_NOT_INSTALLED") from None
    if result.returncode:
        # Older native versions print key previews. Never forward either stream.
        raise Failure(error_code(result.stderr))
    return result.stdout


def message_text(row):
    content = row.get("content", {})
    if isinstance(content, dict):
        for kind, data in content.items():
            if kind in ("Text", "System", "Revoke") and isinstance(data, str):
                return text_safe(data)
            if kind == "Quote" and isinstance(data, dict):
                return text_safe((data.get("reply_text") or "") + "\n引用 " + (data.get("refer_sender") or "") + "：" + (data.get("refer_content") or ""))
            if kind in ("Link", "File", "MiniProgram", "AppGeneric", "ChannelVideo") and isinstance(data, dict):
                return text_safe("\n".join(str(data[k]) for k in ("title", "des", "url") if data.get(k)))
    return text_safe(row.get("snippet") or "[" + TYPE_NAMES.get(row.get("msg_type"), "unsupported") + "]")


def image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class Reader:
    def __init__(self, config):
        self.binary = str(Path(config["binary"]).expanduser())
        original = Path(config["account_dir"]).expanduser()
        if original.is_symlink():
            raise Failure("ACCOUNT_PATH_INVALID")
        self.account = original.resolve(strict=True)
        if not (self.account / "db_storage").is_dir():
            raise Failure("ACCOUNT_PATH_INVALID")
        self.account_tag = hashlib.sha256(str(self.account).encode()).hexdigest()[:16]

    def native(self, args, query=True):
        command = [self.binary, *args]
        if query:
            command += ["--data-dir", str(self.account), "--format", "json", "--no-server"]
        try:
            result = invoke(command)
        except Failure as exc:
            if not query and exc.code in {"DATABASE_READ_FAILED", "BACKEND_FAILED"}:
                raise Failure("IMAGE_DECODE_FAILED") from None
            raise
        if not query:
            return result
        try:
            value = json.loads(result)
        except ValueError:
            raise Failure("BACKEND_FORMAT_CHANGED") from None
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise Failure("BACKEND_FORMAT_CHANGED")
        return value

    def envelope(self, kind, items, paging=None, **extra):
        return {"schema_version": 1, "tool": "wechat-work", "version": VERSION, "kind": kind,
                "queried_at": now(), "source": "wx-cli_local_direct", "account_ref": self.account_tag,
                "items": items, "paging": paging or {}, **extra}

    def reference(self, row):
        payload = {"v": 1, "account": self.account_tag, "chat": row["talker"],
                   "server_id": str(row["server_id"]), "sort_seq": str(row["sort_seq"]), "timestamp": row["create_time"]}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

    def unpack(self, token):
        if len(token) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise Failure("REFERENCE_INVALID")
        try:
            payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            if not isinstance(payload["account"], str):
                raise ValueError()
            if payload["v"] != 1 or not isinstance(payload["chat"], str) or not 1 <= len(payload["chat"]) <= 256:
                raise ValueError()
            for key in ("server_id", "sort_seq"):
                if not re.fullmatch(r"\d{1,20}", payload[key]):
                    raise ValueError()
            if not isinstance(payload["timestamp"], int) or payload["timestamp"] <= 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise Failure("REFERENCE_INVALID") from None
        if payload["account"] != self.account_tag:
            raise Failure("ACCOUNT_MISMATCH")
        return payload

    def normalize(self, row):
        ref = self.reference(row)
        timestamp = row["create_time"]
        result = {"evidence_id": "wx_" + hashlib.sha256(ref.encode()).hexdigest()[:20],
                  "chat_id": row["talker"], "message_id": str(row["server_id"]), "sort_seq": str(row["sort_seq"]),
                  "timestamp": timestamp, "time": dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(),
                  "sender": text_safe(row.get("sender_display_name") or row.get("sender")),
                  "direction": row.get("direction"), "type": TYPE_NAMES.get(row["msg_type"], "unsupported"),
                  "text": message_text(row)}
        if row["msg_type"] == 3:
            result["image_ref"] = ref
        return result

    def sessions(self, limit=40, offset=0, include_official=False):
        data = self.native(["sessions", "--limit", str(limit), "--offset", str(offset)])
        items = []
        for row in data["items"]:
            chat = row["username"]
            if not include_official and (chat.startswith("gh_") or chat in {"brandsessionholder", "brandservicesessionholder", "notification_messages", "foldedchats"}):
                continue
            items.append({"chat_id": chat, "name": text_safe(row.get("display_name") or chat), "is_group": chat.endswith("@chatroom"),
                          "timestamp": row.get("sort_timestamp"), "summary": text_safe(row.get("summary"))})
        return self.envelope("sessions", items, data.get("paging"), filtered_out=len(data["items"]) - len(items),
                             next_offset=offset + len(data["items"]))

    def history(self, chat, since=None, until=None, limit=100, offset=0, images=False):
        if not chat or chat.startswith("-"):
            raise Failure("CHAT_INVALID")
        start, end, window = date_bounds(since, until)
        args = ["query", chat, "--since", str(start), "--until", str(end), "--limit", str(limit), "--offset", str(offset), "--order", "desc"]
        if images:
            args += ["--type", "image"]
        data = self.native(args)
        rows = data["items"]
        if len({r["talker"] for r in rows}) > 1:
            raise Failure("CHAT_AMBIGUOUS")
        normalized = sorted((self.normalize(row) for row in rows), key=lambda x: (x["timestamp"], int(x["sort_seq"])))
        return self.envelope("images" if images else "history", normalized, data.get("paging"),
                             window=window, chat_requested=text_safe(chat), next_offset=offset + len(rows),
                             latest_message_at=max((m["time"] for m in normalized), default=None),
                             coverage="local_records_in_requested_window", shard_warning_count=len(data.get("shard_warnings", [])))

    def resolve_image_message(self, token):
        ref = self.unpack(token)
        anchor = ["--around-server-id", ref["server_id"]] if int(ref["server_id"]) else ["--around-sort-seq", ref["sort_seq"]]
        data = self.native(["query", ref["chat"], *anchor, "--context", "0", "--limit", "1"])
        rows = [r for r in data["items"] if r["talker"] == ref["chat"] and str(r["server_id"]) == ref["server_id"]
                and str(r["sort_seq"]) == ref["sort_seq"] and r["create_time"] == ref["timestamp"] and r["msg_type"] == 3]
        if len(rows) != 1:
            raise Failure("REFERENCE_NOT_FOUND")
        digest = rows[0].get("content", {}).get("Image", {}).get("md5")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", digest):
            raise Failure("IMAGE_METADATA_MISSING")
        return rows[0], digest.lower()

    def candidates(self, chat, digest):
        attach = self.account / "msg" / "attach"
        folder = attach / hashlib.md5(chat.encode()).hexdigest()
        if not folder.exists():
            return []
        if folder.is_symlink() or not folder.resolve().is_relative_to(attach.resolve()):
            raise Failure("MEDIA_PATH_INVALID")
        months = sorted((p for p in folder.iterdir() if re.fullmatch(r"\d{4}-\d{2}", p.name) and p.is_dir() and not p.is_symlink()), reverse=True)
        found = []
        for suffix, quality in [("_h", "cached_hd"), ("", "cached_regular"), ("_t", "thumbnail")]:
            for month in months:
                path = month / "Img" / (digest + suffix + ".dat")
                if path.exists() or path.is_symlink():
                    found.append((path, quality))
        return found

    def image(self, token, output_stem):
        row, digest = self.resolve_image_message(token)
        candidates = self.candidates(row["talker"], digest)
        if not candidates:
            raise Failure("MEDIA_NOT_CACHED")
        stem = Path(output_stem).expanduser().absolute()
        parent = private_dir(stem.parent)
        stem = parent / stem.name
        if any(os.path.lexists(str(stem) + "." + ext) for ext in ("png", "jpg", "gif", "webp")):
            raise Failure("OUTPUT_EXISTS")
        failures = []
        for source, quality in candidates:
            try:
                info = source.lstat()
                if source.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not source.resolve().is_relative_to(self.account):
                    raise Failure("MEDIA_PATH_INVALID")
                if info.st_size > MAX_IMAGE_BYTES + 4096:
                    raise Failure("IMAGE_TOO_LARGE")
                with tempfile.TemporaryDirectory(prefix=".decode-", dir=parent) as temporary:
                    raw = Path(temporary) / "decoded"
                    self.native(["media", "decrypt-dat", str(source), "--data-dir", str(self.account), "--output", str(raw)], query=False)
                    if not raw.is_file():
                        raise Failure("IMAGE_DECODE_FAILED")
                    if raw.stat().st_size > MAX_IMAGE_BYTES:
                        raise Failure("IMAGE_TOO_LARGE")
                    data = raw.read_bytes()
                    fmt = image_format(data)
                    if not fmt:
                        raise Failure("IMAGE_FORMAT_UNSUPPORTED")
                    dimensions = invoke(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(raw)])
                    width = re.search(r"pixelWidth: (\d+)", dimensions)
                    height = re.search(r"pixelHeight: (\d+)", dimensions)
                    if not width or not height:
                        raise Failure("IMAGE_DECODE_FAILED")
                    output = Path(str(stem) + "." + fmt)
                    write_new(output, data)
                return {"status": "ready", "evidence_id": self.normalize(row)["evidence_id"], "path": str(output),
                        "format": fmt, "bytes": len(data), "width": int(width[1]), "height": int(height[1]),
                        "quality": quality, "fallback_used": bool(failures), "skipped_candidate_errors": failures}
            except Failure as exc:
                if exc.code in {"OUTPUT_EXISTS", "MEDIA_PATH_INVALID", "ACCESS_DENIED"}:
                    raise
                failures.append(exc.code)
        raise Failure(failures[-1] if failures else "IMAGE_DECODE_FAILED")

    def status(self):
        version = text_safe(invoke([self.binary, "--version"]).strip())
        result = {"tool": "wechat-work", "version": VERSION, "backend": version, "dashboard_required": False,
                  "model_api_used": False, "native_diagnostics_exposed": False, "account_ref": self.account_tag,
                  "messages": "unchecked", "images": "decoder_available_not_tested", "checked_at": now()}
        try:
            self.native(["sessions", "--limit", "1"])
            result["messages"] = "ready"
        except Failure as exc:
            result["messages"] = "blocked"
            result["message_error"] = {"code": exc.code, "hint": HINTS.get(exc.code)}
        return result


def new_job():
    root = private_dir(tempfile.mkdtemp(prefix="wechat-work-job-"))
    write_json(root / ".wechat-work-job.json", {"tool": "wechat-work", "owner_uid": os.getuid(), "created_at": now()})
    return root


def cleanup_job(path):
    candidate = Path(path).expanduser().absolute()
    if candidate.is_symlink():
        raise Failure("CLEANUP_TARGET_INVALID")
    root = candidate.resolve(strict=True)
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("wechat-work-job-"):
        raise Failure("CLEANUP_TARGET_INVALID")
    marker = json.loads(private_read(root / ".wechat-work-job.json"))
    if marker.get("tool") != "wechat-work" or marker.get("owner_uid") != os.getuid():
        raise Failure("CLEANUP_TARGET_INVALID")
    shutil.rmtree(root)
    return {"cleaned": True, "job": str(root)}


def prepare(reader, chats, since, until, limit, image_limit):
    root = new_job()
    context = {"schema_version": 1, "prepared_at": now(), "analysis_complete": False,
               "source": "local_wechat", "conversations": [], "errors": [], "images": []}
    try:
        for chat in chats:
            try:
                context["conversations"].append(reader.history(chat, since, until, limit))
            except Failure as exc:
                context["errors"].append({"chat_requested": text_safe(chat), "code": exc.code})
        if not context["conversations"]:
            raise Failure("NO_CONVERSATION_READ")
        images = sorted((m for c in context["conversations"] for m in c["items"] if m.get("image_ref")), key=lambda m: m["timestamp"], reverse=True)
        for index, message in enumerate(images[:image_limit]):
            try:
                context["images"].append(reader.image(message["image_ref"], root / f"image-{index + 1}"))
            except Failure as exc:
                context["images"].append({"evidence_id": message["evidence_id"], "status": "unavailable", "code": exc.code})
        context["images_not_requested"] = max(0, len(images) - image_limit)
        write_json(root / "context.json", context)
        return {"job": str(root), "context_path": str(root / "context.json"), "conversations": len(context["conversations"]),
                "messages": sum(len(c["items"]) for c in context["conversations"]), "failed_conversations": len(context["errors"]),
                "images_ready": sum(i["status"] == "ready" for i in context["images"]), "analysis_complete": False}
    except BaseException:
        cleanup_job(root)
        raise


def bounded_number(low, high):
    def parse(value):
        result = int(value)
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return result
    return parse


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="独立微信工作读取工具：消息、图片和有出处的分析材料")
    parser.add_argument("--config", default=str(Path(__file__).resolve().with_name("config.json")))
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="实际探测消息读取状态")
    session = commands.add_parser("sessions", help="列出最近会话，默认过滤部分公众号入口")
    session.add_argument("--limit", type=bounded_number(1, 200), default=100)
    session.add_argument("--offset", type=bounded_number(0, 20000), default=0)
    session.add_argument("--include-official", action="store_true")
    for name in ("history", "images", "prepare"):
        p = commands.add_parser(name, help={"history": "读取消息", "images": "列出图片引用", "prepare": "生成供当前 AI 会话理解的材料"}[name])
        p.add_argument("--chat", required=True, action="append" if name == "prepare" else "store")
        p.add_argument("--since")
        p.add_argument("--until")
        p.add_argument("--limit", type=bounded_number(1, 200), default=20 if name == "images" else 100)
        if name == "prepare":
            p.add_argument("--images", type=bounded_number(0, 8), default=0)
        else:
            p.add_argument("--offset", type=bounded_number(0, 20000), default=0)
    image = commands.add_parser("image", help="按精确消息引用提取本地图片")
    image.add_argument("--ref", required=True)
    image.add_argument("--output", required=True, help="输出文件名前缀；目录须为本人私有目录")
    cleanup = commands.add_parser("cleanup", help="只清理本工具生成的临时任务目录")
    cleanup.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "cleanup":
            result = cleanup_job(args.job)
        else:
            reader = Reader(json.loads(private_read(args.config)))
            if args.command == "status":
                result = reader.status()
            elif args.command == "sessions":
                result = reader.sessions(args.limit, args.offset, args.include_official)
            elif args.command in ("history", "images"):
                result = reader.history(args.chat, args.since, args.until, args.limit, args.offset, args.command == "images")
            elif args.command == "image":
                result = reader.image(args.ref, args.output)
            elif args.command == "prepare":
                chats = list(dict.fromkeys(args.chat))
                if len(chats) > 12:
                    raise Failure("MAX_12_CHATS_PER_BATCH")
                result = prepare(reader, chats, args.since, args.until, args.limit, args.images)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Failure as exc:
        print(json.dumps({"ok": False, "code": exc.code, "hint": HINTS.get(exc.code)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code = "ACCESS_DENIED" if isinstance(exc, PermissionError) else "LOCAL_STATE_INVALID"
        print(json.dumps({"ok": False, "code": code, "hint": HINTS.get(code)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
