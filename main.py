from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import termios
import tty
import zlib
from html.parser import HTMLParser
from pathlib import Path


URL = (
    "https://portal.robocup.de/rescue/scoring/uebersicht/German%20Open"
    "?discipline=0&interval=0&active=false&modal=false&printView=false"
    "&oneLeague=true&display=false&userView=true"
)

CACHE_DIR = Path("data")
HTML_CACHE_FILE = CACHE_DIR / "robocup_page.html"
JSON_CACHE_FILE = CACHE_DIR / "scoreboard.json"
ENTRY_HTML_CACHE_FILE = CACHE_DIR / "robocup_entry_page.html"
ENTRY_JSON_CACHE_FILE = CACHE_DIR / "scoreboard_entry.json"
RENDER_SCRIPT = Path("render_dom.mjs")
CACHE_VERSION = 3

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
UNDERLINE = "\033[4m"
STRIKE = "\033[9m"

TOTAL_ROUNDS = 10
ENTRY_TOTAL_ROUNDS = 7
BAR_HEIGHT = 12
BAR_WIDTH = 1
BAR_GAP = 2
BAR_FILL = "█" * BAR_WIDTH
PIE_SIZE = 30
MIN_AVG_WIDTH = 0
MIN_ABS_WIDTH = 0
MIN_RELATIVE_WIDTH = 0
MIN_SPREAD_WIDTH = 0
TEAM_COLOR_CODES = [
    12,
    10,
    11,
    9,
    13,
    14,
]


def rounds_limit(entry: bool = False) -> int:
    return ENTRY_TOTAL_ROUNDS if entry else TOTAL_ROUNDS


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict] = []
        self._table: dict | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_head = False
        self._in_body = False
        self._cell_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = {"headers": [], "rows": []}
        elif tag == "thead":
            self._in_head = True
        elif tag == "tbody":
            self._in_body = True
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []
            self._cell_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            text = " ".join(part.strip() for part in self._cell if part.strip()).strip()
            self._row.append(text)
            self._cell = None
            self._cell_tag = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            row = self._row
            self._row = None
            if not row:
                return
            if self._in_head or all(self._cell_tag == "th" for _ in row):
                self._table["headers"] = row
            elif self._in_body or row:
                self._table["rows"].append(row)
        elif tag == "thead":
            self._in_head = False
        elif tag == "tbody":
            self._in_body = False
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    print("\n" * 3, end="")


def print_end_spacing() -> None:
    print("\n" * 3, end="")


def short_name(name: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.strip())
    parts = [part for part in re.split(r"[\s_\-+./:·]+", spaced) if part]
    if len(parts) >= 2:
        return "".join(part[0] for part in parts)[:5]
    compact = re.sub(r"[^0-9A-Za-zÄÖÜäöüß]+", "", name)
    digit_chunks = [chunk for chunk in re.split(r"(\d+)", compact) if chunk]
    if len(digit_chunks) >= 2:
        short = []
        for chunk in digit_chunks:
            if chunk.isdigit():
                short.append(chunk)
            else:
                short.append(chunk[0])
            if len("".join(short)) >= 5:
                break
        return "".join(short)[:5]
    return name[:3]


def display_name(name: str, fullname: bool = False) -> str:
    return name if fullname else short_name(name)


def cache_files(entry: bool = False) -> tuple[Path, Path]:
    if entry:
        return ENTRY_HTML_CACHE_FILE, ENTRY_JSON_CACHE_FILE
    return HTML_CACHE_FILE, JSON_CACHE_FILE


def browser_path() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def node_path() -> str | None:
    for name in ("node", "nodejs"):
        path = shutil.which(name)
        if path:
            return path
    return None


def refresh_document(entry: bool = False) -> int:
    clear_screen()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    html_cache_file, _ = cache_files(entry)

    pasted = ""
    if not sys.stdin.isatty():
        pasted = sys.stdin.read()
    else:
        print("paste scoreboard, then ctrl-d")
        print()
        try:
            pasted = sys.stdin.read()
        except KeyboardInterrupt:
            print()
            return 1
    if pasted.strip():
        html_cache_file.write_text(pasted, encoding="utf-8")
        rows = parse_document_rows(pasted, entry=entry)
        if not rows:
            print(f"{RED}saved pasted data, but could not parse scoreboard{RESET}")
            return 1
        save_rows(rows, entry=entry)
        return 0

    browser = browser_path()
    if browser is None:
        print(f"{RED}no chrome or chromium found{RESET}")
        return 1
    node = node_path()
    if node is None:
        print(f"{RED}no node found{RESET}")
        return 1
    if not RENDER_SCRIPT.exists():
        print(f"{RED}missing render script{RESET}")
        return 1

    command = [node, str(RENDER_SCRIPT), browser, URL, str(html_cache_file)]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print(f"{RED}refresh timed out{RESET}")
        return 1
    except OSError:
        print(f"{RED}refresh failed{RESET}")
        return 1

    document = html_cache_file.read_text(encoding="utf-8", errors="ignore") if html_cache_file.exists() else ""
    if result.returncode != 0 or not document.strip():
        print(f"{RED}refresh failed{RESET}")
        return 1

    rows = parse_document_rows(document, entry=entry)
    if not rows:
        print(f"{RED}no scoreboard found in rendered page{RESET}")
        return 1
    save_rows(rows, entry=entry)

    return 0


