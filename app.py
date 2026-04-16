import os
import csv
import html
import sqlite3
import secrets
import datetime
import time
import webbrowser
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

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

@dataclass
class BallotCategory:
    key: str
    label: str
    max_choices: int
    candidates: List[str]
    role_weights: Dict[str, int]


def _find_first_nonempty(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key, "")
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def load_ballot_rules_from_csv(filepath: str) -> List[Dict[str, Any]]:
    """从 CSV 文件读取投票类别与每个类别要求选择的人数"""
    rules: List[Dict[str, Any]] = []
    if not os.path.exists(filepath):
        return rules

    seen_keys = set()
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            key = _find_first_nonempty(row, "category_key", "CATEGORY_KEY", "category", "CATEGORY")
            label = _find_first_nonempty(row, "category_label", "CATEGORY_LABEL", "label", "LABEL") or key
            max_choices_text = _find_first_nonempty(
                row,
                "max_choices",
                "MAX_CHOICES",
                "max_votes",
                "MAX_VOTES",
                "max_select",
                "MAX_SELECT",
            )
            chair_weight_text = _find_first_nonempty(row, "chair_weight", "CHAIR_WEIGHT")
            minister_weight_text = _find_first_nonempty(row, "minister_weight", "MINISTER_WEIGHT")
            member_weight_text = _find_first_nonempty(row, "member_weight", "MEMBER_WEIGHT")

            if not key and not label and not max_choices_text and not chair_weight_text and not minister_weight_text and not member_weight_text:
                continue
            if not key:
                raise ValueError(f"{filepath} 第 {idx} 行缺少 category_key。")
            if key in seen_keys:
                raise ValueError(f"{filepath} 中存在重复的 category_key: {key}")
            if not max_choices_text:
                raise ValueError(f"{filepath} 第 {idx} 行缺少 max_choices。")

            try:
                max_choices = int(max_choices_text)
            except ValueError as exc:
                raise ValueError(f"{filepath} 第 {idx} 行的 max_choices 必须是整数。") from exc

            if max_choices < 1:
                raise ValueError(f"{filepath} 第 {idx} 行的 max_choices 必须大于等于 1。")

            role_weights: Dict[str, int] = {}
            for role, weight_text, field_name in (
                ("chair", chair_weight_text, "chair_weight"),
                ("minister", minister_weight_text, "minister_weight"),
                ("member", member_weight_text, "member_weight"),
            ):
                if not weight_text:
                    continue
                try:
                    weight_value = int(weight_text)
                except ValueError as exc:
                    raise ValueError(f"{filepath} 第 {idx} 行的 {field_name} 必须是整数。") from exc
                if weight_value < 1:
                    raise ValueError(f"{filepath} 第 {idx} 行的 {field_name} 必须大于等于 1。")
                role_weights[role] = weight_value

            rules.append({
                "key": key,
                "label": label,
                "max_choices": max_choices,
                "role_weights": role_weights,
            })
            seen_keys.add(key)

    return rules


def load_candidates_from_csv(filepath: str) -> Dict[str, List[str]]:
    """从 CSV 文件按类别读取候选人名单"""
    candidates_by_category: Dict[str, List[str]] = {}
    seen_names: Dict[str, set] = {}
    if not os.path.exists(filepath):
        return candidates_by_category

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            category_key = _find_first_nonempty(row, "category_key", "CATEGORY_KEY", "category", "CATEGORY")
            candidate_name = _find_first_nonempty(
                row,
                "candidate",
                "CANDIDATE",
                "candidate_name",
                "CANDIDATE_NAME",
                "name",
                "NAME",
                "CANDIDATES",
            )

            if not category_key and not candidate_name:
                continue
            if not category_key:
                raise ValueError(f"{filepath} 第 {idx} 行缺少 category_key。")
            if not candidate_name:
                raise ValueError(f"{filepath} 第 {idx} 行缺少 candidate。")

            if category_key not in candidates_by_category:
                candidates_by_category[category_key] = []
                seen_names[category_key] = set()

            if candidate_name in seen_names[category_key]:
                raise ValueError(f"{filepath} 第 {idx} 行存在重复候选人: {candidate_name}")

            candidates_by_category[category_key].append(candidate_name)
            seen_names[category_key].add(candidate_name)

    return candidates_by_category


