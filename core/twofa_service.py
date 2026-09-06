# -*- coding: utf-8 -*-
"""账号 2FA/TOTP 后台设置队列。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import email as _email_cfg
from core import db
from core.account_export import setup_2fa
from core.session import BrowserSession
from core.email_provider import wait_for_otp

logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="twofa")
_QUEUE_SLOTS = threading.BoundedSemaphore(50)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"twofa-{safe}.log"


def _normalize_proxy(proxy: str | None) -> str | None:
    """
    2FA 入口只接受真实代理地址。

    注册流程里有些 `proxy_used` 字段保存的是环境标签，例如 `skyvern:jp`、
    `browser_use:jp`，这类不是 curl_cffi 可用代理，会导致 Unsupported proxy syntax。
    """
    text = str(proxy or "").strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")):
        return text
    return None


def is_running(acc_id: int) -> bool:
    with _LOCK:
        return int(acc_id) in _RUNNING


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _enable_twofa_via_roxy(email: str, access_token: str, otp_wait: callable = wait_for_otp) -> str:
    """用 Roxy 真实浏览器给已有账号开 2FA（页面内 enroll，避免 curl 被 Cloudflare 重置）。

    流程：开 Roxy → chatgpt 登录（邮箱 OTP）→ 页面内 mfa/enroll + activate。
    返回 totp_secret；失败抛异常。
    """
    logger.info("[2FA] 走 Roxy 浏览器路径：email=%s", email)
    from core.roxybrowser_client import RoxyBrowserClient
    from core.roxy_registration import (
        _build_driver, _type_email_address, _submit_email_step, _wait_email_submit_next_state,
        _is_email_verification_page, _type_otp, _clear_otp_inputs, _click_continue,
        _wait_after_email_otp_submit, _fetch_chatgpt_session, _setup_2fa_selenium,
    )
    from core.roxy_codex_oauth import _fill_mfa_challenge_if_present

    client = RoxyBrowserClient()
    opened = client.open_profile()
    driver = None
    try:
        driver = _build_driver(opened)
        logger.info("[2FA] 登录 chatgpt.com：email=%s profile=%s", email, opened.profile_id)
        driver.get("https://chatgpt.com/auth/login")
        _type_email_address(driver, email)
        _submit_email_step(driver, email)
        next_state = _wait_email_submit_next_state(driver, email, timeout=30)
        logger.info("[2FA] 邮箱提交后状态：%s", next_state)

        # 若还没到邮箱验证码页，再等一会儿（部分情况登录页跳转慢/需二次确认）
        if not _is_email_verification_page(driver):
            wait_otp_page_end = time.time() + 40
            while time.time() < wait_otp_page_end:
                if _is_email_verification_page(driver):
                    next_state = "email_verification"
                    break
                time.sleep(1.5)
            logger.info("[2FA] 等待邮箱验证码页后状态：%s url=%s", next_state, driver.current_url if hasattr(driver,"current_url") else "?")

        otp_after = time.time()
        code = otp_wait(email, after_ts=otp_after)
        logger.info("[2FA] 收到邮箱 OTP：%s", code)
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        _click_continue(driver)
        outcome = _wait_after_email_otp_submit(driver, timeout=45)
        logger.info("[2FA] OTP 提交后状态：%s", outcome)

        # 已有账号可能有 2FA 之外的 mfa（理论上没有），兜底处理
        _fill_mfa_challenge_if_present(driver, email, timeout=15)

        session = _fetch_chatgpt_session(driver, timeout=60)
        fresh_token = session.get("accessToken") or access_token
        logger.info("[2FA] 已登录，开始页面内 enroll...")
        secret = _setup_2fa_selenium(driver, fresh_token, email=email)
        if not secret:
            raise RuntimeError("页面内 enroll 失败（未得到 TOTP secret）")
        logger.info("[2FA] 2FA 设置完成：email=%s secret=%s...", email, secret[:4])
        return secret
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        try:
            client.cleanup_profile(opened)
        except Exception:
            pass


def _run_twofa(*, account_id: int, email: str, access_token: str, proxy: str | None, trigger: str) -> dict:
    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_totp_setup_running(account_id):
            return {"ok": False, "status": "failed", "error": "账号已删除或 2FA 状态已被重置"}
        log_file = log_path(email)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("", encoding="utf-8")
        fh = logging.FileHandler(str(log_path(email)), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)
        logger.info("[2FA] 开始后台设置：email=%s trigger=%s", email, trigger)
        # 优先用 Roxy 真实浏览器（页面内 enroll），避免 curl 协议被 chatgpt Cloudflare 重置
        try:
            secret = _enable_twofa_via_roxy(email, access_token, otp_wait=wait_for_otp)
            if secret:
                db.update_account_totp_secret(
                    account_id,
                    {"ok": True, "status": "success", "totp_secret": secret, "message": "2FA 设置完成（浏览器）"},
                )
                _append_log(email, f"[2FA] 完成：secret={secret[:4]}...{secret[-4:]}")
                logger.info("[2FA] 完成：email=%s secret=%s...%s", email, secret[:4], secret[-4:])
                return {"ok": True, "status": "success", "totp_secret": secret, "message": "2FA 设置完成"}
        except Exception as exc:
            logger.warning("[2FA] Roxy 浏览器路径失败，回退 curl 协议：%s", str(exc)[:200])
        real_proxy = _normalize_proxy(proxy)
        session = BrowserSession(proxy=real_proxy, fingerprint_seed=f"account:{email.lower()}")
        _append_log(email, f"[2FA] 会话创建完成：proxy={session.proxy or 'direct'} device_id={session.device_id}")
        _append_log(email, f"[2FA] 指纹摘要：{session.fingerprint_summary_text()}")
        secret = setup_2fa(session, email, access_token=access_token)
        db.update_account_totp_secret(
            account_id,
            {"ok": True, "status": "success", "totp_secret": secret, "message": "2FA 设置完成"},
        )
        _append_log(email, f"[2FA] 完成：secret={secret[:4]}...{secret[-4:]}")
        logger.info("[2FA] 完成：email=%s secret=%s...%s", email, secret[:4], secret[-4:])
        return {"ok": True, "status": "success", "totp_secret": secret, "message": "2FA 设置完成"}
    except Exception as exc:
        result = {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        try:
            db.update_account_totp_secret(account_id, result)
        except Exception:
            logger.exception("[2FA] 写回失败状态失败: account_id=%s", account_id)
        try:
            _append_log(email, f"[2FA] 失败：{result['error']}")
        except Exception:
            pass
        logger.exception("[2FA] 后台异常: %s", email)
        return result
    finally:
        if fh is not None:
            try:
                root_logger.removeHandler(fh)
                fh.close()
            except Exception:
                pass
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_totp_setup(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
    proxy: str | None = None,
) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not access_token:
        return {"accepted": False, "busy": False, "error": "缺少 access_token"}
    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        return {"accepted": False, "busy": False, "error": "启用 2FA 需要先开启 USE_EMAIL_SERVICE 自动收取邮箱验证码"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "2FA 队列已满，请稍后重试"}
    if not db.claim_account_totp_setup(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在设置 2FA"}

    _append_log(email, f"[2FA] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        future = _EXECUTOR.submit(
            _run_twofa,
            account_id=account_id,
            email=email,
            access_token=access_token,
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
        return {"accepted": True, "busy": False, "future": future, "log_path": str(log_path(email))}
    except Exception as exc:
        _QUEUE_SLOTS.release()
        db.update_account_totp_secret(account_id, {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return {"accepted": False, "busy": False, "error": f"{type(exc).__name__}: {exc}"}
