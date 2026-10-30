"""账号系统初始化命令。

两种用法，语义等价：

- 交互式（最常用，人在终端前）：`python -m app.auth_cli create-admin`，密码走 `getpass`。
- 非交互式（新机器自动部署、容器、由脚本或另一个 agent 执行验收）：加 `--password-env`
  指定一个环境变量名，密码从该变量读取。这条路径存在的原因很具体：`getpass` 在
  非 TTY（管道、CI、docker exec 无 `-t`）下会阻塞，而交付包要求「拿到包的人必须先
  建一个账号才能进界面」，没有非交互入口就卡在登录页 —— 2026-10-18 异机演练
  正是这样停住的。

密码不出现在命令行参数里（那会留在进程列表与 shell 历史里），只经环境变量传递。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from app.auth_store import AuthError, AuthStore


def _read_password(args: argparse.Namespace) -> str | None:
    """取密码：优先环境变量（非交互），否则交互式输入。两次不一致返回 None。"""
    env_name = getattr(args, "password_env", None)
    if env_name:
        password = os.environ.get(env_name, "")
        if not password:
            print(f"环境变量 {env_name} 为空，无法读取密码。", file=sys.stderr)
            return None
        return password
    password = getpass.getpass("管理员密码（至少 12 位）：")
    confirm = getpass.getpass("再次输入密码：")
    if password != confirm:
        print("两次输入的密码不一致。", file=sys.stderr)
        return None
    return password


def main() -> int:
    parser = argparse.ArgumentParser(description="管理循证问答本机账号")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="创建首个管理员账号")
    admin.add_argument("--username", default="admin")
    reset = commands.add_parser("reset-admin-password", help="管理员忘记密码时重置密码")
    reset.add_argument("--username", required=True)
    for sub in (admin, reset):
        sub.add_argument(
            "--password-env",
            metavar="VAR",
            help="从环境变量 VAR 读取密码，用于无非交互终端的场景（管道 / CI / docker exec）",
        )
    args = parser.parse_args()

    password = _read_password(args)
    if password is None:
        return 2
    store = AuthStore()
    try:
        if args.command == "create-admin":
            user = store.create_user(args.username, password, "admin")
        else:
            user = store.get_user_by_username(args.username)
            if user is None or user.role != "admin":
                raise AuthError("没有找到该管理员账号")
            store.reset_password(user.id, password)
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.command == "create-admin":
        print(f"管理员账号已创建：{user.username}（{user.id}）")
    else:
        print(f"管理员密码已重置：{user.username}")
    print("请妥善保管密码；系统仅保存不可逆的密码校验值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