def load_ballot_categories(
    candidates_filepath: str,
    ballot_rules_filepath: str,
    default_role_weights: Dict[str, int],
) -> List[BallotCategory]:
    """组合候选人 CSV 和投票规则 CSV，生成最终的投票类别配置"""
    ballot_rules = load_ballot_rules_from_csv(ballot_rules_filepath)
    candidates_by_category = load_candidates_from_csv(candidates_filepath)

    if not ballot_rules:
        return []

    known_categories = {rule["key"] for rule in ballot_rules}
    extra_categories = sorted(set(candidates_by_category) - known_categories)
    if extra_categories:
        raise ValueError(
            f"{candidates_filepath} 中存在未在 {ballot_rules_filepath} 配置的类别: {', '.join(extra_categories)}"
        )

    categories: List[BallotCategory] = []
    for rule in ballot_rules:
        candidates = candidates_by_category.get(rule["key"], [])
        if not candidates:
            raise ValueError(f"类别 {rule['key']} 没有候选人，请检查 {candidates_filepath}。")
        if rule["max_choices"] > len(candidates):
            raise ValueError(
                f"类别 {rule['key']} 要求每人必须选择 {rule['max_choices']} 人，但当前只有 {len(candidates)} 位候选人。"
            )
        categories.append(
            BallotCategory(
                key=rule["key"],
                label=rule["label"],
                max_choices=rule["max_choices"],
                candidates=candidates,
                role_weights={**default_role_weights, **rule["role_weights"]},
            )
        )

    return categories


