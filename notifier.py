"""琉球ゴールデンキングス 試合速報 → ntfy 通知

GitHub Actions から1時間おきに起動される。
- 今日キングスの試合があれば、試合開始5分前から終了まで30秒おきにスコアを確認
- ティップオフ / 各クォーター終了 / 4Q・延長の逆転・同点 / 試合終了 を ntfy に送る
- 試合開始前(またはティップオフ時)にキングスのスターティング5を送る
- クォーター終了時はキングスの得点上位3人、試合終了時は得点した全員の得点を添える

手動テスト:
  python notifier.py --test            # テスト通知を送り、次の試合を表示
  python notifier.py --key 5938        # 指定試合ページの読み取り結果を表示(通知なし)
  python notifier.py --box 5938        # 指定試合の選手別得点を読み取って表示(通知なし)
"""
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
KINGS_ID = 701
KINGS_NAME = "琉球"
BASE = "https://www.bleague.jp"
SCHEDULE_URL = BASE + "/schedule/?tab=1&club={club}"
DETAIL_URL = BASE + "/game_detail/?ScheduleKey={key}&tab=1"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

POLL_SEC = 30
START_WINDOW_MIN = 75         # この時間内に試合があれば、その起動で待機して担当する
WATCH_LEAD_MIN = 5            # 見張りを始めるのは試合開始の何分前か
MAX_RUN = timedelta(hours=5, minutes=40)  # GitHub Actions の6時間制限より手前で止める
FALLBACK_UPDATE_MIN = 15      # クォーターが読み取れない時の定期スコア通知間隔

HEADERS = {"User-Agent": "kings-score-notifier (personal use)"}


# ---------------------------------------------------------------- 取得・解析
def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def text_lines(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]


def schedule_keys(html):
    keys = re.findall(r"game_detail/\?ScheduleKey=(\d+)", html)
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


SCORE_RE = re.compile(r"^(\d{1,3}|--|-)$")
DATE_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})")
TIP_RE = re.compile(r"(\d{1,2}):(\d{2})\s*TIP\s*OFF", re.I)


@dataclass
class Game:
    key: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    tipoff: datetime | None
    status: str | None      # 生の状況表示(FINAL, 2Q 等)
    period: str | None      # 正規化: Q1..Q4, HALF, OT1.., FINAL

    @property
    def started(self):
        return self.home_score is not None and self.away_score is not None

    @property
    def kings_home(self):
        return KINGS_NAME in self.home

    @property
    def opp(self):
        return self.away if self.kings_home else self.home

    @property
    def kings_pts(self):
        return self.home_score if self.kings_home else self.away_score

    @property
    def opp_pts(self):
        return self.away_score if self.kings_home else self.home_score

    def scoreline(self):
        return f"琉球 {self.kings_pts} - {self.opp_pts} {self.opp}"

    def leader(self):
        d = self.kings_pts - self.opp_pts
        return "kings" if d > 0 else "opp" if d < 0 else "tie"


def normalize_period(s):
    if not s:
        return None
    u = s.upper().replace("　", " ")
    if "FINAL" in u or "試合終了" in s or u.strip() == "F":
        return "FINAL"
    if "HALF" in u or "ハーフ" in s:
        return "HALF"
    m = re.search(r"OT\s*(\d*)|延長\s*(\d*)", u)
    if m:
        n = m.group(1) or m.group(2) or "1"
        return f"OT{n}"
    m = re.search(r"([1-4])\s*(?:Q|ST|ND|RD|TH)|第\s*([1-4])\s*Q", u)
    if m:
        return f"Q{m.group(1) or m.group(2)}"
    return None


LEAGUE_RE = re.compile(r"^(B\.?[A-Z0-9]*|B[123]|B\.(PREMIER|ONE|NEXT)|ホーム|アウェイ)$", re.I)


