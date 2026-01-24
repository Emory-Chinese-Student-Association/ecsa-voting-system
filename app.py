import os
import csv
import re
import time
import sqlite3
import secrets
import datetime
import webbrowser
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, redirect, url_for, render_template_string, send_file, abort, jsonify

try:
    import qrcode
    QR_AVAILABLE = True
except Exception:
    QR_AVAILABLE = False


# =========================
# 0) 配置区
# =========================

def load_candidates_from_csv(filepath: str) -> List[str]:
    """从 CSV 文件读取候选人名单"""
    candidates = []
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("CANDIDATES", "").strip()
                if name:
                    candidates.append(name)
    return candidates


def load_preset_tokens_from_csv(filepath: str) -> List[Dict[str, any]]:
    """从 CSV 文件读取预设 Token 列表"""
    tokens = []
    if filepath and os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                token = row.get("token", "").strip().upper()
                role = row.get("role", "").strip().lower()
                weight = row.get("weight", "").strip()
                note = row.get("note", "").strip()
                if token and role and weight:
                    tokens.append({
                        "token": token,
                        "role": role,
                        "weight": int(weight),
                        "note": note
                    })
    return tokens


@dataclass
class AppConfig:
    # 对外域名路径
    PUBLIC_BASE_URL: str = "https://vote.emorycsa.org/votes"

    # 本地 Flask 监听
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    # 管理员后台密码（强烈建议运行前用环境变量覆盖）
    ADMIN_PASSWORD: str = "CHANGE_ME_STRONG_PASSWORD"

    # 是否运行后自动打开后台页面
    AUTO_OPEN_ADMIN: bool = True

    # 票权
    WEIGHT_CHAIR: int = 5
    WEIGHT_MINISTER: int = 2
    WEIGHT_MEMBER: int = 1

    # 人数配置
    NUM_CHAIR: int = 3
    NUM_MINISTER: int = 5
    NUM_MEMBER: int = 60

    # 候选人 CSV 文件路径
    CANDIDATES_CSV_PATH: str = "candidates.csv"

    # 候选项（从 CSV 加载）
    CANDIDATES: List[str] = field(default_factory=list)

    # 预设 Token CSV 文件路径（如果文件存在且有内容，则使用预设 Token，不自动生成）
    # 设为空字符串或文件不存在时，使用自动生成模式
    PRESET_TOKENS_CSV_PATH: str = "preset_tokens.csv"

    # 数据库与导出文件
    DB_PATH: str = "votes.db"
    EXPORT_DIR: str = "exports"

    # Token 格式
    TOKEN_PREFIX_CHAIR: str = "C"
    TOKEN_PREFIX_MINISTER: str = "M"
    TOKEN_PREFIX_MEMBER: str = "U"
    TOKEN_LENGTH: int = 16


# 创建配置并加载候选人
CONFIG = AppConfig()
CONFIG.CANDIDATES = load_candidates_from_csv(CONFIG.CANDIDATES_CSV_PATH)

# 允许用环境变量覆盖管理员密码
CONFIG.ADMIN_PASSWORD = os.environ.get("ECSA_ADMIN_PASSWORD", CONFIG.ADMIN_PASSWORD)

# 简单检查
if not CONFIG.CANDIDATES or len(CONFIG.CANDIDATES) < 2:
    raise ValueError(f"候选人数量不足。请确保 {CONFIG.CANDIDATES_CSV_PATH} 文件存在且至少包含 2 个候选人。")

os.makedirs(CONFIG.EXPORT_DIR, exist_ok=True)