def load_preset_tokens_from_csv(filepath: str) -> List[Dict[str, Any]]:
    """从 CSV 文件读取预设 Token 列表"""
    tokens = []
    if filepath and os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                token = row.get("token", "").strip().upper()
                role = row.get("role", "").strip().lower()
                note = row.get("note", "").strip()
                if token and role:
                    tokens.append({
                        "token": token,
                        "role": role,
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
    ADMIN_PASSWORD: str = "Emorycsa123456$"

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

    # 投票规则 CSV 文件路径
    BALLOT_RULES_CSV_PATH: str = "ballot_rules.csv"

    # 投票类别（从 CSV 加载）
    BALLOT_CATEGORIES: List[BallotCategory] = field(default_factory=list)

    # 预设 Token CSV 文件路径（如果文件存在且有内容，则使用预设 Token，不自动生成）
    # 设为空字符串或文件不存在时，使用自动生成模式
    PRESET_TOKENS_CSV_PATH: str = "preset_tokens.csv"

    # 数据库与导出文件
    DB_PATH: str = "votes.db"
    EXPORT_DIR: str = "exports"
    SQLITE_TIMEOUT_SECONDS: float = 30.0
    SQLITE_BUSY_TIMEOUT_MS: int = 30000
    SQLITE_LOCK_RETRIES: int = 3
    SQLITE_LOCK_RETRY_DELAY_SECONDS: float = 0.1

    # Token 格式
    TOKEN_PREFIX_CHAIR: str = "C"
    TOKEN_PREFIX_MINISTER: str = "M"
    TOKEN_PREFIX_MEMBER: str = "U"
    TOKEN_LENGTH: int = 16


# 创建配置并加载候选人
CONFIG = AppConfig()
CONFIG.BALLOT_CATEGORIES = load_ballot_categories(
    CONFIG.CANDIDATES_CSV_PATH,
    CONFIG.BALLOT_RULES_CSV_PATH,
    {
        "chair": CONFIG.WEIGHT_CHAIR,
        "minister": CONFIG.WEIGHT_MINISTER,
        "member": CONFIG.WEIGHT_MEMBER,
    },
)

# 允许用环境变量覆盖管理员密码
CONFIG.ADMIN_PASSWORD = os.environ.get("ECSA_ADMIN_PASSWORD", CONFIG.ADMIN_PASSWORD)

# 简单检查
if not CONFIG.BALLOT_CATEGORIES:
    raise ValueError(
        f"未加载到投票类别。请确保 {CONFIG.CANDIDATES_CSV_PATH} 和 {CONFIG.BALLOT_RULES_CSV_PATH} 配置正确。"
    )

os.makedirs(CONFIG.EXPORT_DIR, exist_ok=True)


# =========================
# 1) SQLite 数据库层
# =========================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        CONFIG.DB_PATH,
        check_same_thread=False,
        timeout=CONFIG.SQLITE_TIMEOUT_SECONDS,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {CONFIG.SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def db_init() -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")

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

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='votes'")
    votes_table_exists = cur.fetchone() is not None
    if votes_table_exists:
        cur.execute("PRAGMA table_info(votes)")
        existing_columns = {row["name"] for row in cur.fetchall()}
        expected_columns = {"id", "token", "category_key", "category_label", "choice", "weight", "created_at"}
        if existing_columns != expected_columns:
            backup_name = f"votes_legacy_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            cur.execute(f"ALTER TABLE votes RENAME TO {backup_name}")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            category_key TEXT NOT NULL,
            category_label TEXT NOT NULL,
            choice TEXT NOT NULL,
            weight INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(token) REFERENCES tokens(token),
            UNIQUE(token, category_key, choice)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_category_choice ON votes(category_key, choice)")

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

def get_token_info(token: str) -> Optional[sqlite3.Row]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    return row

def get_ballot_categories_by_key() -> Dict[str, BallotCategory]:
    return {category.key: category for category in CONFIG.BALLOT_CATEGORIES}


def default_role_weight(role: str) -> int:
    return {
        "chair": CONFIG.WEIGHT_CHAIR,
        "minister": CONFIG.WEIGHT_MINISTER,
        "member": CONFIG.WEIGHT_MEMBER,
    }.get(role, CONFIG.WEIGHT_MEMBER)


def role_display_name(role: str) -> str:
    return {"chair": "主席团", "minister": "部长", "member": "部员"}.get(role, role)


def format_role_weights(role_weights: Dict[str, int]) -> str:
    return " | ".join(
        f"{role_display_name(role)} ×{role_weights[role]}"
        for role in ("chair", "minister", "member")
        if role in role_weights
    )


def resolve_vote_weight(category: BallotCategory, role: str, fallback_weight: int) -> int:
    return int(category.role_weights.get(role, fallback_weight))


def submit_ballot(token: str, role: str, selections: Dict[str, List[str]], fallback_weight: int) -> None:
    timestamp = datetime.datetime.utcnow().isoformat()
    rows_to_insert = []
    categories_by_key = get_ballot_categories_by_key()

    for category_key, choices in selections.items():
        category = categories_by_key[category_key]
        applied_weight = resolve_vote_weight(category, role, fallback_weight)
        for choice in choices:
            rows_to_insert.append((token, category.key, category.label, choice, applied_weight, timestamp))

    for attempt in range(CONFIG.SQLITE_LOCK_RETRIES):
        conn = db_connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT used FROM tokens WHERE token=?", (token,))
            token_row = cur.fetchone()
            if token_row is None:
                raise ValueError("invalid_token")
            if int(token_row["used"]) == 1:
                raise RuntimeError("token_used")

            cur.executemany(
                """
                INSERT INTO votes (token, category_key, category_label, choice, weight, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            cur.execute(
                "UPDATE tokens SET used=1, used_at=? WHERE token=? AND used=0",
                (timestamp, token),
            )
            if cur.rowcount != 1:
                raise RuntimeError("token_used")
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            conn.rollback()
            is_locked_error = "locked" in str(exc).lower()
            if is_locked_error and attempt < CONFIG.SQLITE_LOCK_RETRIES - 1:
                time.sleep(CONFIG.SQLITE_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def tally_results_weighted() -> List[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT category_key, category_label, choice, SUM(weight) AS total_weight
        FROM votes
        GROUP BY category_key, category_label, choice
        ORDER BY category_label ASC, total_weight DESC, choice ASC
    """)
    rows = cur.fetchall()
    conn.close()

    tallies_by_category: Dict[str, Dict[str, int]] = {}
    for row in rows:
        tallies_by_category.setdefault(row["category_key"], {})[row["choice"]] = int(row["total_weight"])

    sections: List[Dict[str, Any]] = []
    for category in CONFIG.BALLOT_CATEGORIES:
        category_results = [
            (candidate, tallies_by_category.get(category.key, {}).get(candidate, 0))
            for candidate in category.candidates
        ]
        category_results.sort(key=lambda item: (-item[1], item[0]))
        total_weight = sum(weight for _, weight in category_results)
        max_weight = max((weight for _, weight in category_results), default=0)
        sections.append({
            "category": category,
            "results": category_results,
            "total_weight": total_weight,
            "max_weight": max_weight,
        })

    return sections


def summarize_public_results() -> List[Dict[str, Any]]:
    sections = []
    for section in tally_results_weighted():
        category = section["category"]
        winners_count = min(category.max_choices, len(section["results"]))
        winners = []
        if section["total_weight"] > 0:
            winners = [candidate for candidate, _ in section["results"][:winners_count]]
        sections.append({
            "category": category,
            "winners": winners,
            "has_votes": section["total_weight"] > 0,
        })
    return sections

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
        SELECT
            v.token,
            t.role,
            t.weight AS token_default_weight,
            v.weight AS applied_vote_weight,
            v.category_key,
            v.category_label,
            v.choice,
            v.created_at
        FROM votes v
        JOIN tokens t ON t.token = v.token
        ORDER BY v.created_at ASC, v.category_key ASC, v.choice ASC
    """)
    rows = cur.fetchall()
    conn.close()

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "token",
            "role",
            "token_default_weight",
            "applied_vote_weight",
            "category_key",
            "category_label",
            "choice",
            "created_at",
        ])
        for r in rows:
            w.writerow([
                r["token"],
                r["role"],
                r["token_default_weight"],
                r["applied_vote_weight"],
                r["category_key"],
                r["category_label"],
                r["choice"],
                r["created_at"],
            ])


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
            note = item["note"]
            weight = default_role_weight(role)

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


def category_field_name(category: BallotCategory) -> str:
    return f"choice_{category.key}"


def render_ballot_sections_html(role: str, fallback_weight: int) -> str:
    sections_html = ""
    for category in CONFIG.BALLOT_CATEGORIES:
        current_role_weight = resolve_vote_weight(category, role, fallback_weight)
        candidates_html = ""
        for candidate in category.candidates:
            escaped_candidate = html.escape(candidate)
            candidates_html += f"""
            <label class="candidate-item" data-candidate-item>
              <input
                type="checkbox"
                name="{html.escape(category_field_name(category))}"
                value="{escaped_candidate}"
                class="candidate-checkbox"
              >
              <span class="candidate-name">{escaped_candidate}</span>
            </label>
            """

        sections_html += f"""
        <div class="ballot-section" data-ballot-section data-max-choices="{category.max_choices}">
          <div class="section-header">
            <div>
              <div class="section-title">{html.escape(category.label)}</div>
              <p class="section-help">本类别必须选择 {category.max_choices} 人。您当前在本类别的票权为 ×{current_role_weight}。</p>
              <p class="section-meta">{html.escape(format_role_weights(category.role_weights))}</p>
            </div>
            <span class="section-counter" data-selection-counter>0 / {category.max_choices}</span>
          </div>
          <div class="candidate-list">
            {candidates_html}
          </div>
        </div>
        """

    return sections_html


def collect_ballot_selections(form) -> Dict[str, List[str]]:
    selections: Dict[str, List[str]] = {}
    for category in CONFIG.BALLOT_CATEGORIES:
        raw_choices = [choice.strip() for choice in form.getlist(category_field_name(category)) if choice.strip()]
        deduped_choices = []
        seen = set()
        for choice in raw_choices:
            if choice in seen:
                continue
            seen.add(choice)
            deduped_choices.append(choice)

        invalid_choices = [choice for choice in deduped_choices if choice not in category.candidates]
        if invalid_choices:
            raise ValueError(f"“{category.label}”中包含无效候选项。")

        if len(deduped_choices) != category.max_choices:
            raise ValueError(
                f"“{category.label}”必须选择 {category.max_choices} 位候选人，当前已选择 {len(deduped_choices)} 位。"
            )

        selections[category.key] = deduped_choices

    return selections


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

    .btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
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

    .ballot-section + .ballot-section {
      margin-top: 1.5rem;
      padding-top: 1.5rem;
      border-top: 1px solid var(--border-color);
    }

    .section-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;
      margin-bottom: 1rem;
    }

    .section-title {
      color: var(--primary);
      font-size: 1.05rem;
      font-weight: 700;
      margin-bottom: 0.35rem;
    }

    .section-help {
      color: var(--text-secondary);
      font-size: 0.92rem;
    }

    .section-meta {
      color: var(--text-muted);
      font-size: 0.84rem;
      margin-top: 0.35rem;
    }

    .section-counter {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 72px;
      padding: 0.375rem 0.875rem;
      border-radius: 999px;
      background: rgba(151, 29, 33, 0.08);
      color: var(--primary);
      font-size: 0.85rem;
      font-weight: 700;
      white-space: nowrap;
    }

    .section-counter.complete {
      background: rgba(5, 150, 105, 0.12);
      color: #047857;
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

    .candidate-item.disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }

    .candidate-item.disabled:hover {
      border-color: var(--border-color);
      background: transparent;
    }

    .candidate-radio,
    .candidate-checkbox {
      width: 22px;
      height: 22px;
      accent-color: var(--primary);
      flex-shrink: 0;
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

    .public-result-list {
      display: grid;
      gap: 0.75rem;
    }

    .public-result-item {
      display: flex;
      align-items: center;
      gap: 0.875rem;
      padding: 0.95rem 1rem;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-sm);
      background: linear-gradient(135deg, rgba(30, 58, 95, 0.03) 0%, rgba(209, 164, 108, 0.08) 100%);
    }

    .public-result-rank {
      width: 32px;
      height: 32px;
      border-radius: 999px;
      background: var(--primary);
      color: #fff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      flex-shrink: 0;
    }

    .public-result-name {
      font-size: 1rem;
      font-weight: 600;
      color: var(--text-primary);
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

      .section-header {
        flex-direction: column;
        align-items: stretch;
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
      Emory Chinese Student Association © 2025
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
      <p class="info-text">请输入您的专属投票码（Token）进入投票页面。每个投票码仅限使用一次，进入后可按类别完成整张选票。</p>
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
        <li>每个投票码只能提交一次整张选票，提交后无法修改</li>
        <li>每个类别都必须按页面提示的人数投满</li>
        <li>投票期间不公开结果，投票结束后统一公布</li>
        <li>请妥善保管您的投票码，切勿泄露给他人</li>
      </ul>
    </div>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="ECSA 内部选举",
        subtitle="Emory Chinese Student Association",
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
    fallback_weight = int(info["weight"])

    role_class = f"role-{role}"
    ballot_sections_html = render_ballot_sections_html(role, fallback_weight)

    body = f"""
    <div class="card">
      <div class="badge-row">
        <span class="role-badge {role_class}">{role_display_name(role)}</span>
        <span class="weight-badge">按类别分别计权</span>
      </div>
      <p class="info-text">请按类别选择您支持的候选人。每个类别都必须选满页面要求的人数；不同类别会使用不同权重规则，提交后不可更改。</p>

      <form method="POST" action="{BASE_PREFIX}/submit">
        <input type="hidden" name="token" value="{html.escape(token)}">
        {ballot_sections_html}

        <button type="submit" class="btn btn-primary btn-block" id="submit-ballot-btn" disabled>确认提交</button>
      </form>
    </div>

    <div class="card">
      <div class="card-title">温馨提示</div>
      <p class="info-text" style="margin-bottom: 0;">请确认投票码属于您本人。系统会按类别分别计票，并按照该类别对应的身份权重进行加权。</p>
    </div>

    <script>
      document.querySelectorAll('[data-ballot-section]').forEach((section) => {{
        const maxChoices = Number(section.dataset.maxChoices || '1');
        const checkboxes = Array.from(section.querySelectorAll('input[type="checkbox"]'));
        const counter = section.querySelector('[data-selection-counter]');
        const submitButton = document.getElementById('submit-ballot-btn');

        const syncState = () => {{
          let allComplete = true;
          document.querySelectorAll('[data-ballot-section]').forEach((currentSection) => {{
            const requiredCount = Number(currentSection.dataset.maxChoices || '1');
            const currentSelected = currentSection.querySelectorAll('input[type="checkbox"]:checked').length;
            const currentCounter = currentSection.querySelector('[data-selection-counter]');
            if (currentCounter) {{
              currentCounter.classList.toggle('complete', currentSelected === requiredCount);
            }}
            if (currentSelected !== requiredCount) {{
              allComplete = false;
            }}
          }});

          const selected = checkboxes.filter((checkbox) => checkbox.checked);
          if (counter) {{
            counter.textContent = `${{selected.length}} / ${{maxChoices}}`;
          }}

          checkboxes.forEach((checkbox) => {{
            const item = checkbox.closest('.candidate-item');
            const shouldDisable = !checkbox.checked && selected.length >= maxChoices;
            checkbox.disabled = shouldDisable;
            if (item) {{
              item.classList.toggle('selected', checkbox.checked);
              item.classList.toggle('disabled', shouldDisable);
            }}
          }});

          if (submitButton) {{
            submitButton.disabled = !allComplete;
          }}
        }};

        checkboxes.forEach((checkbox) => checkbox.addEventListener('change', syncState));
        syncState();
      }});
    </script>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="投票",
        subtitle="按类别完成整张选票",
        msg=None,
        body=body
    )


@app.post(BASE_PREFIX + "/submit")
def submit_vote():
    status = get_state()
    token = (request.form.get("token") or "").strip().upper()

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

    try:
        selections = collect_ballot_selections(request.form)
    except ValueError as exc:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg=str(exc),
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}/ballot?token={html.escape(token)}" class="btn btn-secondary">← 返回投票页</a>'
        )

    try:
        submit_ballot(token, info["role"], selections, int(info["weight"]))
    except RuntimeError:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="该投票码可能已被使用（重复提交）。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )
    except ValueError:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="投票码无效。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )
    except sqlite3.IntegrityError:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="该投票码可能已被使用（重复提交）。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>'
        )
    except sqlite3.OperationalError:
        return render_template_string(
            TEMPLATE_BASE,
            title="提交失败",
            subtitle="",
            msg="系统当前较忙，请稍后重试。",
            msg_type="error",
            body=f'<a href="{BASE_PREFIX}/ballot?token={html.escape(token)}" class="btn btn-secondary">← 返回投票页</a>'
        )

    body = f"""
    <div class="card" style="text-align: center; padding: 3rem 2rem;">
      <div style="font-size: 4rem; margin-bottom: 1rem;">🎉</div>
      <h2 style="color: var(--primary); margin-bottom: 1rem;">投票成功！</h2>
      <p class="info-text">感谢您的参与。您本次已完成全部类别投票，投票结束后将统一公布结果。</p>
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

    result_sections = tally_results_weighted()

    status_text = "已结束" if status == "closed" else "投票中（管理员预览）"
    sections_html = ""
    for section in result_sections:
        category = section["category"]
        rows = ""
        for candidate, weighted_votes in section["results"]:
            pct = (weighted_votes / section["total_weight"] * 100) if section["total_weight"] > 0 else 0
            bar_width = (weighted_votes / section["max_weight"] * 100) if section["max_weight"] > 0 else 0
            rows += f"""
            <tr>
              <td>
                <strong>{html.escape(candidate)}</strong>
                <div class="progress-bar"><div class="progress-fill" style="width: {bar_width}%"></div></div>
              </td>
              <td style="text-align: right; font-weight: 600;">{weighted_votes}</td>
              <td style="text-align: right; color: var(--text-muted);">{pct:.1f}%</td>
            </tr>
            """

        sections_html += f"""
        <div class="card">
          <div class="badge-row">
            <span class="weight-badge">{html.escape(category.label)}</span>
            <span class="role-badge role-chair">每人须投 {category.max_choices} 人</span>
            <span class="role-badge role-member">本类别总加权票数 {section["total_weight"]}</span>
          </div>
          <p class="info-text">权重规则：{html.escape(format_role_weights(category.role_weights))}</p>

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
        """

    body = f"""
    <div class="card">
      <div class="badge-row">
        <span class="status-badge {'status-closed' if status == 'closed' else 'status-open'}">
          <span class="status-dot"></span>
          {status_text}
        </span>
        <span class="weight-badge">{len(result_sections)} 个投票类别</span>
      </div>
      <p class="info-text" style="margin-bottom: 0;">结果按类别分别统计。同一选民在某个类别中选择多位候选人时，每位候选人都会获得该类别下对应身份的完整票权。</p>
    </div>

    {sections_html}

    <div class="card">
      <div class="card-title">票权说明</div>
      <p class="info-text" style="margin-bottom: 0;">各类别的权重规则已在上方对应板块展示，请以每个类别下方的“权重规则”为准。</p>
    </div>
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="投票结果",
        subtitle="按类别加权统计",
        msg=None,
        body=body
    )


