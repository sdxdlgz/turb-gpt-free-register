# -*- coding: utf-8 -*-
"""
接码平台客户端。

用于 Codex OAuth "全新 session" 流程过 OpenAI 的 /phone-verification 手机号验证：
    1. acquire_number()       getNumber 取一个手机号（返回 激活ID + 号码）
    2. wait_for_sms_code()    轮询 getStatus 直到拿到短信验证码
    3. complete() / cancel()  setStatus 标记完成(6) / 取消(8)

当前支持：
    - GrizzlySMS：GET 文本接口，文档 https://api.grizzlysms.com
    - L：本地 JSON 管理接口，文档 L_API.md
    - H：本地 JSON 管理接口，文档 H_API.md

价格相关：每取一个号、收到短信都会计费，所以：
    - 取号后若收不到短信，必须 cancel(8) 释放，避免白扣钱；
    - 成功拿到码后 complete(6) 正式完成激活。
"""
import json
import logging
import re
import threading
import time
from urllib.parse import urljoin

from curl_cffi.requests import Session as CurlSession

# 注意：用 `from config import codex` 而不是 `from config.codex import X`，
# 这样 WebUI 调 config.reload_all() 后，本模块通过 codex.X 读到的是最新值。
from config import codex as _cfg
from config import IMPERSONATE

logger = logging.getLogger(__name__)

# GrizzlySMS 规则：号码取出后 2 分钟内不允许取消（防薅号）。
# 这里留 5 秒缓冲，时间到了再发 setStatus=8。
_MIN_CANCEL_DELAY = 125

# 记录每个 activation_id 的取号时间，供 cancel() 判断是否要等。
# 用模块级 dict 而不是改 acquire_number 返回值，保持向后兼容。
_ACQUIRED_AT: dict[str, float] = {}


class SmsProviderError(RuntimeError):
    """接码平台通用错误。"""


class SmsNoNumbersError(SmsProviderError):
    """暂无可用号码（NO_NUMBERS），可换国家或稍后重试。"""


class SmsNoBalanceError(SmsProviderError):
    """余额不足（NO_BALANCE），必须充值，重试无意义——上层应立即停止。"""


class SmsCodeTimeout(SmsProviderError):
    """单个号等短信超时（OpenAI 没发或没到达）。"""


def _http() -> CurlSession:
    s = CurlSession(impersonate=IMPERSONATE)
    s.timeout = _cfg.SMS_REQUEST_TIMEOUT
    return s


def _provider() -> str:
    return str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()


