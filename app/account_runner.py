from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values, load_dotenv

from app.accounts import load_accounts
from app.config import ConfigError, load_settings
from app.history import run_lock
from app.main import LOGGER, _configure_logging, _parse_cli_args, run


_LEGACY_ENV_KEYS = ("DOUYIN_COOKIE", "DOUYIN_STORAGE_STATE", "TASK_CONFIG", "ARTIFACTS_DIR")
# 并发执行账号的最大数量，避免同时打开过多浏览器实例触发风控
_MAX_CONCURRENT_ACCOUNTS = 3


def run_all_accounts() -> int:
    """并行执行 accounts.json 中所有启用账号。

    单账号失败只记为该账号 failed，不阻止其他账号运行。
    返回码语义与单账号一致：0=全部成功，1=存在失败，2=多账号配置整体错误。
    """
    import asyncio as _asyncio

    args = _parse_cli_args()
    accounts = load_accounts()
    if not accounts:
        print("没有启用任何账号，本次任务跳过")
        return 0
    if args.env_file:
        load_dotenv(args.env_file)
    for key in _LEGACY_ENV_KEYS:
        os.environ.pop(key, None)

    _configure_logging(Path("artifacts"), label=None, reset=True)
    LOGGER.info("多账号模式：共 %d 个启用账号，最大并发 %d", len(accounts), _MAX_CONCURRENT_ACCOUNTS)

    async def _run_one(account) -> tuple[str, str, str | None]:
        _configure_logging(Path("artifacts") / account.id, label=account.id, reset=True)
        LOGGER.info("开始执行任务")
        try:
            with account_env(account.env_file, defaults={"ARTIFACTS_DIR": f"artifacts/{account.id}"}):
                settings = load_settings(None)
                _configure_logging(settings.artifacts_dir, label=account.id, reset=True)
                with run_lock(settings.artifacts_dir / "run.lock"):
                    code = await run(dry_run=args.dry_run)
            status = "success" if code == 0 else "failed"
            LOGGER.info("执行完成: %s", status)
            return (account.id, status, None)
        except Exception as exc:
            summary.append((account.id, "failed", type(exc).__name__))
            LOGGER.exception("执行失败: %s", exc)
            return (account.id, "failed", type(exc).__name__)

    summary: list[tuple[str, str, str | None]] = []

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ACCOUNTS)

    async def _guarded_run(account) -> tuple[str, str, str | None]:
        async with semaphore:
            return await _run_one(account)

    async def _gather_all():
        tasks = [_guarded_run(acct) for acct in accounts]
        return await asyncio.gather(*tasks)

    results = _asyncio.run(_gather_all())
    summary = list(results)

    _configure_logging(Path("artifacts"), label=None, reset=True)
    for account_id, status, error in summary:
        detail = f" - {error}" if error else ""
        LOGGER.info("[%s] 结果: %s%s", account_id, status, detail)
    failed = sum(1 for _, status, _ in summary if status == "failed")
    LOGGER.info("多账号执行结束: 成功 %d，失败 %d", len(summary) - failed, failed)
    return 1 if failed else 0


def _load_account_env(env_file: Path, defaults: dict[str, str] | None = None) -> dict[str, str]:
    env_file = Path(env_file)
    if not env_file.is_file():
        raise ConfigError(f"账号环境文件不存在: {env_file}")
    values = {key: value for key, value in (dotenv_values(env_file) or {}).items() if value is not None}
    for key, value in (defaults or {}).items():
        values.setdefault(key, value)
    return values


@contextmanager
def account_env(env_file: Path, defaults: dict[str, str] | None = None):
    """临时把账号 env 应用到进程环境，退出时完全恢复。"""
    values = _load_account_env(env_file, defaults)
    saved = {key: os.environ[key] for key in values if key in os.environ}
    fresh = set(values) - set(saved)
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.update(saved)
        for key in fresh:
            os.environ.pop(key, None)