# =========================
# 1) SQLite 数据库层
# =========================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init() -> None:
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS election_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("SELECT status FROM election_state WHERE id=1")
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO election_state (id, status, updated_at) VALUES (1, ?, ?)",
            ("closed", datetime.datetime.utcnow().isoformat())
        )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            weight INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_at TEXT,
            note TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            token TEXT PRIMARY KEY,
            choice TEXT NOT NULL,
            weight INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(token) REFERENCES tokens(token)
        )
    """)

    conn.commit()
    conn.close()

def get_state() -> str:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT status FROM election_state WHERE id=1")
    status = cur.fetchone()["status"]
    conn.close()
    return status

def set_state(status: str) -> None:
    if status not in ("open", "closed"):
        raise ValueError("status must be 'open' or 'closed'")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE election_state SET status=?, updated_at=? WHERE id=1",
        (status, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def token_exists(token: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM tokens WHERE token=?", (token,))
    ok = cur.fetchone() is not None
    conn.close()
    return ok

def insert_token(token: str, role: str, weight: int, note: str = "") -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO tokens (token, role, weight, used, used_at, note) VALUES (?, ?, ?, 0, NULL, ?)",
        (token, role, weight, note)
    )
    conn.commit()
    conn.close()

def mark_token_used(token: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE tokens SET used=1, used_at=? WHERE token=?",
        (datetime.datetime.utcnow().isoformat(), token)
    )
    conn.commit()
    conn.close()

def get_token_info(token: str) -> Optional[sqlite3.Row]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row

def record_vote(token: str, choice: str, weight: int) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO votes (token, choice, weight, created_at) VALUES (?, ?, ?, ?)",
        (token, choice, weight, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def tally_results_weighted() -> List[Tuple[str, int]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT choice, SUM(weight) AS total_weight
        FROM votes
        GROUP BY choice
        ORDER BY total_weight DESC, choice ASC
    """)
    rows = cur.fetchall()
    conn.close()
    results = [(r["choice"], int(r["total_weight"])) for r in rows]
    existing = {c for c, _ in results}
    for c in CONFIG.CANDIDATES:
        if c not in existing:
            results.append((c, 0))
    
    results.sort(key=lambda x: (-x[1], x[0]))
    return results

def export_tokens_csv(filepath: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT token, role, weight, used, used_at, note FROM tokens ORDER BY role, token")
    rows = cur.fetchall()
    conn.close()

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["token", "role", "weight", "used", "used_at", "note"])
        for r in rows:
            w.writerow([r["token"], r["role"], r["weight"], r["used"], r["used_at"], r["note"]])