@app.get(BASE_PREFIX + "/final-results")
def final_results():
    status = get_state()
    if status != "closed":
        body = f"""
        <div class="card">
          <div class="card-title">最终结果尚未公布</div>
          <p class="info-text">投票结束后，系统会在这里公开最终结果。本页面不需要管理员密码，也不会显示具体票数。</p>
        </div>
        <div style="margin-top: 1rem;">
          <a href="{BASE_PREFIX}" class="btn btn-secondary">← 返回首页</a>
        </div>
        """
        return render_template_string(
            TEMPLATE_BASE,
            title="最终结果",
            subtitle="投票结束后公开",
            msg=None,
            body=body
        )

    public_sections = summarize_public_results()
    sections_html = ""
    for section in public_sections:
        category = section["category"]
        if section["has_votes"]:
            winners_html = ""
            for idx, candidate in enumerate(section["winners"], start=1):
                winners_html += f"""
                <div class="public-result-item">
                  <span class="public-result-rank">{idx}</span>
                  <span class="public-result-name">{html.escape(candidate)}</span>
                </div>
                """
            content_html = f"""
            <div class="public-result-list">
              {winners_html}
            </div>
            """
        else:
            content_html = '<p class="info-text" style="margin-bottom: 0;">该类别暂无有效投票记录。</p>'

        sections_html += f"""
        <div class="card">
          <div class="badge-row">
            <span class="weight-badge">{html.escape(category.label)}</span>
            <span class="role-badge role-member">公布结果 {category.max_choices} 人</span>
          </div>
          {content_html}
        </div>
        """

    body = f"""
    <div class="card">
      <div class="badge-row">
        <span class="status-badge status-closed">
          <span class="status-dot"></span>
          投票已结束
        </span>
        <span class="weight-badge">{len(public_sections)} 个投票类别</span>
      </div>
      <p class="info-text" style="margin-bottom: 0;">本页面仅公布各类别最终结果，不展示具体票数或统计明细。</p>
    </div>

    {sections_html}
    """
    return render_template_string(
        TEMPLATE_BASE,
        title="最终结果",
        subtitle="公开结果页面",
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
      <div class="admin-grid">
        <a href="{BASE_PREFIX}/results?pw={CONFIG.ADMIN_PASSWORD}" class="btn btn-primary">管理员预览结果</a>
        <a href="{BASE_PREFIX}/final-results" class="btn btn-secondary">最终查看结果</a>
      </div>
      <p class="info-text">最终查看结果页在投票结束后开放，不需要管理员密码，且不显示具体票数。</p>
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
    print("\n  投票类别配置:")
    for category in CONFIG.BALLOT_CATEGORIES:
        print(
            f"    - {category.label}: 候选人 {', '.join(category.candidates)} | "
            f"每人须投 {category.max_choices} 人 | 权重 {format_role_weights(category.role_weights)}"
        )
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

    app.run(host=CONFIG.HOST, port=CONFIG.PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
