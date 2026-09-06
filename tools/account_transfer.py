#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号整包导出/导入（Windows ↔ VPS 搬移，完整字段，含 access_token/totp/密码/extra/user/plan/codex 等）。

用法：
  python tools/account_transfer.py export <out.json> [--email a@b.com]   # 全导或只导某邮箱
  python tools/account_transfer.py import <in.json> [--merge]           # 导入；默认跳过已存在，--merge 用文件覆盖已存在
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("account_transfer")


def _load_accounts():
    from core import db
    return db._load_accounts()


def _save_accounts(rows):
    from core import db
    db._save_accounts(rows)


def _normalize(rec):
    """把记录整理成与 db 现有行一致的字段形态，保证导入后 WebUI 能读。"""
    rec = dict(rec or {})
    # 补齐 copy_line 由 _save_accounts 统一生成，这里无需处理。
    return rec


def cmd_export(args):
    rows = _load_accounts()
    if args.email:
        want = {e.strip().lower() for e in args.email.split(",") if e.strip()}
        rows = [r for r in rows if (r.get("email") or "").lower() in want]
    out = {
        "app": "turb-gpt-free-register",
        "kind": "accounts-full",
        "version": 1,
        "count": len(rows),
        "accounts": [dict(r) for r in rows],
    }
    with open(args.file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    logger.info("已导出 %s 个账号到 %s", len(rows), args.file)
    return 0


def cmd_import(args):
    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        records = data.get("accounts") or []
    elif isinstance(data, list):
        records = data
    else:
        logger.error("无法识别的文件格式")
        return 1

    existing = _load_accounts()
    by_email = { (r.get("email") or "").lower(): r for r in existing }
    added, updated, skipped = 0, 0, 0
    for rec in records:
        rec = _normalize(rec)
        email = (rec.get("email") or "").strip()
        if not email:
            skipped += 1
            continue
        key = email.lower()
        if key in by_email:
            if not args.merge:
                skipped += 1
                continue
            # 用文件记录覆盖已存在（保留 id/created_at）
            old = by_email[key]
            for k, v in rec.items():
                if k not in ("id", "created_at", "copy_line"):
                    old[k] = v
            updated += 1
        else:
            from core import db
            row = dict(rec)
            row.setdefault("id", max((int((r.get("id") or 0)) for r in existing + [row]), default=0) + (added + updated + skipped + 1))
            row.setdefault("created_at", __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
            existing.append(row)
            by_email[key] = row
            added += 1

    _save_accounts(existing)
    logger.info("导入完成：新增=%s 更新=%s 跳过=%s", added, updated, skipped)
    return 0


def main():
    ap = argparse.ArgumentParser(description="账号整包导出/导入")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export", help="导出账号到 JSON")
    pe.add_argument("file")
    pe.add_argument("--email", default="", help="只导出这些邮箱（逗号分隔），缺省全部")
    pe.set_defaults(func=cmd_export)
    pi = sub.add_parser("import", help="从 JSON 导入账号")
    pi.add_argument("file")
    pi.add_argument("--merge", action="store_true", help="用文件覆盖已存在的账号")
    pi.set_defaults(func=cmd_import)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