def import_pdf_document(pdf_path: Path, entry: bool = False, use_strikes: bool = True) -> int:
    clear_screen()
    if not pdf_path.exists() or not pdf_path.is_file():
        print(f"{RED}pdf not found{RESET}")
        return 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    html_cache_file, _ = cache_files(entry)
    text = extract_pdf_text(pdf_path)
    if not text.strip():
        print(f"{RED}pdf text extraction failed{RESET}")
        return 1

    rows = parse_pdf_rows(text, entry=entry, use_strikes=use_strikes)
    if not rows:
        print(f"{RED}could not parse scoreboard from pdf{RESET}")
        return 1

    html_cache_file.write_text(text, encoding="utf-8")
    save_rows(rows, entry=entry)
    return 0


def load_document(entry: bool = False) -> str:
    html_cache_file, _ = cache_files(entry)
    if not html_cache_file.exists():
        return ""
    return html_cache_file.read_text(encoding="utf-8", errors="ignore")


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def parse_json_score(value: object) -> tuple[int | None, bool]:
    if value is None:
        return None, False
    if isinstance(value, int):
        return value, False
    if isinstance(value, str):
        text = value.strip()
        unsure = text.endswith("?")
        if unsure:
            text = text[:-1].strip()
        return int(text), unsure
    raise ValueError("invalid score")


def encode_json_score(score: int | None, unsure: bool) -> int | str | None:
    if score is None:
        return None
    if unsure:
        return f"{score}?"
    return score


