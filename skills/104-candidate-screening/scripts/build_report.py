#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把履歷記錄 + 候選人照片組成一份自足的 HTML 報表。

用法：
    python build_report.py --config report_config.json
    python build_report.py --init            # 產生一份設定檔範本

設計取捨：照片內嵌成 base64、沒有任何外部連結，因為報表要能離線讀、能直接寄給人，
而且 104 的圖片網址本來就需要登入才打得開。縮圖壓到 132px 是為了讓四十份履歷的頁面
停在 1 MB 以內——原尺寸照片會讓檔案膨脹到瀏覽器開起來卡頓。
"""
import argparse
import base64
import datetime
import hashlib
import io
import json
import os
import sys

# 104 在候選人沒放照片時回傳的預設灰色人形剪影。它是一個固定檔（樣本中位元組完全
# 相同），所以用 md5 認。不要改用檔案大小當門檻——104 換圖時大小會變，「是固定檔」
# 這個性質才是穩定的。
#
# ⚠ 這是**冗餘的第二道網，不是這條規則的擁有者**。擁有者是 MCP server 自己：
# src/mcp104/tools/resume_files.py 的 _PLACEHOLDER_PHOTO_SHA256（**sha256**，不是
# md5），它在檔案落地之前就把剪影擋掉並回 photo: null，所以經由 get_candidate_photo
# （0.3.1 起）拿到的照片永遠不會走到這裡。這道網擋的是不經那個工具拿到的位元組——
# 舊版留在磁碟上的、手動放進 photo_dir 的。
# 104 換圖那天**兩個常數都要補**（那邊 sha256、這邊 md5），只改一個會讓工具與報表
# 對「有沒有照片」講出相反的話。原委見 references/mcp104-notes.md §3。
PLACEHOLDER_MD5 = {
    "d388efdd9ec6e9a242753f2fdedd90c6",  # 894 bytes PNG，2026-09 量到
}

THUMB_PX = 132
JPEG_QUALITY = 78

TEMPLATE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "assets", "report_template.html"))

CONFIG_TEMPLATE = {
    "title": "應用工程師 · 完整履歷",
    "job_no": "12345678",
    "records": "full/all.json",
    "photo_dir": "招募/應用工程師/附件",
    "output": "招募/應用工程師/104完整履歷.html",
    "note": "（可留空）例如：某位履歷讀取失敗未納入",
    "salary": {
        "over_pattern": "年薪",
        "in_pattern": "月薪.*(4[5-9],|5[0-5],)",
    },
    "groups": [
        {"key": "AB", "label": "半導體設備 ＋ 機械設備 雙命中"},
        {"key": "A", "label": "測試廠／封測廠設備工程師"},
        {"key": "C", "label": "半導體設備維修／設備商客服"},
        {"key": "D", "label": "純機械設備維修"},
    ],
    "flags": [
        {
            "key": "hsin",
            "label": "希望地含新竹",
            "field": "wc",
            "contains": "新竹",
            "tag_when_false": "希望地不含新竹",
        }
    ],
}


def die(msg):
    print("錯誤：" + msg, file=sys.stderr)
    sys.exit(1)


def thumbnail(path):
    """回傳 base64 JPEG 縮圖；如果是 104 的預設剪影則回 None。"""
    raw = open(path, "rb").read()
    if hashlib.md5(raw).hexdigest() in PLACEHOLDER_MD5:
        return None
    try:
        from PIL import Image
    except ImportError:
        die("需要 Pillow 才能處理照片：pip install Pillow"
            "（或在設定檔移除 photo_dir 以跳過照片）")
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    s = THUMB_PX / min(w, h)
    im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    w, h = im.size
    left, top = (w - THUMB_PX) // 2, (h - THUMB_PX) // 2
    im = im.crop((left, top, left + THUMB_PX, top + THUMB_PX))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def attach_photos(records, photo_dir):
    """依 cid 找 photo-<cid>.<ext>，填進 record['photo']。回傳統計。"""
    found = placeholder = missing = 0
    for r in records:
        r["photo"] = None
        cid = r.get("cid")
        if not photo_dir or not cid:
            missing += 1
            continue
        for ext in (".jpg", ".jpeg", ".png"):
            p = os.path.join(photo_dir, "photo-%s%s" % (cid, ext))
            if os.path.exists(p):
                r["photo"] = thumbnail(p)
                if r["photo"]:
                    found += 1
                else:
                    placeholder += 1
                break
        else:
            missing += 1
    return found, placeholder, missing


def main():
    ap = argparse.ArgumentParser(description="產生 104 候選人履歷 HTML 報表")
    ap.add_argument("--config", help="設定檔路徑（JSON）")
    ap.add_argument("--init", action="store_true", help="印出設定檔範本後結束")
    args = ap.parse_args()

    if args.init:
        print(json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2))
        return
    if not args.config:
        die("要 --config <設定檔> 或 --init")

    cfg_path = os.path.abspath(args.config)
    base = os.path.dirname(cfg_path)          # 設定檔內的相對路徑以設定檔所在目錄為準，
    cfg = json.load(open(cfg_path, encoding="utf-8"))   # 這樣整份設定可以搬家

    def rel(p):
        return p if not p or os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    rec_path = rel(cfg.get("records"))
    if not rec_path or not os.path.exists(rec_path):
        die("找不到履歷記錄檔：%s" % rec_path)
    records = json.load(open(rec_path, encoding="utf-8"))
    if not isinstance(records, list) or not records:
        die("履歷記錄檔要是一個非空的陣列")

    found, placeholder, missing = attach_photos(records, rel(cfg.get("photo_dir")))

    known = {g["key"] for g in cfg.get("groups", [])}
    ungrouped = [r.get("n") for r in records if r.get("g") not in known]

    bits = ["%d 位" % len(records)]
    if cfg.get("job_no"):
        bits.insert(0, "jobno " + str(cfg["job_no"]))
    bits.append("照片 %d 位%s" % (found,
                "（%d 位為 104 預設剪影，視同未放）" % placeholder if placeholder else ""))
    n_att = sum(1 for r in records if r.get("att"))
    if n_att:
        bits.append("%d 位有履歷附件" % n_att)
    if cfg.get("note"):
        bits.append(cfg["note"])
    bits.append(datetime.date.today().isoformat())

    view = {
        "subtitle": " ｜ ".join(bits),
        "groups": cfg.get("groups", []),
        "flags": cfg.get("flags", []),
        "salary": cfg.get("salary", {}),
    }

    html = open(TEMPLATE, encoding="utf-8").read()
    html = (html
            .replace("__TITLE__", cfg.get("title", "候選人履歷"))
            .replace("__CONFIG__", json.dumps(view, ensure_ascii=False))
            .replace("__DATA__", json.dumps(records, ensure_ascii=False)))

    out = rel(cfg.get("output") or "report.html")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write(html)

    print("寫入 %s（%.2f MB）" % (out, os.path.getsize(out) / 1048576))
    print("照片 %d 張 ／ 預設剪影 %d ／ 找不到檔 %d" % (found, placeholder, missing))
    if ungrouped:
        print("⚠ %d 位沒有對應到設定檔裡的組別，會顯示在「未分組」：%s"
              % (len(ungrouped), "、".join(str(x) for x in ungrouped[:8])))


if __name__ == "__main__":
    main()