def export_votes_csv(filepath: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT v.token, t.role, t.weight AS token_weight, v.choice, v.created_at
        FROM votes v
        JOIN tokens t ON t.token = v.token
        ORDER BY v.created_at ASC
    """)
    rows = cur.fetchall()
    conn.close()

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["token", "role", "token_weight", "choice", "created_at"])
        for r in rows:
            w.writerow([r["token"], r["role"], r["token_weight"], r["choice"], r["created_at"]])


# =========================
# 2) Token 生成逻辑
# =========================

def _random_body(n: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))

def generate_token(prefix: str) -> str:
    return f"{prefix}-{_random_body(CONFIG.TOKEN_LENGTH)}"

def generate_all_tokens() -> Dict[str, List[str]]:
    """
    生成或加载 Token。
    如果存在预设 Token 文件且有内容，则使用预设 Token；
    否则按配置的人数自动生成。
    """
    groups = {
        "chair": [],
        "minister": [],
        "member": []
    }

    # 尝试加载预设 Token
    preset_tokens = load_preset_tokens_from_csv(CONFIG.PRESET_TOKENS_CSV_PATH)

    if preset_tokens:
        # 使用预设 Token 模式
        print(f"  [预设模式] 从 {CONFIG.PRESET_TOKENS_CSV_PATH} 加载 {len(preset_tokens)} 个预设 Token")
        for item in preset_tokens:
            tok = item["token"]
            role = item["role"]
            weight = item["weight"]
            note = item["note"]

            if not token_exists(tok):
                insert_token(tok, role, weight, note=note)

            if role in groups:
                groups[role].append(tok)
            else:
                # 如果角色不在预定义的组中，归入 member
                groups["member"].append(tok)
    else:
        # 自动生成模式
        print(f"  [自动生成模式] 未找到预设 Token 文件或文件为空，按配置生成 Token")

        for i in range(CONFIG.NUM_CHAIR):
            tok = generate_token(CONFIG.TOKEN_PREFIX_CHAIR)
            while token_exists(tok):
                tok = generate_token(CONFIG.TOKEN_PREFIX_CHAIR)
            insert_token(tok, "chair", CONFIG.WEIGHT_CHAIR, note=f"chair_{i+1}")
            groups["chair"].append(tok)

        for i in range(CONFIG.NUM_MINISTER):
            tok = generate_token(CONFIG.TOKEN_PREFIX_MINISTER)
            while token_exists(tok):
                tok = generate_token(CONFIG.TOKEN_PREFIX_MINISTER)
            insert_token(tok, "minister", CONFIG.WEIGHT_MINISTER, note=f"minister_{i+1}")
            groups["minister"].append(tok)

        for i in range(CONFIG.NUM_MEMBER):
            tok = generate_token(CONFIG.TOKEN_PREFIX_MEMBER)
            while token_exists(tok):
                tok = generate_token(CONFIG.TOKEN_PREFIX_MEMBER)
            insert_token(tok, "member", CONFIG.WEIGHT_MEMBER, note=f"member_{i+1}")
            groups["member"].append(tok)

    return groups

def export_generated_tokens_snapshot(groups: Dict[str, List[str]]) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(CONFIG.EXPORT_DIR, f"tokens_generated_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["role", "token", "weight"])
        for tok in groups["chair"]:
            w.writerow(["chair", tok, CONFIG.WEIGHT_CHAIR])
        for tok in groups["minister"]:
            w.writerow(["minister", tok, CONFIG.WEIGHT_MINISTER])
        for tok in groups["member"]:
            w.writerow(["member", tok, CONFIG.WEIGHT_MEMBER])
    return path

def maybe_generate_qr() -> Optional[str]:
    if not QR_AVAILABLE:
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(CONFIG.EXPORT_DIR, f"qr_votes_{ts}.png")
    img = qrcode.make(CONFIG.PUBLIC_BASE_URL)
    img.save(path)
    return path


# =========================
# 3) Flask Web 层
# =========================

app = Flask(__name__)

@app.get("/")
def root_redirect():
    return redirect("/votes")

def require_admin() -> None:
    pw = request.args.get("pw") or request.form.get("pw") or request.headers.get("X-Admin-PW")
    if not pw or pw != CONFIG.ADMIN_PASSWORD:
        abort(401, description="Unauthorized: admin password missing or incorrect.")

BASE_PREFIX = "/votes"

# =========================
# 美化后的模板
# =========================

TEMPLATE_BASE = """
<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} - ECSA 选举</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;600;700&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --primary: #971d21;
      --primary-light: #b52a2f;
      --primary-dark: #6b1518;
      --accent: #d4a574;
      --accent-light: #e8c9a8;
      --bg-gradient-start: #fefafa;
      --bg-gradient-end: #f5e6e6;
      --card-bg: rgba(255, 255, 255, 0.95);
      --text-primary: #2d1a1a;
      --text-secondary: #5c4a4a;
      --text-muted: #8a7676;
      --border-color: rgba(151, 29, 33, 0.1);
      --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
      --shadow-md: 0 4px 20px rgba(151, 29, 33, 0.12);
      --shadow-lg: 0 10px 40px rgba(151, 29, 33, 0.15);
      --radius-sm: 8px;
      --radius-md: 16px;
      --radius-lg: 24px;
    }

    * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }

    body {
      font-family: 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif;
      background: linear-gradient(135deg, var(--bg-gradient-start) 0%, var(--bg-gradient-end) 100%);
      min-height: 100vh;
      color: var(--text-primary);
      line-height: 1.6;
    }

    .page-wrapper {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }

    .header {
      background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
      padding: 2rem 1.5rem;
      text-align: center;
      position: relative;
      overflow: hidden;
    }

    .header::before {
      content: '';
      position: absolute;
      top: -50%;
      left: -50%;
      width: 200%;
      height: 200%;
      background: radial-gradient(circle, rgba(201, 162, 39, 0.1) 0%, transparent 60%);
      animation: shimmer 15s infinite linear;
    }

    @keyframes shimmer {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }

    .header-content {
      position: relative;
      z-index: 1;
    }

    .header h1 {
      font-family: 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 4rem;
      font-weight: 700;
      color: #fff;
      margin-bottom: 0.5rem;
      letter-spacing: 0.05em;
    }

    .header .subtitle {
      color: rgba(255, 255, 255, 0.8);
      font-size: 0.95rem;
      font-weight: 300;
    }

    .container {
      max-width: 680px;
      margin: 0 auto;
      padding: 2rem 1.5rem 3rem;
      flex: 1;
    }

    .card {
      background: var(--card-bg);
      border-radius: var(--radius-md);
      padding: 1.75rem;
      margin-bottom: 1.25rem;
      box-shadow: var(--shadow-md);
      border: 1px solid var(--border-color);
      backdrop-filter: blur(10px);
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }

    .card:hover {
      transform: translateY(-2px);
      box-shadow: var(--shadow-lg);
    }

    .card-title {
      font-size: 1.1rem;
      font-weight: 600;
      color: var(--primary);
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .card-title::before {
      content: '';
      width: 4px;
      height: 1.2em;
      background: linear-gradient(180deg, var(--accent) 0%, var(--accent-light) 100%);
      border-radius: 2px;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 1rem;
      border-radius: 50px;
      font-size: 0.9rem;
      font-weight: 500;
    }

    .status-open {
      background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%);
      color: #166534;
    }

    .status-closed {
      background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
      color: #92400e;
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      animation: pulse 2s infinite;
    }

    .status-open .status-dot {
      background: #22c55e;
    }

    .status-closed .status-dot {
      background: #f59e0b;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }

    .info-text {
      color: var(--text-secondary);
      font-size: 0.95rem;
      margin-bottom: 1.25rem;
    }

    .form-group {
      margin-bottom: 1rem;
    }

    .form-label {
      display: block;
      font-weight: 500;
      color: var(--text-primary);
      margin-bottom: 0.5rem;
      font-size: 0.95rem;
    }

    .form-input, .form-select {
      width: 100%;
      padding: 0.875rem 1rem;
      font-size: 1rem;
      font-family: inherit;
      border: 2px solid var(--border-color);
      border-radius: var(--radius-sm);
      background: #fff;
      color: var(--text-primary);
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }

    .form-input:focus, .form-select:focus {
      outline: none;
      border-color: var(--primary-light);
      box-shadow: 0 0 0 4px rgba(30, 58, 95, 0.1);
    }

    .form-input::placeholder {
      color: var(--text-muted);
    }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      padding: 0.875rem 1.75rem;
      font-size: 1rem;
      font-weight: 600;
      font-family: inherit;
      border: none;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: all 0.2s ease;
      text-decoration: none;
    }

    .btn-primary {
      background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
      color: #fff;
      box-shadow: 0 4px 15px rgba(30, 58, 95, 0.3);
    }

    .btn-primary:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(30, 58, 95, 0.4);
    }

    .btn-secondary {
      background: #fff;
      color: var(--primary);
      border: 2px solid var(--primary);
    }

    .btn-secondary:hover {
      background: var(--primary);
      color: #fff;
    }

    .btn-success {
      background: linear-gradient(135deg, #059669 0%, #10b981 100%);
      color: #fff;
    }

    .btn-warning {
      background: linear-gradient(135deg, #d97706 0%, #f59e0b 100%);
      color: #fff;
    }

    .btn-block {
      width: 100%;
    }

    .role-badge {
      display: inline-flex;
      align-items: center;
      padding: 0.375rem 0.875rem;
      border-radius: 50px;
      font-size: 0.85rem;
      font-weight: 500;
      margin-right: 0.5rem;
    }

    .role-chair {
      background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
      color: #92400e;
    }

    .role-minister {
      background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%);
      color: #1e40af;
    }

    .role-member {
      background: linear-gradient(135deg, #f3e8ff 0%, #e9d5ff 100%);
      color: #6b21a8;
    }

    .weight-badge {
      display: inline-flex;
      align-items: center;
      padding: 0.375rem 0.875rem;
      border-radius: 50px;
      font-size: 0.85rem;
      font-weight: 600;
      background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
      color: #fff;
    }

    .badge-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-bottom: 1rem;
    }

    .candidate-list {
      list-style: none;
    }

    .candidate-item {
      padding: 1rem;
      border: 2px solid var(--border-color);
      border-radius: var(--radius-sm);
      margin-bottom: 0.75rem;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      gap: 1rem;
    }

    .candidate-item:hover {
      border-color: var(--primary-light);
      background: rgba(30, 58, 95, 0.02);
    }

    .candidate-item.selected {
      border-color: var(--primary);
      background: rgba(30, 58, 95, 0.05);
    }

    .candidate-radio {
      width: 22px;
      height: 22px;
      accent-color: var(--primary);
    }

    .candidate-name {
      font-size: 1.05rem;
      font-weight: 500;
      color: var(--text-primary);
    }

    .results-table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
    }

    .results-table th {
      background: var(--primary);
      color: #fff;
      padding: 1rem;
      text-align: left;
      font-weight: 600;
      font-size: 0.9rem;
    }

    .results-table th:first-child {
      border-radius: var(--radius-sm) 0 0 0;
    }

    .results-table th:last-child {
      border-radius: 0 var(--radius-sm) 0 0;
    }

    .results-table td {
      padding: 1rem;
      border-bottom: 1px solid var(--border-color);
    }

    .results-table tr:last-child td {
      border-bottom: none;
    }

    .results-table tr:last-child td:first-child {
      border-radius: 0 0 0 var(--radius-sm);
    }

    .results-table tr:last-child td:last-child {
      border-radius: 0 0 var(--radius-sm) 0;
    }

    .results-table tbody tr {
      transition: background 0.2s ease;
    }

    .results-table tbody tr:hover {
      background: rgba(30, 58, 95, 0.02);
    }

    .progress-bar {
      height: 8px;
      background: var(--border-color);
      border-radius: 4px;
      overflow: hidden;
      margin-top: 0.5rem;
    }

    .progress-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--primary) 0%, var(--accent) 100%);
      border-radius: 4px;
      transition: width 0.5s ease;
    }

    .alert {
      padding: 1rem 1.25rem;
      border-radius: var(--radius-sm);
      margin-bottom: 1.25rem;
      display: flex;
      align-items: flex-start;
      gap: 0.75rem;
    }

    .alert-success {
      background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%);
      color: #166534;
      border: 1px solid #86efac;
    }

    .alert-error {
      background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%);
      color: #991b1b;
      border: 1px solid #fca5a5;
    }

    .alert-warning {
      background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
      color: #92400e;
      border: 1px solid #fcd34d;
    }

    .alert-icon {
      font-size: 1.25rem;
      flex-shrink: 0;
    }

    .help-list {
      list-style: none;
      color: var(--text-secondary);
    }

    .help-list li {
      padding: 0.5rem 0;
      padding-left: 1.5rem;
      position: relative;
    }

    .help-list li::before {
      content: '→';
      position: absolute;
      left: 0;
      color: var(--accent);
      font-weight: bold;
    }

    .link {
      color: var(--primary);
      text-decoration: none;
      font-weight: 500;
      transition: color 0.2s ease;
    }

    .link:hover {
      color: var(--accent);
    }

    .admin-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1rem;
      margin-top: 1rem;
    }

    .footer {
      text-align: center;
      padding: 1.5rem;
      color: var(--text-muted);
      font-size: 0.85rem;
    }

    @media (max-width: 600px) {
      .header h1 {
        font-size: 1.5rem;
      }

      .container {
        padding: 1.25rem 1rem 2rem;
      }

      .card {
        padding: 1.25rem;
      }

      .btn {
        padding: 0.75rem 1.25rem;
      }
    }
  </style>
