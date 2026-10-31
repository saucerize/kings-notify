"""琉球ゴールデンキングス 試合速報 → ntfy 通知

GitHub Actions から1時間おきに起動される。
- 今日キングスの試合があれば、試合開始5分前から終了まで30秒おきにスコアを確認
  (試合中のスコアはページ最上部の速報帯から、クォーターはブラウザで描画したページから読む)
- ティップオフ / 各クォーター終了 / 4Q・延長の逆転・同点 / 試合終了 を ntfy に送る
- 試合開始前(またはティップオフ時)にキングスのスターティング5とベンチ入りの選手を送る
- クォーター終了時はキングスの得点上位3人、試合終了時は得点した全員の得点を添える
- 試合中のスコアと選手別得点を、Webページ(kings-next-game)用の ntfy トピックにも流す

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
# Webページが読む試合中の状況。ページから読むので公開のトピック(通知用とは別)
LIVE_TOPIC = os.environ.get("NTFY_LIVE_TOPIC", "kings-live-a38853caea").strip()

POLL_SEC = 30
START_WINDOW_MIN = 75         # この時間内に試合があれば、その起動で待機して担当する
WATCH_LEAD_MIN = 5            # 見張りを始めるのは試合開始の何分前か
MAX_RUN = timedelta(hours=5, minutes=40)  # GitHub Actions の6時間制限より手前で止める
FALLBACK_UPDATE_MIN = 15      # クォーターが読み取れない時の定期スコア通知間隔
LIVE_PUSH_SEC = 60            # Webページ用の状況を送る最短間隔(ntfy の送信回数制限に配慮)
# B.LEAGUE 公式の速報が読んでいる試合データ(プレーの記録・両チームのボックススコア)。latestid で最新の番号が分かる
GENIUS_URL = "https://b-league.s3.amazonaws.com/web_json/v2_genius_contexts/{key}/{name}"
GENIUS_READ_SEC = 30          # 試合データを読み直す間隔
PLAYS_MAX = 8                 # Webページに送る直近のプレーの数
LIVE_MAX_BYTES = 3900         # ntfy の1件の上限(4096バイト)より少し小さく
SKIP_PLAY_RE = re.compile(r"プレイヤーイン|プレイヤーアウト|ファウルドローン")   # 交代などは送らない
BOX_RETRY_MIN = 3             # 選手別得点が読めない時、ボックススコアを開き直す間隔

HEADERS = {"User-Agent": "kings-score-notifier (personal use)"}


# ---------------------------------------------------------------- 取得・解析
def fetch(url, tries=4):
    """公式サイトの読み込み。一時的な失敗(混雑・通信切れ)なら少し待ってやり直す。"""
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"[fetch] 失敗、やり直します({i + 1}/{tries - 1}): {e}")
            time.sleep(5 * (i + 1))


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
    home_short: str = ""
    away_short: str = ""

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
    hs_, as_ = as_int(home_score), as_int(away_score)
    # 試合中は見出しのスコアが「--」のままで、ページ最上部の速報帯にだけ数字が出る
    strip = parse_strip(L, home_short, away_short)
    if strip:
        sh, sa, finished = strip
        if hs_ is None or as_ is None:
            hs_, as_ = sh, sa
        if finished and period is None:
            status, period = "試合終了", "FINAL"
    return Game(key, home_full, away_full, hs_, as_, tip, status, period,
                home_short, away_short)


def parse_strip(L, home_short, away_short):
    """ページ最上部の速報帯(複数試合が並ぶ)から、この試合の (ホーム点, アウェイ点, 終了済みか) を読む。"""
    for i in range(len(L) - 3):
        if (L[i] == home_short and L[i + 1].isdigit() and
                L[i + 2] == away_short and L[i + 3].isdigit()):
            h, a = int(L[i + 1]), int(L[i + 3])
            finished = i > 0 and L[i - 1].startswith("試合終了")
            if not finished:
                pat = re.compile(r"^試合終了\s*" + re.escape(home_short))
                finished = any(pat.match(x) for x in L[i + 4:i + 8])
            return h, a, finished
    return None


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
        page.goto(DETAIL_URL.format(key=game.key), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        for label_text in ("ボックススコア", "BOX SCORE", "BOXSCORE"):
            loc = page.get_by_text(label_text, exact=True)
            if loc.count():
                try:
                    loc.first.click(timeout=5000)
                    page.wait_for_timeout(4000)
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


# ---------------------------------------------------------------- 試合中のクォーター
# クォーターや残り時間は公式ページ上でJavaScriptが描くため、ブラウザでページを開いたまま
# 1分おきに読み直す。(1) 状況表示(例「2Q 05:23」) (2) クォーター別得点の表 の2通りで探す。
Q_HEAD_RE = re.compile(r"^(?:([1-4])(?:Q|ST|ND|RD|TH)?|第([1-4])Q|(OT|EX|延長)\s*(\d*))$", re.I)


def parse_quarter_table(tables, game):
    """クォーター別得点の表から {"home": [各Q], "away": [各Q], "labels": [...]} を返す。"""
    hs, as_ = _short_names(game)
    for t in tables:
        head = [h.strip().upper() for h in t["head"]]
        qcols = [(i, h) for i, h in enumerate(head) if Q_HEAD_RE.match(h)]
        if len([1 for _, h in qcols if not re.match(r"^(OT|EX|延長)", h)]) < 4:
            continue
        found = {}
        for r in t["rows"]:
            joined = " ".join(r[:2])
            side = None
            if any(n and n in joined for n in (game.home, hs)):
                side = "home"
            elif any(n and n in joined for n in (game.away, as_)):
                side = "away"
            if side and side not in found:
                found[side] = [_to_int(r[i]) if i < len(r) else None for i, _ in qcols]
        if len(found) == 2:
            return {"home": found["home"], "away": found["away"], "labels": [h for _, h in qcols]}
    return None


def _short_names(game):
    return getattr(game, "home_short", "") or "", getattr(game, "away_short", "") or ""


def quarters_played(qt):
    """表から、得点が入っているクォーターの数を返す(進行中のクォーターを含む)。"""
    n = 0
    for i, (h, a) in enumerate(zip(qt["home"], qt["away"])):
        if h is not None and a is not None and (h + a > 0):
            n = i + 1
    return n


def q_label(i, labels):
    lab = labels[i] if i < len(labels) else ""
    m = Q_HEAD_RE.match(lab)
    if m and m.group(3):
        return f"延長{m.group(4) or (i - 3)}"
    return f"{i + 1}Q"


CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}(\.\d)?$")
PERIOD_LINE_RE = re.compile(r"^(?:([1-4])\s*(?:Q|ST|ND|RD|TH)|第\s*([1-4])\s*Q|(?:OT|延長)\s*(\d*))"
                            r"(?:\s+(\d{1,2}:\d{2}(?:\.\d)?))?$", re.I)


def period_from_text(text):
    """描画後のページの文字から「3Q 05:12」のような、残り時間付きのクォーター表示を探す。
    残り時間が付いていない「1Q」(クォーター別得点の見出しなど)は使わない。"""
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    for i, l in enumerate(lines):
        if re.fullmatch(r"(HALF\s*TIME|ハーフタイム)", l, re.I):
            return "HALF"
        m = PERIOD_LINE_RE.match(l)
        if not m:
            continue
        has_clock = m.group(4) or (i + 1 < len(lines) and CLOCK_RE.match(lines[i + 1]))
        if not has_clock:
            continue
        if m.group(1) or m.group(2):
            return f"Q{m.group(1) or m.group(2)}"
        return f"OT{m.group(3) or 1}"
    return None


class LivePage:
    """ブラウザで試合ページを開きっぱなしにして、描画後の状況を読む。"""

    def __init__(self, game):
        self.game = game
        self.pw = self.browser = self.page = None
        self.misses = 0
        self.diag_sent = False
        self.blobs = []
        self.scorers = []   # 直近の読み取りで分かったキングスの選手別得点

    def _on_resp(self, resp):
        if "json" in (resp.headers.get("content-type") or ""):
            try:
                self.blobs.append(resp.json())
            except Exception:
                pass

    def _open(self):
        _ensure_browser()
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()
        self.page = self.browser.new_page(user_agent=HEADERS["User-Agent"], locale="ja-JP")
        self.page.on("response", self._on_resp)
        self.page.goto(DETAIL_URL.format(key=self.game.key), wait_until="domcontentloaded", timeout=60000)

    def read(self):
        """(状況表示から読んだクォーター, クォーター別得点の表) を返す。読めなければ None。"""
        self.blobs = []
        try:
            if self.page is None:
                self._open()
            else:
                self.page.reload(wait_until="domcontentloaded", timeout=45000)
            self.page.wait_for_timeout(6000)
            html = self.page.content()
            tables = self.page.evaluate(TABLES_JS)
        except Exception as e:
            print(f"[live] 読み取り失敗: {e}")
            self.close()
            return None, None
        period = None
        try:
            period = parse_game(self.game.key, html).period
            if period == "FINAL":
                period = None  # 終了判定は速報帯/見出しに任せる
        except Exception as e:
            print(f"[live] 状況表示を解析できず: {e}")
        qt = parse_quarter_table(tables, self.game)
        try:
            rows = players_from_json(self.blobs) or players_from_tables(tables, self.game)
            self.scorers = sorted([(n, p) for n, p in rows if p > 0], key=lambda x: -x[1])
        except Exception as e:
            print(f"[live] 選手別得点を解析できず: {e}")
            self.scorers = []
        if period is None:
            try:
                period = period_from_text(self.page.evaluate("() => document.body.innerText"))
            except Exception:
                pass
        print(f"[live] period={period} table={qt}")
        if period is None and qt is None:
            self.misses += 1
            if self.misses == 3 and not self.diag_sent:
                self.diag_sent = True
                notify("🔧 診断用(このままClaudeに送ってください)", self._diag(tables), priority="low")
        return period, qt

    def _diag(self, tables):
        """クォーター表示を探すための手がかり(描画後の文字と表の見出し)をまとめる。"""
        try:
            text = self.page.evaluate("() => document.body.innerText")
        except Exception:
            text = ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        keys = ("スコアチャート", "ゲームスコア", "試合中", "Q", "TIP OFF")
        pick = []
        for i, l in enumerate(lines):
            if any(k in l for k in keys):
                pick.append(" | ".join(lines[max(0, i - 3):i + 4]))
        heads = [" ".join(t["head"])[:80] for t in tables[:8]]
        out = "[text]\n" + "\n".join(dict.fromkeys(pick))[:2200] + "\n[tables]\n" + "\n".join(heads)
        return out[:3500]

    def close(self):
        for x in (self.browser, self.pw):
            try:
                if x is self.pw and x:
                    x.stop()
                elif x:
                    x.close()
            except Exception:
                pass
        self.pw = self.browser = self.page = None


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


# ---------------------------------------------------------------- ベンチ入り(その試合の登録選手)
STAT_HEADS = ("PTS", "MIN", "得点", "出場時間", "FGM", "REB")


def roster_from_json(blobs):
    out = []
    for blob in blobs:
        for d in _walk_json(blob):
            nk = next((k for k in NAME_KEYS if isinstance(d.get(k), str) and d.get(k)), None)
            if not nk:
                continue
            team = next((str(d[k]) for k in TEAM_KEYS if k in d), None)
            if team == str(KINGS_ID) and not any(w in d[nk] for w in TOTAL_WORDS):
                out.append(d[nk].strip())
    out = list(dict.fromkeys(out))
    return out if 8 <= len(out) <= 15 else []


def roster_from_tables(tables, game):
    """ボックススコアのキングスの表から、その試合に登録された選手の名前を全員分読む。"""
    cands = []
    for t in tables:
        head = [h.strip().upper() for h in t["head"]]
        if not any(h in STAT_HEADS for h in head):
            continue
        ni = next((i for i, h in enumerate(head) if any(w in h for w in ("選手", "PLAYER", "NAME"))), None)
        names = []
        for r in t["rows"]:
            if not r or any(w in " ".join(r) for w in TOTAL_WORDS):
                continue
            if ni is not None and ni < len(r):
                name = r[ni].strip()
            else:
                name = next((c.strip() for c in r if len(c.strip()) > 1
                             and not re.fullmatch(r"[\d.%:/\-#]+", c.strip())
                             and c.strip() not in MARKS), "")
            if name:
                names.append(name)
        names = list(dict.fromkeys(names))
        if 8 <= len(names) <= 15:
            cands.append((t.get("before", ""), names))
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


def fetch_kings_lineup(game):
    """(先発5人, ベンチ入りの選手) を返す。読めない部分は空リスト。"""
    try:
        blobs, tables = load_boxscore(game)
        starters = starters_from_json(blobs) or starters_from_tables(tables, game)
        roster = roster_from_json(blobs) or roster_from_tables(tables, game)
        bench = [n for n in roster if n not in starters] if starters else roster
        print(f"[lineup] starters={starters} bench={bench}")
        return starters, bench
    except Exception as e:
        print(f"[lineup] 取得失敗: {e}")
        return [], []


def fetch_kings_starters(game):
    return fetch_kings_lineup(game)[0]


def lineup_text(starters, bench):
    lines = []
    if starters:
        lines.append("スタメン: " + " / ".join(starters))
    if bench:
        lines.append(("ベンチ: " if starters else f"ベンチ入り({len(bench)}人): ") + " / ".join(bench))
    return "\n".join(lines)


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


def fetch_genius(key):
    """公式の速報用の試合データ(JSON)を読む。読めなければ None。"""
    try:
        r = requests.get(GENIUS_URL.format(key=key, name="latestid"), headers=HEADERS, timeout=15)
        r.raise_for_status()
        latest = r.text.strip()
        if not latest.isdigit():
            return None
        r = requests.get(GENIUS_URL.format(key=key, name=f"{latest}.json"), headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[genius] 読めませんでした: {e}")
        return None


def genius_live(data, kings_home):
    """試合データから、Webページ用の {clock, plays, box} を作る。
    plays は新しい順。スコアや box は「キングス / 相手」に並べ替える。"""
    to_ko = lambda h, a: (h, a) if kings_home else (a, h)
    raw = sorted(data.get("PlayByPlays") or [], key=lambda p: p.get("No") or 0)
    out = {"clock": (raw[-1].get("RestTime") or "") if raw else ""}
    plays = []
    for p in reversed(raw):
        text = re.sub(r"\s+", " ", p.get("PlayText") or "").strip()
        if not text or SKIP_PLAY_RE.search(text):
            continue
        ha = p.get("HomeAway")
        team = "" if ha not in (1, 2) else ("kings" if (ha == 1) == kings_home else "opp")
        m = re.match(r"^(\d+)-(\d+)$", p.get("Score") or "")
        k, o = to_ko(int(m.group(1)), int(m.group(2))) if m else (None, None)
        per = p.get("Period")
        period = f"Q{per}" if per in (1, 2, 3, 4) else (f"OT{per - 4}" if isinstance(per, int) and per > 4 else None)
        plays.append({"period": period, "clock": p.get("RestTime") or "", "text": text[:60],
                      "team": team, "kings": k, "opp": o})
        if len(plays) >= PLAYS_MAX:
            break
    out["plays"] = plays

    def team_box(rows):
        # クォーター別・前後半・試合全体の行が混ざっているので、合計得点がいちばん多い区分(=試合全体)を使う
        by = {}
        for r in rows or []:
            if r.get("PlayerID") and r.get("PlayerNameJ"):
                by.setdefault(r.get("PeriodCategory"), []).append(r)
        if not by:
            return []
        cat = max(by, key=lambda c: sum(int(r.get("Point") or 0) for r in by[c]))
        res = []
        for r in by[cat]:
            t = r.get("PlayTime") or ""
            pts = int(r.get("Point") or 0)
            if pts == 0 and re.fullmatch(r"0?0:00|", t):
                continue   # 出場していない選手
            res.append({"name": re.sub(r"\s+", " ", r["PlayerNameJ"]).strip(), "min": t, "pts": pts,
                        "reb": int(r.get("RB_TOT") or 0), "ast": int(r.get("AS") or 0)})
        return sorted(res, key=lambda x: -x["pts"])

    home, away = team_box(data.get("HomeBoxscores")), team_box(data.get("AwayBoxscores"))
    out["box"] = dict(zip(("kings", "opp"), to_ko(home, away)))
    return out


def genius_state(data):
    """試合データの最新のプレーから (ホーム得点, アウェー得点, クォーター) を返す。分からない部分は None。"""
    raw = sorted(data.get("PlayByPlays") or [], key=lambda p: p.get("No") or 0)
    home = away = period = None
    for p in reversed(raw):
        if period is None:
            per = p.get("Period")
            if isinstance(per, int) and per >= 1:
                period = f"Q{per}" if per <= 4 else f"OT{per - 4}"
        m = re.match(r"^(\d+)-(\d+)$", p.get("Score") or "")
        if m:
            home, away = int(m.group(1)), int(m.group(2))
            break
    return home, away, period


def apply_genius_state(g, state):
    """公式ページの表示が遅れているとき、試合データの方が新しければスコアとクォーターを上書きする。"""
    if not state or g.period == "FINAL":
        return
    home, away, period = state
    if home is not None and away is not None and g.started and home + away >= g.home_score + g.away_score:
        g.home_score, g.away_score = home, away
    pi_g, pi_new = period_index(g.period), period_index(period)
    if pi_new is not None and (pi_g is None or pi_new > pi_g):
        g.period = period


def valid_scorers(rows, g):
    """選手別得点がスコアと矛盾しないか(合計がチーム得点を超えていないか)。"""
    if not rows:
        return False
    return g.kings_pts is None or sum(p for _, p in rows) <= g.kings_pts


_live_last = {"payload": None, "at": None}


def publish_live(g, qt=None, scorers=None, force=False, extra=None):
    """Webページ(kings-next-game)用に、試合中の状況を ntfy の公開トピックへ送る。
    内容が変わったときだけ、最短 LIVE_PUSH_SEC 秒おきに送る(クォーターが変わった時と force はすぐ送る)。"""
    import json
    if not LIVE_TOPIC:
        return
    quarters = None
    if qt:
        quarters = []
        for i, (h, a) in enumerate(zip(qt["home"], qt["away"])):
            if h is None or a is None or (i >= 4 and h + a == 0):
                continue
            k, o = (h, a) if g.kings_home else (a, h)
            quarters.append([q_label(i, qt["labels"]), k, o])
    payload = {
        "type": "kings-live", "v": 1, "key": g.key,
        "datetime": g.tipoff.isoformat() if g.tipoff else None,
        "opponent": g.opp, "home": g.kings_home,
        "kings": g.kings_pts, "opp": g.opp_pts, "period": g.period,
        "quarters": quarters, "scorers": [[n, p] for n, p in (scorers or [])],
        "opp_short": g.away_short if g.kings_home else g.home_short,
    }
    payload.update(extra or {})   # 残り時間・直近のプレー・両チームの統計
    last, at = _live_last["payload"], _live_last["at"]
    now = datetime.now(JST)
    if payload == last:
        return
    period_changed = last is None or last.get("period") != payload["period"]
    if not (force or period_changed or at is None or (now - at).total_seconds() >= LIVE_PUSH_SEC):
        return
    body = json.dumps(dict(payload, updated=now.isoformat(timespec="seconds")), ensure_ascii=False)
    # ntfy の1件の大きさの上限に収まるよう、大きすぎるときはプレーの数・統計を減らす
    trimmed = dict(payload, updated=now.isoformat(timespec="seconds"))
    while len(body.encode("utf-8")) > LIVE_MAX_BYTES:
        if len(trimmed.get("plays") or []) > 3:
            trimmed["plays"] = trimmed["plays"][:-1]
        elif trimmed.get("box", {}).get("opp"):
            trimmed["box"] = dict(trimmed["box"], opp=trimmed["box"]["opp"][:-1])
        elif trimmed.get("box", {}).get("kings"):
            trimmed["box"] = dict(trimmed["box"], kings=trimmed["box"]["kings"][:-1])
        else:
            break
        body = json.dumps(trimmed, ensure_ascii=False)
    try:
        requests.post(f"{NTFY_SERVER}/{LIVE_TOPIC}", data=body.encode("utf-8"),
                      headers={"Content-Type": "application/json"}, timeout=15).raise_for_status()
        _live_last["payload"], _live_last["at"] = payload, now
        print(f"[live-push] {g.period} {payload.get('clock', '')} {g.kings_pts}-{g.opp_pts} "
              f"scorers={len(payload['scorers'])} plays={len(payload.get('plays') or [])} bytes={len(body.encode('utf-8'))}")
    except Exception as e:
        print(f"[live-push] 送信失敗: {e}")


CURRENT_KEY = None
PERIOD_LABEL = {"Q1": "1Q", "Q2": "2Q", "Q3": "3Q", "Q4": "4Q"}


def label(p):
    return PERIOD_LABEL.get(p) or (f"延長{p[2:]}" if p and p.startswith("OT") else p)


# ---------------------------------------------------------------- 見張りループ
LIVE_READ_SEC = 60   # 描画後のページ(クォーター情報)を読み直す間隔


def score_text(g, h, a):
    k, o = (h, a) if g.kings_home else (a, h)
    return f"琉球 {k} - {o} {g.opp}"


def period_index(p):
    """Q1..Q4, OTn を 0始まりの番号に。HALF は2Qの終わり扱い。"""
    if not p:
        return None
    if p == "HALF":
        return 1
    if p.startswith("Q"):
        return int(p[1:]) - 1
    if p.startswith("OT"):
        return 3 + int(p[2:] or 1)
    return None


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
    live = LivePage(game)
    last_live = None
    live_period, qt = None, None
    ended = set()          # 終了を通知済みのクォーター(0始まり)
    scorers = []           # Webページに出すキングスの選手別得点
    last_box = None
    genius = {}            # Webページに出す残り時間・直近のプレー・両チームの統計
    gstate = None          # 試合データから読んだ (ホーム得点, アウェー得点, クォーター)
    last_genius = None

    def read_genius(g, now):
        """試合データを読み直し、キングスの選手別得点もそこから取れれば使う。"""
        nonlocal genius, gstate, last_genius, scorers
        if last_genius is not None and (now - last_genius).total_seconds() < GENIUS_READ_SEC:
            return
        last_genius = now
        data = fetch_genius(g.key)
        if not data:
            return
        try:
            genius = genius_live(data, g.kings_home)
            gstate = genius_state(data)
        except Exception as e:
            print(f"[genius] 解析できませんでした: {e}")
            return
        rows = [(p["name"], p["pts"]) for p in genius["box"].get("kings", []) if p["pts"] > 0]
        apply_genius_state(g, gstate)
        if valid_scorers(rows, g):
            scorers = rows

    def announce_end(g, idx):
        nonlocal scorers
        if idx in ended or idx < 0:
            return
        ended.add(idx)
        labels = qt["labels"] if qt else []
        name = q_label(idx, labels)
        half = "(ハーフタイム)" if idx == 1 else ""
        body = g.scoreline()
        if qt and len(qt["home"]) > idx and all(
                x is not None for x in qt["home"][:idx + 1] + qt["away"][:idx + 1]):
            h, a = sum(qt["home"][:idx + 1]), sum(qt["away"][:idx + 1])
            qh, qa = qt["home"][idx], qt["away"][idx]
            qk, qo = (qh, qa) if g.kings_home else (qa, qh)
            body = f"{score_text(g, h, a)}\n{name}だけ: 琉球 {qk} - {qo}"
        rows = fetch_kings_scorers(g)
        if valid_scorers(rows, g):
            scorers = rows
        save_status(g, rows)
        sc = scorers_text(rows, top=3)
        if sc:
            body += "\n" + sc
        notify(f"{name}終了{half}", body)

    try:
        while datetime.now(JST) - started_at < MAX_RUN:
            try:
                g = parse_game(game.key, fetch(DETAIL_URL.format(key=game.key)))
                errors = 0
            except Exception as e:
                errors += 1
                print(f"[error] {e}")
                if errors >= 10 and not warned:
                    notify("⚠️ 速報の取得に失敗中",
                           "公式ページを読み取れていません。ページの作りが変わった可能性があります。",
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
                    starters, bench = fetch_kings_lineup(g)
                    if starters:
                        tip = f"{g.tipoff:%H:%M} TIP OFF" if g.tipoff else ""
                        notify("📋 キングス スタメン",
                               f"vs {g.opp}  {tip}\n" + lineup_text(starters, bench))
                        starters_sent = True
                prev = g
                time.sleep(POLL_SEC)
                continue

            if g.period == "FINAL":
                if prev is None or prev.period != "FINAL":
                    win = g.kings_pts > g.opp_pts
                    body = f"FINAL  {g.scoreline()}"
                    _, q_final = live.read()  # 最終クォーターまで埋まった表を読み直す
                    if q_final:
                        qt = q_final
                    if qt:
                        parts = []
                        for i, (h, a) in enumerate(zip(qt["home"], qt["away"])):
                            if h is None or a is None or (i >= 4 and h + a == 0):
                                continue
                            k, o = (h, a) if g.kings_home else (a, h)
                            parts.append(f"{q_label(i, qt['labels'])} {k}-{o}")
                        if parts:
                            body += "\n" + " / ".join(parts)
                    rows = fetch_kings_scorers(g)
                    last_genius = None
                    read_genius(g, datetime.now(JST))
                    publish_live(g, qt, rows if valid_scorers(rows, g) else scorers, force=True, extra=genius)
                    save_status(g, rows)
                    sc = scorers_text(rows)
                    if sc:
                        body += "\n" + sc
                    notify("🏆 勝利!" if win else "試合終了", body,
                           tags="trophy" if win else "basketball", priority="high")
                return

            # 描画後のページからクォーター情報を読む(1分おき)
            if last_live is None or (now - last_live).total_seconds() >= LIVE_READ_SEC:
                last_live = now
                p_r, q_r = live.read()
                if p_r:
                    live_period = p_r
                if q_r:
                    qt = q_r
                if valid_scorers(live.scorers, g):
                    scorers = live.scorers
                elif last_box is None or now - last_box >= timedelta(minutes=BOX_RETRY_MIN):
                    # 開きっぱなしのページから読めない時は、ボックススコアを開いて読む
                    last_box = now
                    rows = fetch_kings_scorers(g)
                    if valid_scorers(rows, g):
                        scorers = rows
            played = quarters_played(qt) if qt else 0
            g.period = g.period or live_period or (f"Q{played}" if 1 <= played <= 4 else
                                                    f"OT{played - 4}" if played > 4 else None)
            # 公式ページのスコア・クォーターは遅れることがあるので、試合データの方が新しければそちらを使う
            read_genius(g, now)
            apply_genius_state(g, gstate)

            if prev is None or not prev.started:
                first = prev is not None  # 開始前から見ていたなら「ティップオフ」
                body = f"vs {g.opp}" + ("" if first else f"  {label(g.period) or ''}  {g.scoreline()}")
                if not starters_sent:  # 開始前に読めなかった場合はここで添える
                    starters, bench = fetch_kings_lineup(g)
                    lt = lineup_text(starters, bench)
                    if lt:
                        body += "\n" + lt
                        starters_sent = True
                notify("🏀 ティップオフ!" if first else "🏀 試合中", body)
                last_fallback = now
                save_status(g)
                # 途中から見始めた場合、すでに終わったクォーターは通知しない
                cur = period_index(g.period)
                if cur is not None:
                    ended.update(range(cur if g.period != "HALF" else 2))
            else:
                # クォーター終了: 状況表示の変化、またはクォーター別得点の表に次のQが出たとき
                pi_prev, pi_now = period_index(prev.period), period_index(g.period)
                if g.period == "HALF":
                    announce_end(g, 1)
                elif pi_prev is not None and pi_now is not None and pi_now > pi_prev:
                    for i in range(pi_prev, pi_now):
                        announce_end(g, i)
                if played >= 2:
                    for i in range(played - 1):
                        announce_end(g, i)
                if g.period or ended:
                    last_fallback = now
                # 4Q・延長の逆転/同点
                clutch = g.period in ("Q4",) or (g.period or "").startswith("OT")
                if clutch and g.leader() != prev.leader():
                    msg = {"kings": "🔥 キングス逆転!" if prev.leader() == "opp" else "🔥 キングス勝ち越し!",
                           "opp": "😱 逆転された…" if prev.leader() == "kings" else "😱 勝ち越された…",
                           "tie": "⚖️ 同点!"}[g.leader()]
                    notify(msg, f"{label(g.period)}  {g.scoreline()}", priority="high")
                # クォーターがまったく読めない場合の保険
                if g.period is None and not ended and (
                        last_fallback is None or
                        now - last_fallback >= timedelta(minutes=FALLBACK_UPDATE_MIN)):
                    notify("スコア更新", g.scoreline())
                    last_fallback = now

            publish_live(g, qt, scorers, extra=genius)
            prev = g
            time.sleep(POLL_SEC)
    finally:
        live.close()


def main():
    now = datetime.now(JST)
    args = sys.argv[1:]

    if args[:1] == ["--key"]:
        g = parse_game(args[1], fetch(DETAIL_URL.format(key=args[1])))
        print(g, g.scoreline() if g.started else "(未開始)")
        return

    if args[:1] == ["--starters"]:
        g = parse_game(args[1], fetch(DETAIL_URL.format(key=args[1])))
        print(lineup_text(*fetch_kings_lineup(g)) or "スタメン・ベンチ入りを読み取れませんでした")
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

    g = None
    for i in range(6):  # 日程が読めない時は最大5分ほどやり直す
        try:
            g = find_today_game(now)
            break
        except Exception as e:
            print(f"[main] 日程の取得に失敗、やり直します: {e}")
            time.sleep(60)
    else:
        notify("⚠️ 日程を読み込めませんでした", "公式サイトに接続できませんでした。次の起動でもう一度試します。",
               tags="warning")
        return
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