def _team_before(L, idx, stop):
    """HOME/AWAY の行から上にさかのぼり、(正式名, 略称, 得点) を返す。"""
    score, names = "--", []
    for j in range(idx - 1, stop, -1):
        x = L[j]
        if SCORE_RE.match(x):
            if not names and score == "--":
                score = x
            continue
        if "位" in x or "｜" in x or "|" in x or LEAGUE_RE.match(x):
            continue
        names.append(x)
        if len(names) == 2:
            break
    if len(names) < 2:
        raise ValueError("チーム名が見つからない")
    short, full = names
    return full, short, score


def parse_game(key, html):
    L = text_lines(html)
    di = next((i for i, l in enumerate(L) if DATE_RE.match(l)), None)
    if di is None:
        raise ValueError("試合日の行が見つからない")
    y, mo, d = map(int, DATE_RE.match(L[di]).groups())

    hi = next(i for i in range(di, len(L)) if L[i] == "HOME")
    wi = next(i for i in range(hi + 1, len(L)) if L[i] == "AWAY")
    home_full, home_short, home_score = _team_before(L, hi, di)

    tip = None
    for i in range(hi, wi):
        if re.search(r"TIP\s*OFF", L[i], re.I):
            m = re.search(r"(\d{1,2}):(\d{2})", L[i]) or re.search(r"(\d{1,2}):(\d{2})", L[i - 1])
            if m:
                tip = datetime(y, mo, d, int(m.group(1)), int(m.group(2)), tzinfo=JST)
            break
    stop = next((i for i in range(wi - 1, hi, -1) if re.search(r"TIP\s*OFF|^VS$", L[i], re.I)), hi)
    away_full, away_short, away_score = _team_before(L, wi, stop)

    # スコアチャート欄: 「ホーム略称 / x / 状況 / y / アウェイ略称」
    status = None
    for i in range(wi + 1, len(L)):
        if L[i] == home_short:
            win = L[i + 1:i + 7]
            if away_short in win:
                mid = [x for x in win[:win.index(away_short)] if not SCORE_RE.match(x)]
                if mid:
                    status = mid[0]
                    break
    period = normalize_period(status)
    if period is None:  # 念のためヘッダー周辺からも探す
        for l in L[di:wi + 1]:
            p = normalize_period(l)
            if p and not re.search(r"TIP\s*OFF", l, re.I):
                status, period = l, p
                break

    as_int = lambda s: int(s) if s.isdigit() else None
    return Game(key, home_full, away_full, as_int(home_score), as_int(away_score),
                tip, status, period)


def find_today_game(now):
    html = fetch(SCHEDULE_URL.format(club=KINGS_ID))
    for key in schedule_keys(html)[:6]:
        try:
            g = parse_game(key, fetch(DETAIL_URL.format(key=key)))
        except Exception as e:
            print(f"[skip] {key}: {e}")
            continue
        if g.tipoff and g.tipoff.date() == now.date():
            return g
    return None


# ---------------------------------------------------------------- 選手別得点
# ボックススコアはページ上のJavaScriptで読み込まれるため、クォーター終了時と
# 試合終了時だけ、ヘッドレスブラウザでページを開いて読み取る。
# 取り方は2通り: (1) 裏で届くJSONから選手データを探す (2) 表示された表を読む

NAME_KEYS = ("PlayerNameJ", "PlayerName", "PlayerNameJp", "NameJ", "Name", "name", "player_name")
PTS_KEYS = ("PTS", "Pts", "pts", "Point", "Points", "Score", "points")
TEAM_KEYS = ("TeamID", "TeamId", "team_id", "TeamCode")
TOTAL_WORDS = ("TOTAL", "Total", "合計", "チーム", "TEAM", "Team")

_pw_ready = False


def _ensure_browser():
    global _pw_ready
    if _pw_ready:
        return
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "playwright"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
                   check=True)
    _pw_ready = True


def _walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json(v)


def players_from_json(blobs):
    """JSONの中から「名前+得点(+チームID)」を持つ行を探す。"""
    out = []
    for blob in blobs:
        for d in _walk_json(blob):
            nk = next((k for k in NAME_KEYS if isinstance(d.get(k), str) and d.get(k)), None)
            pk = next((k for k in PTS_KEYS if k in d), None)
            if not nk or not pk:
                continue
            try:
                pts = int(d[pk])
            except (TypeError, ValueError):
                continue
            team = next((str(d[k]) for k in TEAM_KEYS if k in d), None)
            out.append((team, d[nk].strip(), pts))
    kings = [(n, p) for t, n, p in out if t == str(KINGS_ID)]
    return _dedupe(kings)