</head>
<body>
  <div class="page-wrapper">
    <header class="header">
      <div class="header-content">
        <h1>{{ title }}</h1>
        <p class="subtitle">{{ subtitle }}</p>
      </div>
    </header>

    <main class="container">
      {% if msg %}
      <div class="alert alert-{{ msg_type|default('warning') }}">
        <span class="alert-icon">{% if msg_type == 'success' %}✓{% elif msg_type == 'error' %}✕{% else %}!{% endif %}</span>
        <span>{{ msg }}</span>
      </div>
      {% endif %}

      {{ body|safe }}
    </main>

    <footer class="footer">
      Emory Chinese Students Association © 2025
    </footer>
  </div>
</body>
</html>
"""


@app.get(BASE_PREFIX)
def vote_home():
    status = get_state()
    status_class = "status-open" if status == "open" else "status-closed"
    status_text = "投票进行中" if status == "open" else "投票未开放"

    body = f"""
    <div class="card">
      <div class="badge-row">
        <span class="status-badge {status_class}">
          <span class="status-dot"></span>
          {status_text}
        </span>
      </div>
      <p class="info-text">请输入您的专属投票码（Token）进入投票页面。每个投票码仅限使用一次。</p>
      <form method="GET" action="{BASE_PREFIX}/ballot">
        <div class="form-group">
          <label class="form-label">投票码</label>
          <input class="form-input" name="token" placeholder="例如：C-ABCDEFGHJKLMNPQ..." required autocomplete="off">
        </div>
        <button type="submit" class="btn btn-primary btn-block">进入投票</button>
      </form>
    </div>

    <div class="card">
      <div class="card-title">投票须知</div>
      <ul class="help-list">
        <li>每个投票码只能投票一次，提交后无法修改</li>
        <li>投票期间不公开结果，投票结束后统一公布</li>
        <li>请妥善保管您的投票码，切勿泄露给他人</li>
      </ul>
    </div>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="ECSA 内部选举",
        subtitle="Emory Chinese Students Association",
        msg=None,
        body=body
    )