def _request_grizzly(http: CurlSession, params: dict) -> str:
    """发一个 SMS-Activate 兼容 API 请求（GrizzlySMS / SMSBower 等），返回去空白的响应文本。"""
    provider_label = "SMSBower" if _provider() == "smsbower" else "GrizzlySMS"
    base_params = {"api_key": _cfg.SMS_API_KEY}
    base_params.update(params)
    resp = http.get(_cfg.SMS_API_BASE, params=base_params)
    if resp.status_code != 200:
        raise SmsProviderError(
            f"{provider_label} HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    text = (resp.text or "").strip()

    # 公共错误码（任何 action 都可能返回）
    if text == "BAD_KEY":
        raise SmsProviderError("接码平台 API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalanceError("接码平台余额不足（NO_BALANCE），请充值")
    if text == "NO_NUMBERS":
        raise SmsNoNumbersError("接码平台暂无可用号码（NO_NUMBERS）")
    if text == "SERVICE_UNAVAILABLE_REGION":
        raise SmsProviderError("接码平台地区受限（SERVICE_UNAVAILABLE_REGION），请换 IP")
    if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
        raise SmsProviderError(f"接码平台请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsProviderError("激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"该服务被平台禁售：{text}")

    return text


def _request_grizzly_json(http: CurlSession, params: dict) -> dict:
    """调用 SMS-Activate 兼容平台返回 JSON 的接口（getCountries / getTopCountriesByService / getPrices*）。"""
    provider_label = "SMSBower" if _provider() == "smsbower" else "GrizzlySMS"
    base_params = {"api_key": _cfg.SMS_API_KEY}
    base_params.update(params)
    resp = http.get(_cfg.SMS_API_BASE, params=base_params)
    if resp.status_code != 200:
        raise SmsProviderError(
            f"{provider_label} HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SmsProviderError(
            f"{provider_label} 响应不是 JSON ({params.get('action')}): {(resp.text or '')[:200]}"
        ) from exc
    if isinstance(payload, dict):
        err = payload.get("status") if isinstance(payload.get("status"), str) else None
        if payload.get("BAD_KEY") or err in ("error", "BAD_KEY", "BAD_SERVICE", "BAD_ACTION"):
            raise SmsProviderError(f"{provider_label} 自动选号接口错误：{str(payload)[:200]}")
    return payload


def _smooth_country_key(value) -> str:
    """把国家名归一化成小写字母指纹，兼容 'United States' / 'united-states' / 'USA' 等写法。"""
    return re.sub(r"[^a-z]", "", str(value or "").strip().lower())


def _smsbower_country_id_map(http: CurlSession) -> dict[str, str]:
    """国家英文名指纹 -> 国家 id，保留原始小写名两个 key（getNumber 用数字 id）。"""
    mapping: dict[str, str] = {}
    try:
        payload = _request_grizzly_json(http, {"action": "getCountries"})
    except Exception as exc:
        logger.warning("[SMS] getCountries 拉取失败（自动选号回退固定国家）：%s", str(exc)[:160])
        return mapping

    items: list = []
    if isinstance(payload, dict):
        vals = list(payload.values())
        if vals and all(isinstance(v, dict) for v in vals):
            items = vals
        else:
            items = payload.get("countries") or payload.get("data") or payload.get("list") or []
            if not isinstance(items, list) and isinstance(payload.get("data"), list):
                items = payload.get("data")
    elif isinstance(payload, list):
        items = payload

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("eng") or item.get("rus") or item.get("name") or "").strip().lower()
        cid = str(item.get("id") or "").strip()
        if not name or not cid:
            continue
        mapping[name] = cid
        mapping[_smooth_country_key(name)] = cid
    return mapping


def _smsbower_pick_cheapest_gold(http: CurlSession) -> list[tuple[str, str, float]]:
    """在 SMSBower gold 级别号码里，按价格上限选最便宜的一批候选（价格升序）。

    返回 [(country_id, provider_ids, price), ...]；为空则抛 SmsNoNumbersError。
    调用方逐个尝试（某供应商没货就换下一个 / 去掉供应商限制），避免“单个没货即失败”。
    """
    top = _request_grizzly_json(
        http, {"action": "getTopCountriesByService", "service": _cfg.SMS_SERVICE}
    )
    if not isinstance(top, dict):
        raise SmsProviderError("getTopCountriesByService 响应不是对象，无法自动选号")
    id_map = _smsbower_country_id_map(http)

    max_price_raw = str(_cfg.SMS_MAX_PRICE or "").strip()
    try:
        max_price = float(max_price_raw) if max_price_raw else None
    except (TypeError, ValueError):
        max_price = None

    candidates: list[tuple[str, str, float]] = []
    seen = set()
    for cname, providers in top.items():
        if not isinstance(cname, str) or not isinstance(providers, dict):
            continue
        # 虚拟号通常收不到 OpenAI 短信，默认跳过
        if "virtual" in cname.lower() or "虚拟" in cname:
            continue
        cid = id_map.get(cname.strip().lower()) or id_map.get(_smooth_country_key(cname))
        if not cid:
            continue
        for pid, info in providers.items():
            if not isinstance(pid, str) and not isinstance(pid, int):
                continue
            if not isinstance(info, dict):
                continue
            try:
                price = float(info.get("price") or 0)
                count = int(info.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            if max_price is not None and price > max_price:
                continue
            key = (cid, str(pid))
            if key in seen:
                continue
            seen.add(key)
            candidates.append((cid, str(pid), price))

    # 价格升序
    candidates.sort(key=lambda x: x[2])
    if not candidates:
        raise SmsNoNumbersError(
            "自动选号：当前服务没有符合价格上限的 gold 号码"
            + (f"（maxPrice={max_price}）" if max_price is not None else "")
        )
    logger.info(
        "[SMS] 自动选号（gold 候选）：共 %s 个，最便宜 country_id=%s provider_id=%s price=%.4f",
        len(candidates), candidates[0][0], candidates[0][1], candidates[0][2],
    )
    return candidates


def _l_url(path: str) -> str:
    base = str(getattr(_cfg, "L_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("L_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _l_headers() -> dict:
    token = str(getattr(_cfg, "L_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("L_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_l_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_l_url(path), headers=_l_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"L HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"L 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"L 暂无可用号码：{combined}")
        raise SmsProviderError(f"L 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"L 响应不是 JSON 对象：{text[:200]}")
    return data


def _h_url(path: str) -> str:
    base = str(getattr(_cfg, "H_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("H_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _h_headers() -> dict:
    token = str(getattr(_cfg, "H_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("H_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_h_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_h_url(path), headers=_h_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"H HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"H 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"H 暂无可用号码：{combined}")
        raise SmsProviderError(f"H 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"H 响应不是 JSON 对象：{text[:200]}")
    return data


def _release_h_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 H_API /api/admin/h/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("H release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"H release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:H] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_h_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 H 号码。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("H release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:H] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _release_l_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 L_API /api/admin/l/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("L release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            # 接口允许部分失败。单个释放时 failed 非空基本代表这个 id 释放失败。
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"L release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:L] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_l_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 L 号码，供工具/后续批处理复用。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("L release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:L] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _normalize_phone_digits(value: str) -> str:
    """把平台返回/配置的号码片段规范化为纯数字，避免 +-849... 这类非法 E.164。"""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _normalize_l_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "L_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _normalize_h_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "H_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _h_phone_acquire_mode() -> str:
    """
    H 取号模式：
      - reusable/reuse/prefer_reuse：优先复用，调用 /api/admin/h/take-reusable-phone
      - new/fresh/always_new：每次取新号，调用 /api/admin/h/take-phone
    """
    raw = str(getattr(_cfg, "H_PHONE_ACQUIRE_MODE", "reusable") or "reusable").strip().lower()
    if raw in ("new", "fresh", "always_new", "take_phone", "take-phone", "每次取新号", "新号"):
        return "new"
    return "reusable"


# ============================================================
# 取号
# ============================================================

def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
) -> tuple[str, str]:
    """
    取一个手机号（getNumber）。

    Returns:
        (activation_id, phone_number) —— phone_number 不带 + 前缀（如 16195366483）

    Raises:
        SmsNoNumbersError / SmsNoBalanceError / SmsProviderError
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "l":
            payload = {
                "service": service or _cfg.SMS_SERVICE,
                "country": country or _cfg.SMS_COUNTRY,
            }
            if _cfg.SMS_MAX_PRICE:
                payload["maxPrice"] = _cfg.SMS_MAX_PRICE

            data = _post_l_json(http, "/api/admin/l/take-phone", payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "L_PHONE_PREFIX", "") or "")
            phone = _normalize_l_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:L] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"L take-phone 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            logger.info(f"[SMS:L] 取号成功：id={activation_id}, phone=+{phone}")
            return activation_id, phone

        if _provider() == "h":
            # H_API 使用 projectId + country；统一复用 SMS_SERVICE / SMS_COUNTRY，
            # 避免接码平台之间出现重复的“服务/国家”配置。
            project_id = str(service or _cfg.SMS_SERVICE).strip()
            h_country = str(country or _cfg.SMS_COUNTRY).strip()
            if not project_id:
                raise SmsProviderError("H projectId 不能为空：请填写 SMS_SERVICE")
            if not h_country:
                raise SmsProviderError("H country 不能为空：请填写 SMS_COUNTRY")
            payload = {
                "projectId": project_id,
                "country": h_country,
            }
            mode = _h_phone_acquire_mode()
            api_path = "/api/admin/h/take-phone" if mode == "new" else "/api/admin/h/take-reusable-phone"
            data = _post_h_json(http, api_path, payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "H_PHONE_PREFIX", "") or "")
            phone = _normalize_h_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:H] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"H {api_path.rsplit('/', 1)[-1]} 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            logger.info(
                f"[SMS:H] 取号成功：mode={mode}, api={api_path}, id={activation_id}, phone=+{phone}, "
                f"reused={bool(data.get('reused'))}, duplicate={bool(data.get('duplicate'))}"
            )
            return activation_id, phone

        auto_country = False
        country_val = str(country or _cfg.SMS_COUNTRY or "").strip()
        if country_val.lower() in ("auto", "random", "cheapest", "*"):
            auto_country = True

        base_params: dict = {"action": "getNumber", "service": service or _cfg.SMS_SERVICE}
        if _cfg.SMS_MAX_PRICE:
            base_params["maxPrice"] = _cfg.SMS_MAX_PRICE

        def _try_getnumber(params: dict) -> tuple[str, str]:
            text = _request_grizzly(http, params)
            # 成功格式：ACCESS_NUMBER:激活ID:号码
            if not text.startswith("ACCESS_NUMBER:"):
                raise SmsProviderError(f"getNumber 非预期响应：{text[:200]}")
            parts = text.split(":")
            if len(parts) < 3:
                raise SmsProviderError(f"getNumber 响应格式异常：{text[:200]}")
            activation_id = parts[1].strip()
            phone = parts[2].strip()
            _ACQUIRED_AT[activation_id] = time.time()
            logger.info(f"[SMS] 取号成功：activation_id={activation_id}, phone=+{phone}")
            return activation_id, phone

        if auto_country:
            # 自动选号：候选按价格升序，逐个试；带供应商没货则同国去供应商再试。
            candidates = _smsbower_pick_cheapest_gold(http)
            last_no_numbers: SmsNoNumbersError | None = None
            for cid, pid, price in candidates:
                params = dict(base_params, country=cid, providerIds=pid)
                try:
                    return _try_getnumber(params)
                except SmsNoNumbersError as exc:
                    last_no_numbers = exc
                    logger.info("[SMS] 自动选号候选无货(带供应商)：country=%s provider=%s price=%.4f，尝试同国去掉供应商", cid, pid, price)
                    # 同国去掉 providerIds，可能其它供应商有号
                    try:
                        return _try_getnumber(dict(base_params, country=cid))
                    except SmsNoNumbersError:
                        logger.info("[SMS] 自动选号同国也无货，换下一个候选：country=%s provider=%s", cid, pid)
                        continue
            if last_no_numbers is not None:
                raise last_no_numbers
            raise SmsNoNumbersError("自动选号：所有候选 gold 号码均无货")

        params = dict(base_params, country=country_val or _cfg.SMS_COUNTRY or "")
        return _try_getnumber(params)
    finally:
        if own_http:
            http.close()


# ============================================================
# 取短信验证码
# ============================================================

def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
) -> str:
    """
    轮询 getStatus 直到拿到短信验证码。

    Returns:
        验证码字符串

    Raises:
        SmsCodeTimeout —— 超时没收到（上层可换号重试）
        SmsProviderError —— 激活被取消等
    """
    own_http = http is None
    http = http or _http()
    deadline = time.time() + (max_wait or _cfg.SMS_CODE_WAIT)
    interval = poll_interval or _cfg.SMS_POLL_INTERVAL
    try:
        provider = _provider()
        total_wait = max_wait or _cfg.SMS_CODE_WAIT
        logger.info(f"[SMS] 等待短信验证码 activation_id={activation_id}，最长 {total_wait}s...")
        round_no = 0
        while time.time() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            round_no += 1
            elapsed = max(0, int(total_wait - max(0, deadline - time.time())))
            remaining_before = max(0, int(deadline - time.time()))
            logger.info(
                f"[SMS] 第 {round_no} 轮获取验证码 activation_id={activation_id}，"
                f"已等 {elapsed}s，剩余约 {remaining_before}s"
            )
            if provider == "l":
                data = _post_l_json(http, "/api/admin/l/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:L] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:L] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "h":
                data = _post_h_json(http, "/api/admin/h/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:H] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:H] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            text = _request_grizzly(http, {"action": "getStatus", "id": activation_id})

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info(f"[SMS] 第 {round_no} 轮收到验证码：{code}")
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError("激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY:* / STATUS_WAIT_RESEND → 继续等
            remaining = max(0, int(deadline - time.time()))
            logger.info(f"[SMS] 第 {round_no} 轮未收到验证码，状态={text}，{interval}s 后重试（剩余 {remaining}s）")
            time.sleep(interval)

        raise SmsCodeTimeout(f"等待短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


# ============================================================
# 改状态
# ============================================================

def set_status(activation_id: str, status: int, http: CurlSession | None = None) -> str:
    """
    设置激活状态（setStatus）。
        1 = 号码已就绪（短信已发出）
        3 = 等下一条短信（重发）
        6 = 完成激活
        8 = 取消激活
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "l":
            logger.debug(f"[SMS:L] 忽略状态设置 id={activation_id}, status={status}")
            return "OK"
        return _request_grizzly(http, {"action": "setStatus", "status": str(status), "id": activation_id})
    finally:
        if own_http:
            http.close()


def complete(activation_id: str, http: CurlSession | None = None) -> None:
    """标记激活完成（status=6）。失败只告警不抛，避免影响主流程。"""
    if _provider() == "l":
        logger.info(f"[SMS:L] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        return
    if _provider() == "h":
        # H 成功 fetch-code 后后台会自动按多次收码策略重取；这里不 release。
        logger.info(f"[SMS:H] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        return
    try:
        set_status(activation_id, 6, http=http)
        logger.info(f"[SMS] 已标记完成 activation_id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
    except Exception as exc:
        logger.warning(f"[SMS] 标记完成失败（不影响结果）：{exc}")


def _do_cancel_sync(activation_id: str, http_factory) -> None:
    """实际的同步取消逻辑：等够 2 分钟限制 → 发请求 → 失败重试一次。"""
    acquired_at = _ACQUIRED_AT.get(activation_id)
    if acquired_at is not None:
        elapsed = time.time() - acquired_at
        if elapsed < _MIN_CANCEL_DELAY:
            wait = _MIN_CANCEL_DELAY - elapsed
            logger.info(
                f"[SMS] 取消等待 GrizzlySMS 2 分钟限制：activation_id={activation_id}，"
                f"还需等 {wait:.0f}s..."
            )
            time.sleep(wait)

    # 后台线程不能复用外部 http session（curl_cffi 非线程安全），自己建一个
    http = http_factory()
    try:
        for attempt in range(1, 3):
            try:
                set_status(activation_id, 8, http=http)
                logger.info(f"[SMS] 已取消 activation_id={activation_id}")
                _ACQUIRED_AT.pop(activation_id, None)
                return
            except Exception as exc:
                if attempt == 1:
                    logger.warning(f"[SMS] 取消失败（{exc}），5s 后重试...")
                    time.sleep(5)
                else:
                    logger.warning(
                        f"[SMS] 取消最终失败（不影响结果，需到平台手动取消）：activation_id={activation_id}, {exc}"
                    )
    finally:
        try:
            http.close()
        except Exception:
            pass


def cancel(activation_id: str, http: CurlSession | None = None, background: bool = True) -> None:
    """
    取消激活（status=8），释放号码避免白扣费。

    GrizzlySMS 规则：号码取出后约 2 分钟内不允许取消。本函数默认 background=True，
    把"等 2 分钟+取消"放到后台守护线程里执行，主流程立刻返回继续走（如换下一个号），
    避免被这 2 分钟阻塞。

    background=False 时同步等够时间再返回（少数场景需要确认取消完成时用）。

    失败只告警不抛，不影响主流程。
    """
    if _provider() == "l":
        try:
            _release_l_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:L] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
        return
    if _provider() == "h":
        try:
            _release_h_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:H] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
        return

    if not background:
        _do_cancel_sync(activation_id, _http)
        return

    t = threading.Thread(
        target=_do_cancel_sync,
        args=(activation_id, _http),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    t.start()
    logger.debug(f"[SMS] 取消任务已派后台：activation_id={activation_id}")