def _to_int(s):
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


def players_from_tables(tables, game):
    """表示された表から、キングスの選手の得点を読む。
    tables: [{"before": 表の直前のテキスト, "head": [見出し], "rows": [[セル]]}]"""
    cands = []
    for t in tables:
        head = [h.strip().upper() for h in t["head"]]
        pi = next((i for i, h in enumerate(head) if h in ("PTS", "得点", "PT", "POINTS")), None)
        if pi is None:
            continue
        ni = next((i for i, h in enumerate(head) if any(w in h for w in ("選手", "PLAYER", "NAME"))), None)
        rows = []
        for r in t["rows"]:
            if len(r) <= pi:
                continue
            pts = _to_int(r[pi])
            if pts is None:
                continue
            if ni is not None and ni < len(r):
                name = r[ni].strip()
            else:  # 名前列が分からなければ、数字でない最初のセル
                name = next((c.strip() for c in r if c.strip() and not re.fullmatch(r"[\d.%:/\-#]+", c.strip())), "")
            if not name or any(w in name for w in TOTAL_WORDS):
                continue
            rows.append((name, pts))
        if rows:
            cands.append((t.get("before", ""), rows))
    if not cands:
        return []
    # 表の直前に出てくるチーム名で判定(後に出てくる方がその表のチーム)
    def kings_score(before):
        k = max(before.rfind("琉球"), before.rfind("キングス"))
        o = max(before.rfind(game.opp), before.rfind(_short(game.opp)))
        return k - o
    kings_tables = [rows for before, rows in cands if kings_score(before) > 0]
    if kings_tables:
        return _dedupe(kings_tables[0])
    # 判定できなければ ホーム→アウェイ の順と仮定
    idx = 0 if game.kings_home else 1
    return _dedupe(cands[idx][1]) if len(cands) > idx else []


def _short(name):
    return name[:2]


def _dedupe(rows):
    seen, out = set(), []
    for n, p in rows:
        if n not in seen:
            seen.add(n)
            out.append((n, p))
    return out


TABLES_JS = """
() => [...document.querySelectorAll('table')].map(t => {
  const r = document.createRange();
  r.setStart(document.body, 0); r.setEndBefore(t);
  const before = r.toString().replace(/\\s+/g, ' ').slice(-300);
  const trs = [...t.querySelectorAll('tr')];
  const headRow = t.querySelector('thead tr') || trs[0];
  const cells = tr => [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim());
  return {before, head: headRow ? cells(headRow) : [],
          rows: trs.filter(tr => tr !== headRow).map(cells)};
})
"""


def load_boxscore(game):
    """試合ページをブラウザで開き、(裏で届いたJSON一覧, 表示された表一覧) を返す。"""
    _ensure_browser()
    from playwright.sync_api import sync_playwright
    blobs = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(user_agent=HEADERS["User-Agent"], locale="ja-JP")

        def on_resp(resp):
            if "json" in (resp.headers.get("content-type") or ""):
                try:
                    blobs.append(resp.json())
                except Exception:
                    pass
        page.on("response", on_resp)
        page.goto(DETAIL_URL.format(key=game.key), wait_until="networkidle", timeout=60000)
        for label_text in ("ボックススコア", "BOX SCORE", "BOXSCORE"):
            loc = page.get_by_text(label_text, exact=True)
            if loc.count():
                try:
                    loc.first.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                break
        page.wait_for_timeout(2000)
        tables = page.evaluate(TABLES_JS)
        b.close()
    return blobs, tables


def fetch_kings_scorers(game):
    """キングス選手の (名前, 得点) を得点順で返す。取れなければ空リスト。"""
    try:
        blobs, tables = load_boxscore(game)
        rows = players_from_json(blobs) or players_from_tables(tables, game)
        rows = [(n, p) for n, p in rows if p > 0]
        rows.sort(key=lambda x: -x[1])
        print(f"[boxscore] {rows}")
        return rows
    except Exception as e:
        print(f"[boxscore] 取得失敗: {e}")
        return []


