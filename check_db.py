"""KamaClaude S8 SQLite 诊断脚本 — 美化输出版"""
import sqlite3
import os
import json

# ──────────────────────────────── 显示宽度 ────────────────────────────────

def display_width(s):
    w = 0
    for ch in str(s):
        cp = ord(ch)
        # CJK + 全角
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2A6DF or
                0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF):
            w += 2
        # Emoji
        elif (0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or
              0x1F1E0 <= cp <= 0x1F1FF):
            w += 2
        else:
            w += 1
    return w

def pad(s, width, align="<"):
    d = display_width(s)
    if d >= width:
        return s
    gap = " " * (width - d)
    return gap + s if align == ">" else s + gap

def short(text, max_width):
    text = str(text)
    w = 0
    out = []
    for ch in text:
        cw = display_width(ch)
        if w + cw > max_width:
            break
        out.append(ch)
        w += cw
    if out != list(text):
        out.append("…")
    return "".join(out)

# ──────────────────────────────── 装饰 ────────────────────────────────

def h1(text):
    print(f"\n{'='*64}")
    print(f"  {text}")
    print(f"{'='*64}")

def h2(text):
    bar = "─" * max(20, 52 - len(text))
    print(f"\n── {text} {bar}")

def info(label, value, indent=2):
    print(f"{' '*indent}{pad(label, 18)}{value}")

# ──────────────────────────────── 主逻辑 ────────────────────────────────

db = os.path.expanduser(r"~/.kama/sessions/execution.sqlite3")

h1("KamaClaude S8 SQLite 诊断")
info("DB 路径", db)
info("文件存在", os.path.exists(db))

if not os.path.exists(db):
    print("\n  ! SQLite DB 还不存在 — 先跑一次 kama run 吧")
    raise SystemExit(0)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("PRAGMA user_version")
version = cur.fetchone()[0]
info("Schema version", f"v{version}  {'[OK]' if version>=3 else '[!!]'}  (expected=3)")

cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
info("表数量", f"{len(tables)} 张")
info("表列表", ", ".join(tables))

# ── 行数统计 ──
h2("数据量")
for t in ("sessions", "runs", "messages", "checkpoints", "summaries",
          "tool_calls", "approvals", "reviews", "daemon_state"):
    if t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        n = cur.fetchone()[0]
        mark = " *" if n > 0 else ""
        info(f"  {t}", f"{n:>4} rows{mark}")
    else:
        info(f"  {t}", "(missing)")

# ── Sessions 表格 ──
h2("Sessions (最近 5 个)")
if "sessions" in tables:
    cur.execute("PRAGMA table_info(sessions)")
    scols = [r[1] for r in cur.fetchall()]
    cur.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 5")
    rows = cur.fetchall()

    # 列宽：ID 12  状态 20  Run数 6  标题 30
    W_ID = 12; W_ST = 20; W_RUNS = 6; W_TITLE = 30
    print(f"  {pad('ID',W_ID)} {pad('状态',W_ST)} {pad('Run数',W_RUNS,'>')}  标题")
    print(f"  {'─'*W_ID} {'─'*W_ST} {'─'*W_RUNS}  {'─'*W_TITLE}")

    for r in rows:
        d = dict(zip(scols, r))
        j = json.loads(d["data_json"])
        sid = d["id"][-W_ID:]
        status = j.get("status", "?")
        n_runs = len(j.get("run_ids", []))
        title = short(j.get("title", ""), W_TITLE)
        icon = {"closed":"🔴","waiting_for_input":"🟡","active":"🟢"}.get(status,"⚪")
        st_col = f"{icon} {status}"
        print(f"  {pad(sid,W_ID)} {pad(st_col,W_ST)} "
              f"{pad(str(n_runs),W_RUNS,'>')}  {title}")

# ── Runs 表格 ──
h2("Runs (最近 5 个)")
if "runs" in tables:
    cur.execute("PRAGMA table_info(runs)")
    rcols = [r[1] for r in cur.fetchall()]
    cur.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 5")
    rows = cur.fetchall()

    W_RID = 20; W_SESS = 10; W_ST2 = 18; W_STEP = 5; W_REQ = 36
    print(f"  {pad('Run ID',W_RID)} {pad('Session',W_SESS)} "
          f"{pad('状态',W_ST2)} {pad('Step',W_STEP,'>')}  请求内容")
    print(f"  {'─'*W_RID} {'─'*W_SESS} {'─'*W_ST2} {'─'*W_STEP}  {'─'*W_REQ}")

    for r in rows:
        d = dict(zip(rcols, r))
        rid = d["run_id"]
        sess = d["session_id"][-W_SESS:]
        st = d["status"]
        step = str(d["step"] or "?")
        try:
            content = json.loads(d["request_content_json"])
        except Exception:
            content = d.get("request_content_json", "") or ""
        content = short(content.strip('"'), W_REQ)
        icon = {"succeeded":"✅","cancelled":"🚫","failed":"❌",
                "running":"🔄","dispatching":"📡","needs_review":"⚠️"
                }.get(st, "·")
        print(f"  {pad(rid,W_RID)} {pad(sess,W_SESS)} "
              f"{pad(icon+' '+st,W_ST2)} {pad(step,W_STEP,'>')}  {content}")

# ── 关键指标 ──
h2("关键指标")

cur.execute("SELECT value_json FROM daemon_state WHERE key='daemon_epoch'")
row_epoch = cur.fetchone()
if row_epoch:
    current_epoch = row_epoch[0]
    cur.execute(
        "SELECT COUNT(*) FROM runs "
        "WHERE status IN ('running','dispatching') AND owner_epoch != ?",
        (current_epoch,))
    interrupted = cur.fetchone()[0]
    info("daemon epoch", current_epoch[:12] + "...")
    info("中断未恢复 run 数", interrupted)
else:
    info("daemon epoch", "(daemon 还没写过)")

if "summaries" in tables:
    cur.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(summary_text)),0) FROM summaries")
    s = cur.fetchone()
    info("摘要数量 / 总字符", f"{s[0]} / {s[1]}")

if "sessions" in tables:
    cur.execute("SELECT COUNT(*) FROM sessions WHERE workspace LIKE '%kamaclaude - v1%'")
    info("新 workspace sessions", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM sessions WHERE workspace LIKE '%wend%'")
    info("旧 workspace sessions", cur.fetchone()[0])

conn.close()
print()