@app.get(BASE_PREFIX + "/ballot")
def ballot():
    status = get_state()
    token = (request.args.get("token") or "").strip().upper()

    if not token:
        return redirect(BASE_PREFIX)

    info = get_token_info(token)
    if info is None:
        return render_template_string(
            TEMPLATE_BASE,
            title="投票",
            subtitle="",
            msg="投票码无效，请检查是否输入正确。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    if info["used"] == 1:
        return render_template_string(
            TEMPLATE_BASE,
            title="投票",
            subtitle="",
            msg="该投票码已使用，无法再次投票。",
            msg_type="warning",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    if status != "open":
        return render_template_string(
            TEMPLATE_BASE,
            title="投票",
            subtitle="",
            msg="当前未开放投票或投票已结束。",
            msg_type="warning",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    role = info["role"]
    weight = info["weight"]

    role_names = {"chair": "主席团", "minister": "部长", "member": "部员"}
    role_class = f"role-{role}"

    candidates_html = ""
    for i, c in enumerate(CONFIG.CANDIDATES):
        candidates_html += f"""
        <label class="candidate-item" onclick="this.querySelector('input').checked = true; document.querySelectorAll('.candidate-item').forEach(el => el.classList.remove('selected')); this.classList.add('selected');">
          <input type="radio" name="choice" value="{c}" class="candidate-radio" required {"checked" if i == 0 else ""}>
          <span class="candidate-name">{c}</span>
        </label>
        """

    body = f"""
    <div class="card">
      <div class="badge-row">
        <span class="role-badge {role_class}">{role_names.get(role, role)}</span>
        <span class="weight-badge">票权 ×{weight}</span>
      </div>
      <p class="info-text">请在下方选择您支持的候选人，提交后不可更改。</p>

      <form method="POST" action="{BASE_PREFIX}/submit">
        <input type="hidden" name="token" value="{token}">

        <div class="form-group">
          <label class="form-label">选择候选人（单选）</label>
          <div class="candidate-list">
            {candidates_html}
          </div>
        </div>

        <button type="submit" class="btn btn-primary btn-block">确认提交</button>
      </form>
    </div>

    <div class="card">
      <div class="card-title">温馨提示</div>
      <p class="info-text" style="margin-bottom: 0;">请确认投票码属于您本人。提交后将无法修改投票结果。</p>
    </div>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="投票",
        subtitle="请谨慎选择",
        msg=None,
        body=body
    )


@app.post(BASE_PREFIX + "/submit")
def submit_vote():
    status = get_state()
    token = (request.form.get("token") or "").strip().upper()
    choice = (request.form.get("choice") or "").strip()

    if status != "open":
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="当前未开放投票或投票已结束。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    info = get_token_info(token)
    if info is None:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="投票码无效。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    if info["used"] == 1:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="该投票码已使用。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    if choice not in CONFIG.CANDIDATES:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="候选项无效。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    try:
        record_vote(token, choice, int(info["weight"]))
        mark_token_used(token)
    except sqlite3.IntegrityError:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="该投票码可能已被使用（重复提交）。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )

    body = f"""
    <div class="card" style="text-align: center; padding: 3rem 2rem;">
      <div style="font-size: 4rem; margin-bottom: 1rem;">🎉</div>
      <h2 style="color: var(--primary); margin-bottom: 1rem;">投票成功！</h2>
      <p class="info-text">感谢您的参与。投票结束后将统一公布结果。</p>
    </div>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="提交成功",
        subtitle="感谢您的参与",
        msg=None,
        body=body
    )


@app.get(BASE_PREFIX + "/results")
def results():
    status = get_state()
    pw = request.args.get("pw") or request.args.get("admin_pw")
    is_admin = (pw == CONFIG.ADMIN_PASSWORD)

    # 投票未结束时，需要管理员密码
    if status != "closed" and not is_admin:
        # 显示密码输入表单
        body = f"""
        <div class="card">
          <div class="card-title">管理员验证</div>
          <p class="info-text">投票尚未结束，查看结果需要输入管理员密码。</p>
          <form method="GET" action="{BASE_PREFIX}/results">
            <div class="form-group">
              <label class="form-label">管理员密码</label>
              <input class="form-input" type="password" name="admin_pw" placeholder="请输入管理员密码" required>
            </div>
            <button type="submit" class="btn btn-primary btn-block">验证并查看</button>
          </form>
        </div>
        <div style="margin-top: 1rem;">
          <a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>
        </div>
        """
        return render_template_string(
            TEMPLATE_BASE,
            title="查看结果",
            subtitle="需要管理员权限",
            msg=None,
            body=body
        )

    results_list = tally_results_weighted()
    total_weight = sum(w for _, w in results_list)
    max_weight = max((w for _, w in results_list), default=1)

    status_text = "已结束" if status == "closed" else "投票中（管理员预览）"

    rows = ""
    for c, w in results_list:
        pct = (w / total_weight * 100) if total_weight > 0 else 0
        bar_width = (w / max_weight * 100) if max_weight > 0 else 0
        rows += f"""
        <tr>
          <td>
            <strong>{c}</strong>
            <div class="progress-bar"><div class="progress-fill" style="width: {bar_width}%"></div></div>
          </td>
          <td style="text-align: right; font-weight: 600;">{w}</td>
          <td style="text-align: right; color: var(--text-muted);">{pct:.1f}%</td>
        </tr>
        """

    body = f"""
    <div class="card">
      <div class="badge-row">
        <span class="status-badge {'status-closed' if status == 'closed' else 'status-open'}">
          <span class="status-dot"></span>
          {status_text}
        </span>
        <span class="weight-badge">总票权 {total_weight}</span>
      </div>

      <table class="results-table">
        <thead>
          <tr>
            <th>候选人</th>
            <th style="text-align: right;">加权票数</th>
            <th style="text-align: right;">占比</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">票权说明</div>
      <p class="info-text" style="margin-bottom: 0;">主席团 ×5 | 部长 ×2 | 部员 ×1</p>
    </div>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="投票结果",
        subtitle="加权统计",
        msg=None,
        body=body
    )


@app.get(BASE_PREFIX + "/admin")
def admin_home():
    require_admin()
    status = get_state()
    status_class = "status-open" if status == "open" else "status-closed"
    status_text = "投票进行中" if status == "open" else "投票已关闭"

    body = f"""
    <div class="card">
      <div class="card-title">当前状态</div>
      <div class="badge-row">
        <span class="status-badge {status_class}">
          <span class="status-dot"></span>
          {status_text}
        </span>
      </div>
      <p class="info-text">建议流程：生成 Token → 开启投票 → 结束投票 → 查看结果</p>
    </div>

    <div class="card">
      <div class="card-title">状态控制</div>
      <div class="admin-grid">
        <form method="POST" action="{BASE_PREFIX}/admin/open">
          <input type="hidden" name="pw" value="{CONFIG.ADMIN_PASSWORD}">
          <button type="submit" class="btn btn-success btn-block">开启投票</button>
        </form>
        <form method="POST" action="{BASE_PREFIX}/admin/close">
          <input type="hidden" name="pw" value="{CONFIG.ADMIN_PASSWORD}">
          <button type="submit" class="btn btn-warning btn-block">结束投票</button>
        </form>
      </div>
    </div>

    <div class="card">
      <div class="card-title">数据导出</div>
      <div class="admin-grid">
        <a href="{BASE_PREFIX}/admin/export_tokens_all?pw={CONFIG.ADMIN_PASSWORD}" class="btn btn-secondary">导出全部 Token</a>
        <a href="{BASE_PREFIX}/admin/export_votes?pw={CONFIG.ADMIN_PASSWORD}" class="btn btn-secondary">导出投票记录</a>
      </div>
    </div>

    <div class="card">
      <div class="card-title">查看结果</div>
      <a href="{BASE_PREFIX}/results" class="btn btn-primary">管理员预览结果</a>
    </div>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="ECSA换届竞选投票管理后台",
        subtitle="Admin Panel",
        msg=None,
        body=body
    )


@app.post(BASE_PREFIX + "/admin/open")
def admin_open():
    require_admin()
    set_state("open")
    return redirect(f"{BASE_PREFIX}/admin?pw={CONFIG.ADMIN_PASSWORD}")


@app.post(BASE_PREFIX + "/admin/close")
def admin_close():
    require_admin()
    set_state("closed")
    return redirect(f"{BASE_PREFIX}/admin?pw={CONFIG.ADMIN_PASSWORD}")


@app.get(BASE_PREFIX + "/admin/export_tokens_all")
def admin_export_tokens_all():
    require_admin()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(CONFIG.EXPORT_DIR, f"tokens_all_{ts}.csv")
    export_tokens_csv(path)
    return send_file(path, as_attachment=True)


@app.get(BASE_PREFIX + "/admin/export_votes")
def admin_export_votes():
    require_admin()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(CONFIG.EXPORT_DIR, f"votes_{ts}.csv")
    export_votes_csv(path)
    return send_file(path, as_attachment=True)


# =========================
# 4) 启动逻辑
# =========================

def main():
    db_init()

    groups = generate_all_tokens()
    snapshot_path = export_generated_tokens_snapshot(groups)
    qr_path = maybe_generate_qr()

    print("\n" + "=" * 50)
    print("  ECSA Voting System Started")
    print("=" * 50)
    print(f"\n  投票入口: {CONFIG.PUBLIC_BASE_URL}")
    print(f"  管理后台: http://127.0.0.1:{CONFIG.PORT}{BASE_PREFIX}/admin?pw=YOUR_PASSWORD")
    print(f"\n  候选人: {', '.join(CONFIG.CANDIDATES)}")
    print(f"  Token 快照: {snapshot_path}")
    if qr_path:
        print(f"  二维码: {qr_path}")
    print("\n" + "=" * 50 + "\n")

    if CONFIG.AUTO_OPEN_ADMIN:
        admin_url = f"http://127.0.0.1:{CONFIG.PORT}{BASE_PREFIX}/admin?pw={CONFIG.ADMIN_PASSWORD}"
        try:
            webbrowser.open(admin_url, new=2)
        except Exception:
            pass

    app.run(host=CONFIG.HOST, port=CONFIG.PORT, debug=False)


if __name__ == "__main__":
    main()