# ---------------------------------------------------------------- スターティング5
STARTER_KEYS = ("StartingFlg", "StartFlg", "StartingFlag", "Starter", "IsStarter",
                "Starting", "StarterFlag", "Start", "GS", "starter", "is_starter")
STARTER_HEADS = ("S", "ST", "GS", "STARTER", "先発", "スタメン", "STARTING")
MARKS = {"○", "◯", "●", "*", "＊", "S", "✓", "✔", "★", "◎"}


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().upper() in ("1", "TRUE", "S", "Y", "YES", "○", "◯", "●", "*")


def starters_from_json(blobs):
    out = []
    for blob in blobs:
        for d in _walk_json(blob):
            nk = next((k for k in NAME_KEYS if isinstance(d.get(k), str) and d.get(k)), None)
            sk = next((k for k in STARTER_KEYS if k in d), None)
            if not nk or not sk:
                continue
            team = next((str(d[k]) for k in TEAM_KEYS if k in d), None)
            if team == str(KINGS_ID) and _truthy(d[sk]):
                out.append(d[nk].strip())
    out = list(dict.fromkeys(out))
    return out if len(out) == 5 else []


def starters_from_tables(tables, game):
    """キングスの表から、先発の印が付いた5人を探す。"""
    cands = []
    for t in tables:
        head = [h.strip().upper() for h in t["head"]]
        rows = [r for r in t["rows"] if r and not any(w in " ".join(r) for w in TOTAL_WORDS)]
        if len(rows) < 5:
            continue
        ni = next((i for i, h in enumerate(head) if any(w in h for w in ("選手", "PLAYER", "NAME"))), None)
        width = max(len(r) for r in rows)
        # 先発の印の列: 見出しで分かるか、ちょうど5行にだけ印がある列
        cols = [i for i, h in enumerate(head) if h in STARTER_HEADS]
        for i in range(width):
            vals = [r[i].strip() if i < len(r) else "" for r in rows]
            if sum(v in MARKS for v in vals) == 5:
                cols.append(i)
        for si in cols:
            names = []
            for r in rows:
                v = r[si].strip() if si < len(r) else ""
                if v not in MARKS and not _truthy(v):
                    continue
                if ni is not None and ni < len(r):
                    name = r[ni].strip()
                else:
                    name = next((c.strip() for c in r if len(c.strip()) > 1
                                 and not re.fullmatch(r"[\d.%:/\-#]+", c.strip())
                                 and c.strip() not in MARKS), "")
                if name:
                    names.append(name)
            if len(names) == 5:
                cands.append((t.get("before", ""), names))
                break
    if not cands:
        return []

    def kings_score(before):
        k = max(before.rfind("琉球"), before.rfind("キングス"))
        o = max(before.rfind(game.opp), before.rfind(_short(game.opp)))
        return k - o
    mine = [n for before, n in cands if kings_score(before) > 0]
    if mine:
        return mine[0]
    idx = 0 if game.kings_home else 1
    return cands[idx][1] if len(cands) > idx else []


def fetch_kings_starters(game):
    """キングスの先発5人の名前を返す。まだ発表されていない・読めない時は空リスト。"""
    try:
        blobs, tables = load_boxscore(game)
        names = starters_from_json(blobs) or starters_from_tables(tables, game)
        print(f"[starters] {names}")
        return names
    except Exception as e:
        print(f"[starters] 取得失敗: {e}")
        return []


def scorers_text(rows, top=None):
    if not rows:
        return ""
    shown = rows[:top] if top else rows
    return "得点: " + " / ".join(f"{n} {p}" for n, p in shown)


# ---------------------------------------------------------------- 通知
def notify(title, body, tags="basketball", priority="default"):
    print(f"[notify] {title} | {body}")
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC 未設定のため送信なし)")
        return
    try:
        payload = {"topic": NTFY_TOPIC, "title": title, "message": body,
                   "tags": [tags], "priority": {"low": 2, "default": 3, "high": 4}[priority]}
        if CURRENT_KEY:
            payload["click"] = DETAIL_URL.format(key=CURRENT_KEY)
        requests.post(NTFY_SERVER, json=payload, timeout=15).raise_for_status()
    except Exception as e:
        print(f"  送信失敗: {e}")


