#!/usr/bin/env python3
"""Install the optional local reader and shared AI skill; never copy native keys."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


def regular(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError("Refused unexpected file ownership/type: " + str(path))


def directory(path, home):
    """Check each managed parent before creating or writing below it."""
    relative = path.relative_to(home)
    current = home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Refused symlink directory: " + str(current))
        current.mkdir(mode=0o700, exist_ok=True)
        if not current.is_dir() or current.stat().st_uid != os.getuid():
            raise ValueError("Refused unexpected directory ownership/type: " + str(current))


def install(source, home, binary=None, account_dir=None, agent="both", replace_wx_image=False):
    os.umask(0o077)
    home = home.resolve()
    runtime = home / ".local/share/wechat-work"
    config_path = runtime / "config.json"
    config = {}
    if config_path.exists() or config_path.is_symlink():
        regular(config_path)
        config = json.loads(config_path.read_text())
    binary = Path(binary or config.get("binary") or home / ".local/bin/wx-cli").expanduser().resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("wx-cli must be an executable file")
    account = account_dir or config.get("account_dir")
    if not account:
        raise ValueError("First installation requires --account-dir pointing to the account directory containing db_storage")
    account = Path(account).expanduser()
    if account.is_symlink():
        raise ValueError("Account directory must not be a symlink")
    account = account.resolve(strict=True)
    if not (account / "db_storage").is_dir():
        raise ValueError("Account directory must contain db_storage")
    config = {"schema_version": 1, "binary": str(binary), "account_dir": str(account)}
    hosts = (".codex", ".claude") if agent == "both" else ("." + agent,)
    # Preflight all destinations before changing any existing entrypoint.
    files = [runtime / name for name in ("config.json", "wechat_work.py", "README.md", "skill/SKILL.md")]
    files += [home / ".local/bin/wechat-work"]
    if ".claude" in hosts:
        files.append(home / ".claude/commands/wechat-work.md")
        if replace_wx_image:
            files.append(home / ".claude/commands/wx-image.md")
    targets = [home / host / "skills/wechat-work" for host in hosts]
    for path in files + targets:
        directory(path.parent, home)
    for path in files:
        if path.exists() or path.is_symlink():
            regular(path)
    for target in targets:
        if (target.exists() or target.is_symlink()) and (
            not target.is_symlink() or target.resolve() != (runtime / "skill").resolve()
        ):
            raise ValueError("Existing unrelated skill left unchanged: " + str(target))
    runtime.chmod(0o700)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = runtime / "backups" / stamp

    def put(path, contents, mode=0o600):
        if path.exists():
            if path.read_bytes() == contents:
                path.chmod(mode)
                return
            backup = backup_root / str(path.relative_to(home)).replace("/", "__")
            directory(backup.parent, home)
            shutil.copy2(path, backup)
            backup.chmod(0o600)
        fd, temporary = tempfile.mkstemp(prefix=".wechat-work-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(contents)
                out.flush()
                os.fchmod(out.fileno(), mode)
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    for name in ("wechat_work.py", "README.md", "skill/SKILL.md"):
        put(runtime / name, (source / name).read_bytes())
    put(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
    launcher = "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(runtime / "wechat_work.py")) + ' "$@"\n'
    put(home / ".local/bin/wechat-work", launcher.encode(), 0o700)
    for target in targets:
        if not target.is_symlink():
            target.symlink_to(runtime / "skill", target_is_directory=True)
    if ".claude" in hosts:
        command = """---
description: 读取微信消息和图片，整理成有出处的工作建议、待办或草稿
argument-hint: 会话、时间范围和工作目标
---

用户请求：$ARGUMENTS

读取并使用 `~/.claude/skills/wechat-work/SKILL.md`。
统一入口为 `~/.local/bin/wechat-work`，无需 Dashboard。
"""
        put(home / ".claude/commands/wechat-work.md", command.encode())
        if replace_wx_image:
            image_command = """---
description: 按消息出处提取本机微信图片并结合上下文理解
argument-hint: 会话和图片时间
---

用户请求：$ARGUMENTS

使用 `~/.claude/skills/wechat-work/SKILL.md` 的图片分支。
先用 `~/.local/bin/wechat-work images` 取得精确引用，再用 `image` 提取并实际打开图片。
"""
            put(home / ".claude/commands/wx-image.md", image_command.encode())
    return {"installed": True, "command": str(home / ".local/bin/wechat-work"), "agents": list(hosts),
            "backup_dir": str(backup_root) if backup_root.exists() else None, "keys_modified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", help="Path to native wx-cli (default: ~/.local/bin/wx-cli)")
    parser.add_argument("--account-dir", help="WeChat account directory containing db_storage; required on first install")
    parser.add_argument("--agent", choices=("codex", "claude", "both"), default="both")
    parser.add_argument("--replace-wx-image", action="store_true", help="Back up and replace the legacy Claude /wx-image command")
    args = parser.parse_args(argv)
    try:
        result = install(Path(__file__).resolve().parent, Path.home(), args.binary, args.account_dir, args.agent, args.replace_wx_image)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError):
        # Config/parser exceptions can include source values. Keep them local.
        print("Installation failed: verify executable, account directory, destination ownership and existing skill entries.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