def save_rows(rows: list[dict], entry: bool = False) -> None:
    total_rounds = rounds_limit(entry)
    raw_rows = []
    for row in rows:
        unsure_indices = set(row.get("unsure_indices", []))
        scores = [
            encode_json_score(score, index in unsure_indices)
            for index, score in enumerate(row["scores"][:total_rounds])
        ]
        raw_rows.append({"name": row["name"], "scores": scores})
    _, json_cache_file = cache_files(entry)
    json_cache_file.write_text(
        json.dumps({"version": CACHE_VERSION, "rows": raw_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json_rows(entry: bool = False, use_strikes: bool = True) -> list[dict]:
    _, json_cache_file = cache_files(entry)
    if not json_cache_file.exists():
        return []
    try:
        data = json.loads(json_cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("version") != CACHE_VERSION:
        return []
    data_rows = data.get("rows")
    if not isinstance(data_rows, list):
        return []
    rows = []
    for item in data_rows:
        if not isinstance(item, dict):
            continue
        try:
            name = str(item["name"])
            raw_scores = item["scores"]
            if not isinstance(raw_scores, list):
                continue
            scores = []
            unsure_indices = []
            for index, value in enumerate(raw_scores):
                score, unsure = parse_json_score(value)
                scores.append(score)
                if unsure:
                    unsure_indices.append(index)
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            build_row(
                name,
                scores,
                unsure_indices=unsure_indices,
                rounds_count=rounds_limit(entry),
                use_strikes=use_strikes,
            )
        )
    return finalize_rows(rows)


def parse_rendered_rows(document: str, entry: bool = False, use_strikes: bool = True) -> list[dict]:
    parser = TableParser()
    parser.feed(document)

    best_table: dict | None = None
    best_score = -1
    for table in parser.tables:
        headers = [header.lower() for header in table.get("headers", [])]
        score = 0
        if any("platz" in header or "place" in header for header in headers):
            score += 1
        if any("team" in header for header in headers):
            score += 1
        if any("runde" in header or "run" in header for header in headers):
            score += 1
        if any("sum" in header for header in headers):
            score += 1
        if score > best_score and table.get("rows"):
            best_score = score
            best_table = table

    if best_table is None:
        return []

    rows = []
    for raw_row in best_table["rows"]:
        if len(raw_row) < 4:
            continue
        place_text = raw_row[0].strip()
        team_name = raw_row[1].strip()
        if not place_text or not team_name:
            continue

        scores: list[int | None] = []
        for cell in raw_row[2:-1]:
            match = re.match(r"\s*(-?\d+)", cell)
            if match:
                scores.append(int(match.group(1)))
            else:
                scores.append(None)

        total_rounds = rounds_limit(entry)
        if len(scores) < total_rounds:
            scores.extend([None] * (total_rounds - len(scores)))
        else:
            scores = scores[:total_rounds]

        valid_scores = [score for score in scores if score is not None]
        if len(valid_scores) < 3:
            continue

        rows.append(build_row(team_name, scores, rounds_count=total_rounds, use_strikes=use_strikes))

    return finalize_rows(rows)


def load_rows(entry: bool = False, use_strikes: bool = True) -> list[dict]:
    rows = load_json_rows(entry=entry, use_strikes=use_strikes)
    if rows:
        return rows

    document = load_document(entry=entry)
    if not document:
        return []
    rows = parse_document_rows(document, entry=entry, use_strikes=use_strikes)
    if rows:
        save_rows(rows, entry=entry)
    return rows


def normalize_rows(rows: list[dict], rounds_count: int = TOTAL_ROUNDS, use_strikes: bool = True) -> list[dict]:
    if not rows:
        return []

    max_runs = max(rounds_count, max(len(row["scores"]) for row in rows))
    column_best = []
    for index in range(max_runs):
        best = 0
        for row in rows:
            if index < len(row["scores"]) and row["scores"][index] is not None:
                best = max(best, row["scores"][index])
        column_best.append(best)

    normalized_rows = []
    for row in rows:
        scores: list[int | None] = []
        for index, score in enumerate(row["scores"]):
            if score is None:
                scores.append(None)
                continue
            best = column_best[index]
            normalized = round((score / best) * 100) if best > 0 else 0
            scores.append(normalized)
        normalized_rows.append(
            build_row(
                row["name"],
                scores,
                row.get("source_place", row.get("place")),
                row.get("unsure_indices", []),
                rounds_count=rounds_count,
                use_strikes=use_strikes,
            )
        )

    return finalize_rows(normalized_rows)


def finalize_rows(rows: list[dict]) -> list[dict]:
    rows.sort(
        key=lambda row: (
            -(row["avg"] if row["avg"] is not None else -1),
            -(row["raw_avg"] if row["raw_avg"] is not None else -1),
            row.get("source_place", row.get("place", 10**9)),
            row["name"].lower(),
        )
    )
    for index, row in enumerate(rows, start=1):
        row["place"] = index
    return rows


def build_row(
    name: str,
    scores: list[int | None],
    source_place: int | None = None,
    unsure_indices: list[int] | None = None,
    rounds_count: int = TOTAL_ROUNDS,
    use_strikes: bool = True,
) -> dict:
    padded_scores = scores[:rounds_count]
    if len(padded_scores) < rounds_count:
        padded_scores = padded_scores + [None] * (rounds_count - len(padded_scores))
    valid_unsure_indices = sorted(
        index for index in (unsure_indices or []) if 0 <= index < len(padded_scores) and padded_scores[index] is not None
    )

    valid_scores = [score for score in padded_scores if score is not None]
    raw_avg = (sum(valid_scores) / len(valid_scores)) if valid_scores else None

    if len(valid_scores) >= 1:
        removed_indices = strike_indices(padded_scores) if use_strikes else []
        removed = [padded_scores[index] for index in removed_indices if padded_scores[index] is not None]
        kept = [score for index, score in enumerate(padded_scores) if score is not None and index not in removed_indices]
        avg = sum(kept) / len(kept) if kept else None
    else:
        removed_indices = []
        removed = []
        avg = raw_avg

    return {
        "name": name,
        "scores": padded_scores,
        "removed": removed,
        "removed_indices": removed_indices,
        "avg": avg,
        "raw_avg": raw_avg,
        "source_place": source_place,
        "unsure_indices": valid_unsure_indices,
    }


def limit_rows(
    rows: list[dict],
    visible_rounds: int | None,
    rounds_count: int = TOTAL_ROUNDS,
    use_strikes: bool = True,
) -> list[dict]:
    if visible_rounds is None:
        return rows

    limited_rows = []
    for row in rows:
        masked_scores = [
            score if index < visible_rounds else None
            for index, score in enumerate(row["scores"][:rounds_count])
        ]
        limited_rows.append(
            build_row(
                row["name"],
                masked_scores,
                row.get("source_place", row.get("place")),
                [index for index in row.get("unsure_indices", []) if index < visible_rounds],
                rounds_count=rounds_count,
                use_strikes=use_strikes,
            )
        )
    return finalize_rows(limited_rows)


def parse_document_rows(document: str, entry: bool = False, use_strikes: bool = True) -> list[dict]:
    rows = parse_rendered_rows(document, entry=entry, use_strikes=use_strikes)
    if rows:
        return rows
    return parse_pasted_rows(document, entry=entry, use_strikes=use_strikes)


def parse_pdf_rows(document: str, entry: bool = False, use_strikes: bool = True) -> list[dict]:
    rows = []
    total_rounds = rounds_limit(entry)
    for raw_line in document.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^(\d+)\s+([A-Za-z]\S*)\s+(.+?)\s+(\d+(?::\d+)+)$", line)
        if not match:
            continue
        tokens = match.group(3).split()
        if len(tokens) < 4:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", tokens[-1]):
            tokens = tokens[:-1]
        if len(tokens) < 2:
            continue
        score_tokens = tokens[::2]
        scores: list[int | None] = []
        for token in score_tokens[:total_rounds]:
            if re.fullmatch(r"-?\d+", token):
                scores.append(int(token))
            else:
                scores.append(None)
        if len([score for score in scores if score is not None]) < 1:
            continue
        rows.append(
            build_row(
                match.group(2),
                scores,
                source_place=int(match.group(1)),
                rounds_count=total_rounds,
                use_strikes=use_strikes,
            )
        )
    return finalize_rows(rows)


def parse_pasted_rows(document: str, entry: bool = False, use_strikes: bool = True) -> list[dict]:
    lines = [line.strip() for line in document.splitlines()]
    rows = []
    i = 0

    while i < len(lines):
        line = lines[i]
        match = re.match(r"^(\d+)\s+(.+)$", line)
        if not match:
            i += 1
            continue

        team_name = match.group(2).strip()
        i += 1

        while i < len(lines):
            current = lines[i]
            if not current:
                i += 1
                continue
            if re.search(r"\d+\s*\([^)]*\)", current):
                break
            if re.match(r"^\d+\s+.+$", current):
                team_name = ""
                break
            i += 1

        if not team_name:
            continue
        if i >= len(lines):
            break

        score_parts: list[str] = []
        total_line = ""
        while i < len(lines):
            current = lines[i]
            if not current:
                i += 1
                continue
            if re.match(r"^\d+\s*\([^)]*\)\s*$", current):
                total_line = current
                break
            if re.match(r"^\d+\s+.+$", current):
                break
            score_parts.append(current)
            i += 1

        scores_text = " ".join(score_parts)
        scores = [int(value) for value in re.findall(r"(\d+)\s*\([^)]*\)", scores_text)]
        if len(scores) >= 3:
            rows.append(build_row(team_name, scores, rounds_count=rounds_limit(entry), use_strikes=use_strikes))

        if total_line:
            i += 1

    return finalize_rows(rows)


def heat_color(value: float, max_value: float) -> str:
    if max_value <= 0:
        return DIM
    ratio = max(0.0, min(1.0, value / max_value))
    red = round(255 * (1.0 - ratio))
    green = round(255 * ratio)
    return f"\033[38;2;{red};{green};0m"


def team_color(index: int, background: bool = False) -> str:
    code = TEAM_COLOR_CODES[index % len(TEAM_COLOR_CODES)]
    return f"\033[{48 if background else 38};5;{code}m"


def team_color_map(rows: list[dict]) -> dict[str, int]:
    # Keep a smooth palette, but assign positions by a stable name-seeded shuffle.
    ordered_names = sorted(
        (row["name"] for row in rows),
        key=lambda name: (zlib.crc32(name.encode("utf-8")), name.lower()),
    )
    return {name: index for index, name in enumerate(ordered_names)}


def worst_two_indices(scores: list[int | None]) -> list[int]:
    valid = [(index, score) for index, score in enumerate(scores) if score is not None]
    valid.sort(key=lambda item: (item[1], item[0]))
    return [index for index, _ in valid[:2]]


def strike_count(scores: list[int | None]) -> int:
    valid_count = sum(1 for score in scores if score is not None)
    return min(valid_count, int((valid_count * 0.13) + 0.5))


def strike_indices(scores: list[int | None]) -> list[int]:
    removed_count = strike_count(scores)
    if removed_count <= 0:
        return []
    valid = [(index, score) for index, score in enumerate(scores) if score is not None]
    valid.sort(key=lambda item: (item[1], item[0]))
    return [index for index, _ in valid[:removed_count]]


def column_best_scores(rows: list[dict], rounds_count: int = TOTAL_ROUNDS) -> list[int]:
    if not rows:
        return []

    max_runs = max(rounds_count, max(len(row["scores"]) for row in rows))
    best_scores = []
    for index in range(max_runs):
        best = 0
        for row in rows:
            if index < len(row["scores"]) and row["scores"][index] is not None:
                best = max(best, row["scores"][index])
        best_scores.append(best)
    return best_scores


def projected_avg(
    scores: list[int | None],
    fill_score: float,
    removed_indices: list[int] | None = None,
    rounds_count: int = TOTAL_ROUNDS,
    use_strikes: bool = True,
) -> float:
    completed = [score if score is not None else fill_score for score in scores[:rounds_count]]
    if len(completed) < rounds_count:
        completed.extend([fill_score] * (rounds_count - len(completed)))
    if removed_indices is None:
        removed_indices = strike_indices(completed) if use_strikes else []
    kept = [score for index, score in enumerate(completed) if index not in removed_indices]
    return sum(kept) / len(kept)


def needed_fill_score(
    scores: list[int | None],
    target_avg: float,
    removed_indices: list[int] | None = None,
    max_fill: int | None = None,
    rounds_count: int = TOTAL_ROUNDS,
    use_strikes: bool = True,
) -> int | None:
    missing = sum(1 for score in scores[:rounds_count] if score is None)
    if missing == 0:
        return 0 if projected_avg(scores, 0, removed_indices, rounds_count, use_strikes) >= target_avg else None

    if projected_avg(scores, 0, removed_indices, rounds_count, use_strikes) >= target_avg:
        return 0

    if max_fill is not None:
        if projected_avg(scores, max_fill, removed_indices, rounds_count, use_strikes) < target_avg:
            return None
        high = max_fill
    else:
        high = 1
        while projected_avg(scores, high, removed_indices, rounds_count, use_strikes) < target_avg and high < 100000:
            high *= 2
        if projected_avg(scores, high, removed_indices, rounds_count, use_strikes) < target_avg:
            return None

    if high <= 0:
        return None

    low = 0
    while low + 1 < high:
        mid = (low + high) // 2
        if projected_avg(scores, mid, removed_indices, rounds_count, use_strikes) >= target_avg:
            high = mid
        else:
            low = mid
    return high


def needed_fill_score_normalized(
    scores: list[int | None], target_avg: float, rounds_count: int = TOTAL_ROUNDS, use_strikes: bool = True
) -> int | None:
    return needed_fill_score(scores, target_avg, None, 100, rounds_count, use_strikes)


def needed_values_for_rows(
    rows: list[dict],
    normalized: bool,
    keepaverage: bool,
    target_place: int = 1,
    rounds_count: int = TOTAL_ROUNDS,
    use_strikes: bool = True,
) -> list[int | None]:
    if not rows:
        return []
    target_index = min(max(1, target_place), len(rows)) - 1
    if rows[target_index]["avg"] is None:
        return [None] * len(rows)

    target_fill = rows[target_index]["raw_avg"] if keepaverage else 0
    if target_fill is None:
        target_fill = 0
    target_avg = projected_avg(rows[target_index]["scores"], target_fill, rounds_count=rounds_count, use_strikes=use_strikes)
    needed_values = []
    if normalized:
        for index, row in enumerate(rows):
            if index == target_index:
                needed_values.append(0)
            else:
                needed_values.append(needed_fill_score_normalized(row["scores"], target_avg, rounds_count, use_strikes))
        return needed_values
    for index, row in enumerate(rows):
        if index == target_index:
            needed_values.append(0)
        else:
            needed_values.append(needed_fill_score(row["scores"], target_avg, rounds_count=rounds_count, use_strikes=use_strikes))
    return needed_values


def sigma_text(scores: list[int | None]) -> str:
    valid_scores = [score for score in scores if score is not None]
    if len(valid_scores) < 2:
        return "~/"
    mean = sum(valid_scores) / len(valid_scores)
    if mean == 0:
        return "~/"
    variance = sum((score - mean) ** 2 for score in valid_scores) / len(valid_scores)
    cv = (variance ** 0.5) / mean
    return f"~{round(cv * 100)}%"


def load_display_rows(
    normalized: bool = False, visible_rounds: int | None = None, entry: bool = False, use_strikes: bool = True
) -> list[dict]:
    total_rounds = rounds_limit(entry)
    rows = load_rows(entry=entry, use_strikes=use_strikes)
    if not rows:
        return []
    rows = limit_rows(rows, visible_rounds, total_rounds, use_strikes)
    if normalized:
        rows = normalize_rows(rows, total_rounds, use_strikes)
    return rows


def print_scoreboard(
    normalized: bool = False,
    keepaverage: bool = False,
    entry: bool = False,
    visible_rounds: int | None = None,
    target_place: int = 1,
    show_sum: bool = False,
    show_relative: bool = False,
    use_strikes: bool = True,
    final_mode: bool = False,
    fullname: bool = False,
) -> int:
    clear_screen()
    total_rounds = rounds_limit(entry)

    rows = load_display_rows(normalized=normalized, visible_rounds=visible_rounds, entry=entry, use_strikes=use_strikes)
    if not rows:
        html_cache_file, _ = cache_files(entry)
        if html_cache_file.exists():
            print(f"{RED}no scoreboard found in cached page{RESET}")
            return 1
        print(f"{RED}missing score data{RESET}")
        return 1

    max_name = max(len(display_name(row["name"], fullname)) for row in rows)
    max_runs = max(total_rounds, max(len(row["scores"]) for row in rows))
    visible_scores = [score for row in rows for score in row["scores"] if score is not None]
    heat_max = 100 if normalized else max(visible_scores, default=0)
    score_width = max(
        3,
        max((len(str(score)) for score in visible_scores), default=1),
    )
    needed_values = needed_values_for_rows(rows, normalized, keepaverage, target_place, total_rounds, use_strikes)
    target_index = min(max(1, target_place), len(rows)) - 1
    max_avg = max((row["avg"] or 0) for row in rows)
    max_score_count = max((sum(1 for score in row["scores"] if score is not None) for row in rows), default=0)
    has_score_gaps = any(
        any(score is None for score in row["scores"][: max((index + 1 for index, score in enumerate(row["scores"]) if score is not None), default=0)])
        for row in rows
    )
    avg_texts = []
    needed_texts = []
    relative_texts = []
    spread_texts = []
    for row_index, (row, needed) in enumerate(zip(rows, needed_values)):
        has_visible_scores = any(score is not None for score in row["scores"])
        if row["avg"] is None or not has_visible_scores:
            avg_texts.append("/")
        elif show_sum:
            strike_count = len(row.get("removed_indices", []))
            predicted_sum = round(row["avg"] * max(0, max_score_count - strike_count))
            avg_texts.append(str(predicted_sum))
        elif show_relative:
            relative_avg = ((row["avg"] / max_avg) * 100) if max_avg > 0 else 0
            avg_texts.append(f"{round(relative_avg)}%")
        else:
            avg_texts.append(str(round(row["avg"])))
        if not has_visible_scores:
            needed_text = "/"
            relative_text = "/"
        else:
            needed_text = "/" if needed is None else str(needed)
            if needed is None or not row["avg"]:
                relative_text = "/"
            elif needed == 0 and row_index == target_index:
                relative_text = "0%"
            else:
                relative_percent = round((needed / row["avg"]) * 100) - 100
                relative_text = f"{relative_percent}%"
        needed_texts.append(needed_text)
        relative_texts.append(relative_text)
        spread_texts.append(sigma_text(row["scores"]) if has_visible_scores else "/")

    avg_width = max(MIN_AVG_WIDTH, max((len(text) for text in avg_texts), default=0))
    spread_width = max(MIN_SPREAD_WIDTH, max((len(text) for text in spread_texts), default=0))
    abs_width = max(MIN_ABS_WIDTH, max((len(text) for text in needed_texts), default=0))
    relative_width = max(MIN_RELATIVE_WIDTH, max((len(text) for text in relative_texts), default=0))
    score_headers = " ".join(f"{index:>{score_width}}" for index in range(1, max_runs + 1))
    left_pad = " " * (max_name + 4)
    if final_mode:
        print(
            f" {left_pad}{score_headers}  "
            f" {BOLD}{'A':>{avg_width}}{RESET} "
            f"   {BOLD}{'B':>{spread_width}}{RESET}"
        )
    else:
        print(
            f" {left_pad}{score_headers}  "
            f" {BOLD}{'A':>{avg_width}}{RESET} "
            f"   {BOLD}{'B':>{abs_width}}{RESET} "
            f"{BOLD}{'C':>{relative_width}}{RESET} "
            f"{BOLD}{'D':>{spread_width}}{RESET}"
        )
    print()

    for row_index, (row, needed) in enumerate(zip(rows, needed_values)):
        has_visible_scores = any(score is not None for score in row["scores"])
        scores = []
        for score_index, score in enumerate(row["scores"]):
            if score is None:
                scores.append(f"{DIM}{'/':>{score_width}}{RESET}")
                continue
            color = heat_color(score, heat_max)
            style = UNDERLINE if score_index in row.get("unsure_indices", []) else ""
            if score_index in row.get("removed_indices", []):
                scores.append(f"{color}{style}{STRIKE}{score:>{score_width}}{RESET}")
            else:
                scores.append(f"{color}{style}{score:>{score_width}}{RESET}")
        if len(scores) < max_runs:
            scores.extend(f"{DIM}{'/':>{score_width}}{RESET}" for _ in range(max_runs - len(scores)))
        score_text = " ".join(scores)
        needed_text = needed_texts[row_index]
        relative_text = relative_texts[row_index]
        if relative_text == "/":
            needed_color = f"{DIM}{RED}"
        elif relative_text in {"0", "0%"}:
            needed_color = DIM
        elif relative_text.startswith("-"):
            needed_color = f"{DIM}{GREEN}"
        else:
            needed_color = f"{DIM}{RED}"
        avg_text = avg_texts[row_index]
        spread_text = spread_texts[row_index]
        stats_text = (
            f"{spread_text:>{spread_width}}"
            if final_mode
            else f"{needed_text:>{abs_width}} {relative_text:>{relative_width}} {spread_text:>{spread_width}}"
        )
        print(
            f"{row['place']:>2}. "
            f"{BOLD}{display_name(row['name'], fullname):<{max_name}}{RESET} "
            f"{score_text} "
            f"  "
            f"{BOLD}{avg_text:>{avg_width}}{RESET} "
            f"  {needed_color}({stats_text}){RESET}"
        )
    score_kind = "normalized " if normalized else ""
    target_assumption = "keep their raw average" if keepaverage else "fail all runs fully"
    print()
    if show_sum:
        print(f"{BOLD}A{RESET} - {'predicted sum of scores' if has_score_gaps else 'sum of scores'}")
    elif show_relative:
        print(f"{BOLD}A{RESET} - percentage of maximum final score")
    elif use_strikes:
        print(f"{BOLD}A{RESET} - current average with strikes")
    else:
        print(f"{BOLD}A{RESET} - current average without strikes")
    if final_mode:
        print(f"{BOLD}B{RESET} - coefficient of variation")
    else:
        print(
            f"{BOLD}B{RESET} - needed {score_kind}scores to place {target_place} "
            f"if they {target_assumption}"
        )
        print(f"{BOLD}C{RESET} - relative needed improvement from raw average")
        print(f"{BOLD}D{RESET} - coefficient of variation")
    print_end_spacing()
    return 0


def print_bars(
    normalized: bool = False,
    visible_rounds: int | None = None,
    entry: bool = False,
    use_strikes: bool = True,
    fullname: bool = False,
) -> int:
    clear_screen()

    rows = load_display_rows(normalized=normalized, visible_rounds=visible_rounds, entry=entry, use_strikes=use_strikes)
    if not rows:
        html_cache_file, _ = cache_files(entry)
        if html_cache_file.exists():
            print(f"{RED}no scoreboard found in cached page{RESET}")
            return 1
        print(f"{RED}missing score data{RESET}")
        return 1

    max_avg = max((row["avg"] or 0) for row in rows)
    color_map = team_color_map(rows)
    values = [max(0.0, row["avg"] or 0.0) for row in rows]
    total = sum(values)
    heights = []
    colors = []
    for row in rows:
        row_avg = row["avg"] or 0
        height = round((row_avg / max_avg) * BAR_HEIGHT) if max_avg else 0
        heights.append(max(0, height - 1))
        colors.append(team_color(color_map[row["name"]]))

    for level in range(BAR_HEIGHT - 1, 0, -1):
        parts = []
        for height, color in zip(heights, colors):
            if height >= level:
                parts.append(f"{color}{BAR_FILL}{RESET}")
            else:
                parts.append(" " * BAR_WIDTH)
        print((" " * BAR_GAP).join(parts))

    print((" " * BAR_GAP).join(f"{color}{BAR_FILL}{RESET}" for color in colors))
    print(f"{DIM}{(' ' * BAR_GAP).join('▀' * BAR_WIDTH for _ in rows)}{RESET}")
    print((" " * BAR_GAP).join(f"{row['place']:^{BAR_WIDTH}}" for row in rows))
    print()
    for index, row in enumerate(rows):
        share = round((values[index] / total) * 100) if total > 0 else 0
        print(f"{team_color(color_map[row['name']])}{row['place']}. {display_name(row['name'], fullname)} {share}%{RESET}")

    print_end_spacing()
    return 0


def print_block(
    normalized: bool = False,
    visible_rounds: int | None = None,
    entry: bool = False,
    use_strikes: bool = True,
    fullname: bool = False,
) -> int:
    clear_screen()

    rows = load_display_rows(normalized=normalized, visible_rounds=visible_rounds, entry=entry, use_strikes=use_strikes)
    if not rows:
        html_cache_file, _ = cache_files(entry)
        if html_cache_file.exists():
            print(f"{RED}no scoreboard found in cached page{RESET}")
            return 1
        print(f"{RED}missing score data{RESET}")
        return 1

    rows = sorted(rows, key=lambda row: (-(row["avg"] or 0), row["place"]))
    values = [max(0.0, row["avg"] or 0.0) for row in rows]
    color_map = team_color_map(rows)
    total = sum(values)
    cell_count = PIE_SIZE * PIE_SIZE
    filled_cells = [0] * len(rows)
    if total > 0:
        raw_cells = [(value / total) * cell_count for value in values]
        filled_cells = [int(cell_value) for cell_value in raw_cells]
        remainder = cell_count - sum(filled_cells)
        order = sorted(
            range(len(rows)),
            key=lambda index: (raw_cells[index] - filled_cells[index], -index),
            reverse=True,
        )
        for index in order[:remainder]:
            filled_cells[index] += 1

    cell_colors = []
    for index, count in enumerate(filled_cells):
        cell_colors.extend([index] * count)
    if len(cell_colors) < cell_count:
        cell_colors.extend([-1] * (cell_count - len(cell_colors)))

    for row_index in range(PIE_SIZE):
        parts = []
        for column_index in range(PIE_SIZE):
            color_index = cell_colors[(row_index * PIE_SIZE) + column_index]
            if color_index < 0:
                parts.append("  ")
            else:
                parts.append(f"{team_color(color_map[rows[color_index]['name']], background=True)}  {RESET}")
        print("".join(parts))

    print()
    for index, row in enumerate(rows):
        share = round((values[index] / total) * 100) if total > 0 else 0
        print(f"{team_color(color_map[row['name']])}{row['place']}. {display_name(row['name'], fullname)} {share}%{RESET}")

    print_end_spacing()
    return 0


def print_pie(
    normalized: bool = False,
    visible_rounds: int | None = None,
    entry: bool = False,
    use_strikes: bool = True,
    fullname: bool = False,
) -> int:
    clear_screen()

    rows = load_display_rows(normalized=normalized, visible_rounds=visible_rounds, entry=entry, use_strikes=use_strikes)
    if not rows:
        html_cache_file, _ = cache_files(entry)
        if html_cache_file.exists():
            print(f"{RED}no scoreboard found in cached page{RESET}")
            return 1
        print(f"{RED}missing score data{RESET}")
        return 1

    rows = sorted(rows, key=lambda row: (-(row["avg"] or 0), row["place"]))
    values = [max(0.0, row["avg"] or 0.0) for row in rows]
    color_map = team_color_map(rows)
    total = sum(values)
    fractions = [value / total if total > 0 else 0.0 for value in values]
    cumulative = []
    current = 0.0
    for fraction in fractions:
        current += fraction
        cumulative.append(current)

    radius = PIE_SIZE / 2
    center = (PIE_SIZE - 1) / 2
    for row_index in range(PIE_SIZE):
        parts = []
        for column_index in range(PIE_SIZE):
            dx = column_index - center
            dy = row_index - center
            distance = math.hypot(dx, dy)
            if distance > radius:
                parts.append("  ")
                continue
            if total <= 0:
                parts.append("  ")
                continue
            angle = (math.atan2(dy, dx) + (math.pi / 2)) % (2 * math.pi)
            position = angle / (2 * math.pi)
            team_index = 0
            while team_index < len(cumulative) and position > cumulative[team_index]:
                team_index += 1
            team_index = min(team_index, len(rows) - 1)
            parts.append(f"{team_color(color_map[rows[team_index]['name']], background=True)}  {RESET}")
        print("".join(parts))

    print()
    for index, row in enumerate(rows):
        share = round((values[index] / total) * 100) if total > 0 else 0
        print(f"{team_color(color_map[row['name']])}{row['place']}. {display_name(row['name'], fullname)} {share}%{RESET}")

    print_end_spacing()
    return 0


def print_help() -> int:
    clear_screen()
    print("python3 main.py [refresh] [--refresh] [--pdf FILE] [--entry] [--bars] [--block] [--pie] [--normalized] [--sum] [--relative] [--nostrike] [--final] [--fullname] [--assumebad] [--to N] [--animate] [--help]")
    print()
    print("--refresh   paste scoreboard, ctrl-d")
    print("--pdf FILE  import local score pdf")
    print("--entry     use entry list")
    print("--bars      bars")
    print("--block     30x30 share block")
    print("--pie       round share pie")
    print("--normalized normalized mode")
    print("--sum       show predicted sum in A")
    print("--relative  show % of maximum final score in A")
    print("--nostrike  disable strike removal everywhere")
    print("--final     show only coefficient of variation as trailing column")
    print("--fullname  show full team names")
    print("--assumebad leader missing runs = 0")
    print("--to N      target place N")
    print("--animate   left/right, q quits")
    print("--help      this")
    print_end_spacing()
    return 0


def read_animation_key() -> str | None:
    first = sys.stdin.buffer.read(1)
    if not first:
        return "q"
    if first in {b"q", b"Q"}:
        return "q"
    if first == b"\x1b":
        second = sys.stdin.buffer.read(1)
        if second != b"[":
            return None
        third = sys.stdin.buffer.read(1)
        if third == b"C":
            return "right"
        if third == b"D":
            return "left"
    return None


def animate_view(
    normalized: bool = False,
    keepaverage: bool = False,
    entry: bool = False,
    bars: bool = False,
    block: bool = False,
    pie: bool = False,
    target_place: int = 1,
    show_sum: bool = False,
    show_relative: bool = False,
    use_strikes: bool = True,
    final_mode: bool = False,
    fullname: bool = False,
    pdf_path: str | None = None,
) -> int:
    total_rounds = rounds_limit(entry)
    rows = load_rows(entry=entry, use_strikes=use_strikes)
    if not rows:
        clear_screen()
        html_cache_file, _ = cache_files(entry)
        if html_cache_file.exists():
            print(f"{RED}no scoreboard found in cached page{RESET}")
            return 1
        print(f"{RED}missing score data{RESET}")
        return 1

    visible_rounds = 0
    stdin_fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(stdin_fd)
    try:
        tty.setcbreak(stdin_fd)
        while True:
            command = [sys.executable, __file__]
            if bars:
                command.append("--bars")
            if block:
                command.append("--block")
            if pie:
                command.append("--pie")
            if normalized:
                command.append("--normalized")
            if show_sum:
                command.append("--sum")
            if show_relative:
                command.append("--relative")
            if not use_strikes:
                command.append("--nostrike")
            if final_mode:
                command.append("--final")
            if fullname:
                command.append("--fullname")
            if pdf_path:
                command.extend(["--pdf", pdf_path])
            if not keepaverage:
                command.append("--assumebad")
            if entry:
                command.append("--entry")
            if target_place != 1:
                command.extend(["--to", str(target_place)])
            command.extend(["--visible-rounds", str(visible_rounds)])
            subprocess.run(command, check=False)
            key = read_animation_key()
            if key == "q":
                break
            if key == "right":
                visible_rounds = min(total_rounds, visible_rounds + 1)
            elif key == "left":
                visible_rounds = max(0, visible_rounds - 1)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
        clear_screen()
    return 0


def main() -> int:
    args = sys.argv[1:]
    normalized = "--normalized" in args
    show_sum = "--sum" in args
    show_relative = "--relative" in args
    use_strikes = "--nostrike" not in args
    final_mode = "--final" in args
    fullname = "--fullname" in args
    keepaverage = "--assumebad" not in args
    animate = "--animate" in args
    entry = "--entry" in args
    block = "--block" in args
    pie = "--pie" in args
    visible_rounds = None
    target_place = 1
    refresh_alias = any(arg.casefold() == "refresh" for arg in args)
    total_rounds = rounds_limit(entry)
    pdf_path = None

    if "--pdf" in args:
        index = args.index("--pdf")
        try:
            pdf_path = args[index + 1]
        except IndexError:
            print(f"{RED}missing pdf path{RESET}")
            return 1

    if "--visible-rounds" in args:
        index = args.index("--visible-rounds")
        try:
            visible_rounds = max(0, min(total_rounds, int(args[index + 1])))
        except (IndexError, ValueError):
            print(f"{RED}invalid visible rounds{RESET}")
            return 1
    if "--to" in args:
        index = args.index("--to")
        try:
            target_place = max(1, int(args[index + 1]))
        except (IndexError, ValueError):
            print(f"{RED}invalid target place{RESET}")
            return 1
    if "--help" in args:
        return print_help()
    if pdf_path is not None:
        return import_pdf_document(Path(pdf_path), entry=entry, use_strikes=use_strikes)
    if "--refresh" in args or refresh_alias:
        return refresh_document(entry=entry)
    if animate:
        return animate_view(
            normalized=normalized,
            keepaverage=keepaverage,
            entry=entry,
            bars="--bars" in args,
            block=block,
            pie=pie,
            target_place=target_place,
            show_sum=show_sum,
            show_relative=show_relative,
            use_strikes=use_strikes,
            final_mode=final_mode,
            fullname=fullname,
            pdf_path=pdf_path,
        )
    if "--bars" in args:
        return print_bars(
            normalized=normalized,
            visible_rounds=visible_rounds,
            entry=entry,
            use_strikes=use_strikes,
            fullname=fullname,
        )
    if block:
        return print_block(
            normalized=normalized,
            visible_rounds=visible_rounds,
            entry=entry,
            use_strikes=use_strikes,
            fullname=fullname,
        )
    if pie:
        return print_pie(
            normalized=normalized,
            visible_rounds=visible_rounds,
            entry=entry,
            use_strikes=use_strikes,
            fullname=fullname,
        )

    return print_scoreboard(
        normalized=normalized,
        keepaverage=keepaverage,
        entry=entry,
        visible_rounds=visible_rounds,
        target_place=target_place,
        show_sum=show_sum,
        show_relative=show_relative,
        use_strikes=use_strikes,
        final_mode=final_mode,
        fullname=fullname,
    )


if __name__ == "__main__":
    raise SystemExit(main())