# ---------------------------------------------------------------- ウィジェット用の状況ファイル
STATUS_FILE = "status.json"


def save_status(g, scorers=None):
    """ホーム画面ウィジェットが読む status.json を更新してリポジトリに保存する。"""
    import json
    import subprocess
    data = {
        "key": g.key, "date": g.tipoff.strftime("%Y-%m-%d") if g.tipoff else None,
        "tipoff": g.tipoff.strftime("%H:%M") if g.tipoff else None,
        "home": g.home, "away": g.away, "kings_home": g.kings_home,
        "home_score": g.home_score, "away_score": g.away_score,
        "period": g.period, "scorers": [[n, p] for n, p in (scorers or [])],
        "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        run = lambda *c: subprocess.run(c, check=True, capture_output=True, text=True)
        run("git", "config", "user.name", "kings-bot")
        run("git", "config", "user.email", "kings-bot@users.noreply.github.com")
        run("git", "add", STATUS_FILE)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        run("git", "commit", "-m", f"状況更新 {data['updated']}")
        run("git", "pull", "--rebase", "-q")
        run("git", "push", "-q")
    except Exception as e:
        print(f"[status] 保存失敗: {e}")


CURRENT_KEY = None
PERIOD_LABEL = {"Q1": "1Q", "Q2": "2Q", "Q3": "3Q", "Q4": "4Q"}


def label(p):
    return PERIOD_LABEL.get(p) or (f"延長{p[2:]}" if p and p.startswith("OT") else p)


# ---------------------------------------------------------------- 見張りループ
def watch(game):
    global CURRENT_KEY
    CURRENT_KEY = game.key
    try:  # 選手別得点の読み取りに使うブラウザを先に用意しておく
        _ensure_browser()
    except Exception as e:
        print(f"[boxscore] ブラウザの準備に失敗: {e}")
    started_at = datetime.now(JST)
    prev = None
    starters_sent = False
    starter_tries = 0
    last_starter_try = None
    last_fallback = None
    errors = 0
    warned = False

    while datetime.now(JST) - started_at < MAX_RUN:
        try:
            g = parse_game(game.key, fetch(DETAIL_URL.format(key=game.key)))
            errors = 0
        except Exception as e:
            errors += 1
            print(f"[error] {e}")
            if errors >= 10 and not warned:
                notify("⚠️ 速報の取得に失敗中", "公式ページを読み取れていません。ページの作りが変わった可能性があります。",
                       tags="warning")
                warned = True
            time.sleep(POLL_SEC)
            continue

        now = datetime.now(JST)
        print(f"{now:%H:%M:%S} status={g.status!r} period={g.period} "
              f"{g.home_score}-{g.away_score}")

        if not g.started:
            if g.tipoff and now > g.tipoff + timedelta(hours=4):
                print("開始が確認できないまま4時間経過。終了。")
                return
            # 開始前: 2分おきに最大3回、スタメンが出ていないか確認
            if (not starters_sent and starter_tries < 3 and
                    (last_starter_try is None or now - last_starter_try >= timedelta(minutes=2))):
                starter_tries += 1
                last_starter_try = now
                names = fetch_kings_starters(g)
                if names:
                    tip = f"{g.tipoff:%H:%M} TIP OFF" if g.tipoff else ""
                    notify("📋 キングス スタメン", f"vs {g.opp}  {tip}\n" + " / ".join(names))
                    starters_sent = True
            prev = g
            time.sleep(POLL_SEC)
            continue

        if g.period == "FINAL":
            if prev is None or prev.period != "FINAL":
                win = g.kings_pts > g.opp_pts
                body = f"FINAL  {g.scoreline()}"
                rows = fetch_kings_scorers(g)
                save_status(g, rows)
                sc = scorers_text(rows)
                if sc:
                    body += "\n" + sc
                notify("🏆 勝利!" if win else "試合終了", body,
                       tags="trophy" if win else "basketball", priority="high")
            return

        if prev is None or not prev.started:
            first = prev is not None  # 開始前から見ていたなら「ティップオフ」
            body = f"vs {g.opp}" + ("" if first else f"  {label(g.period) or ''}  {g.scoreline()}")
            if not starters_sent:  # 開始前に読めなかった場合はここで添える
                names = fetch_kings_starters(g)
                if names:
                    body += "\nスタメン: " + " / ".join(names)
                    starters_sent = True
            notify("🏀 ティップオフ!" if first else "🏀 試合中", body)
            last_fallback = now
            save_status(g)
        else:
            # クォーター終了
            if g.period and prev.period and g.period != prev.period and prev.period != "HALF":
                extra = "(ハーフタイム)" if g.period == "HALF" else ""
                body = g.scoreline()
                rows = fetch_kings_scorers(g)
                save_status(g, rows)
                sc = scorers_text(rows, top=3)
                if sc:
                    body += "\n" + sc
                notify(f"{label(prev.period)}終了{extra}", body)
                last_fallback = now
            # 4Q・延長の逆転/同点
            clutch = g.period in ("Q4",) or (g.period or "").startswith("OT")
            if clutch and g.leader() != prev.leader():
                t = {"kings": "🔥 キングス逆転!" if prev.leader() == "opp" else "🔥 キングス勝ち越し!",
                     "opp": "😱 逆転された…" if prev.leader() == "kings" else "😱 勝ち越された…",
                     "tie": "⚖️ 同点!"}[g.leader()]
                notify(t, f"{label(g.period)}  {g.scoreline()}", priority="high")
            # クォーターが読めない場合の保険
            if g.period is None and (last_fallback is None or
                                     now - last_fallback >= timedelta(minutes=FALLBACK_UPDATE_MIN)):
                notify("スコア更新", g.scoreline(), priority="low")
                last_fallback = now

        prev = g
        time.sleep(POLL_SEC)


def main():
    now = datetime.now(JST)
    args = sys.argv[1:]

    if args[:1] == ["--key"]:
        g = parse_game(args[1], fetch(DETAIL_URL.format(key=args[1])))
        print(g, g.scoreline() if g.started else "(未開始)")
        return

    if args[:1] == ["--starters"]:
        g = parse_game(args[1], fetch(DETAIL_URL.format(key=args[1])))
        print(" / ".join(fetch_kings_starters(g)) or "スタメンを読み取れませんでした")
        return

    if args[:1] == ["--box"]:
        g = parse_game(args[1], fetch(DETAIL_URL.format(key=args[1])))
        print(scorers_text(fetch_kings_scorers(g)) or "選手別得点を読み取れませんでした")
        return

    if args[:1] == ["--test"]:
        g = None
        try:
            html = fetch(SCHEDULE_URL.format(club=KINGS_ID))
            keys = schedule_keys(html)
            if keys:
                g = parse_game(keys[0], fetch(DETAIL_URL.format(key=keys[0])))
        except Exception as e:
            print(f"日程の取得に失敗: {e}")
        body = (f"次戦: {g.tipoff:%m/%d %H:%M} {g.home} vs {g.away}" if g and g.tipoff
                else "テスト送信です(日程は読み取れませんでした)")
        notify("✅ キングス速報 テスト", body)
        return

    g = find_today_game(now)
    if not g:
        print("今日のキングスの試合なし")
        return
    print(f"今日の試合: {g}")
    if g.period == "FINAL":
        print("すでに終了")
        return
    if g.tipoff and not g.started:
        begin = g.tipoff - timedelta(minutes=WATCH_LEAD_MIN)
        if now < g.tipoff - timedelta(minutes=START_WINDOW_MIN):
            print("まだ早い。次の起動で見張る。")
            return
        wait = (begin - now).total_seconds()
        if wait > 0:
            print(f"開始{WATCH_LEAD_MIN}分前({begin:%H:%M})まで待機")
            time.sleep(wait)
    watch(g)


if __name__ == "__main__":
    main()
