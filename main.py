from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
RENDER_SCRIPT = Path("render_dom.mjs")
CACHE_VERSION = 2

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
STRIKE = "\033[9m"

TOTAL_ROUNDS = 10
BAR_HEIGHT = 12
BAR_WIDTH = 1
BAR_GAP = 2
BAR_FILL = "█" * BAR_WIDTH


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


def refresh_document() -> int:
    clear_screen()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

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
        HTML_CACHE_FILE.write_text(pasted, encoding="utf-8")
        rows = parse_document_rows(pasted)
        if not rows:
            print(f"{RED}saved pasted data, but could not parse scoreboard{RESET}")
            return 1
        save_rows(rows)
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

    command = [node, str(RENDER_SCRIPT), browser, URL, str(HTML_CACHE_FILE)]
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

    document = HTML_CACHE_FILE.read_text(encoding="utf-8", errors="ignore") if HTML_CACHE_FILE.exists() else ""
    if result.returncode != 0 or not document.strip():
        print(f"{RED}refresh failed{RESET}")
        return 1

    rows = parse_document_rows(document)
    if not rows:
        print(f"{RED}no scoreboard found in rendered page{RESET}")
        return 1
    save_rows(rows)

    return 0


def load_document() -> str:
    if not HTML_CACHE_FILE.exists():
        return ""
    return HTML_CACHE_FILE.read_text(encoding="utf-8", errors="ignore")


def save_rows(rows: list[dict]) -> None:
    JSON_CACHE_FILE.write_text(
        json.dumps({"version": CACHE_VERSION, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json_rows() -> list[dict]:
    if not JSON_CACHE_FILE.exists():
        return []
    try:
        data = json.loads(JSON_CACHE_FILE.read_text(encoding="utf-8"))
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
            scores = [int(value) if value is not None else None for value in raw_scores]
            removed = [int(value) for value in item.get("removed", [])]
            removed_indices = [int(value) for value in item.get("removed_indices", [])]
            avg = float(item["avg"])
            raw_avg = float(item["raw_avg"])
            place = int(item["place"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            {
                "name": name,
                "scores": scores,
                "removed": removed,
                "removed_indices": removed_indices,
                "avg": avg,
                "raw_avg": raw_avg,
                "place": place,
            }
        )
    return rows


def parse_rendered_rows(document: str) -> list[dict]:
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

        if len(scores) < TOTAL_ROUNDS:
            scores.extend([None] * (TOTAL_ROUNDS - len(scores)))
        else:
            scores = scores[:TOTAL_ROUNDS]

        valid_scores = [score for score in scores if score is not None]
        if len(valid_scores) < 3:
            continue

        removed_indices = worst_two_indices(scores)
        removed = [scores[index] for index in removed_indices if scores[index] is not None]
        kept = [score for index, score in enumerate(scores) if score is not None and index not in removed_indices]
        avg = sum(kept) / len(kept)
        raw_avg = sum(valid_scores) / len(valid_scores)

        rows.append(
            {
                "name": team_name,
                "scores": scores,
                "removed": removed,
                "removed_indices": removed_indices,
                "avg": avg,
                "raw_avg": raw_avg,
            }
        )

    return finalize_rows(rows)


def load_rows() -> list[dict]:
    rows = load_json_rows()
    if rows:
        return rows

    document = load_document()
    if not document:
        return []
    rows = parse_document_rows(document)
    if rows:
        save_rows(rows)
    return rows


def normalize_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        return []

    max_runs = max(TOTAL_ROUNDS, max(len(row["scores"]) for row in rows))
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
        if len(scores) < TOTAL_ROUNDS:
            scores.extend([None] * (TOTAL_ROUNDS - len(scores)))

        valid_scores = [score for score in scores if score is not None]
        removed_indices = worst_two_indices(scores)
        removed = [scores[index] for index in removed_indices if scores[index] is not None]
        kept = [score for index, score in enumerate(scores) if score is not None and index not in removed_indices]
        avg = sum(kept) / len(kept)
        raw_avg = sum(valid_scores) / len(valid_scores)

        normalized_rows.append(
            {
                "name": row["name"],
                "scores": scores,
                "removed": removed,
                "removed_indices": removed_indices,
                "avg": avg,
                "raw_avg": raw_avg,
            }
        )

    return finalize_rows(normalized_rows)


def finalize_rows(rows: list[dict]) -> list[dict]:
    rows.sort(key=lambda row: (-row["avg"], -row["raw_avg"], row["name"].lower()))
    for index, row in enumerate(rows, start=1):
        row["place"] = index
    return rows


def parse_document_rows(document: str) -> list[dict]:
    rows = parse_rendered_rows(document)
    if rows:
        return rows
    return parse_pasted_rows(document)


def parse_pasted_rows(document: str) -> list[dict]:
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
        padded_scores: list[int | None] = scores[:TOTAL_ROUNDS]
        if len(padded_scores) < TOTAL_ROUNDS:
            padded_scores.extend([None] * (TOTAL_ROUNDS - len(padded_scores)))

        if len(scores) >= 3:
            removed_indices = worst_two_indices(padded_scores)
            removed = [padded_scores[index] for index in removed_indices if padded_scores[index] is not None]
            kept = [score for index, score in enumerate(padded_scores) if score is not None and index not in removed_indices]
            rows.append(
                {
                    "name": team_name,
                    "scores": padded_scores,
                    "removed": removed,
                    "removed_indices": removed_indices,
                    "avg": sum(kept) / len(kept),
                    "raw_avg": sum(scores) / len(scores),
                }
            )

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


def worst_two_indices(scores: list[int | None]) -> list[int]:
    valid = [(index, score) for index, score in enumerate(scores) if score is not None]
    valid.sort(key=lambda item: (item[1], item[0]))
    return [index for index, _ in valid[:2]]


def column_best_scores(rows: list[dict]) -> list[int]:
    if not rows:
        return []

    max_runs = max(TOTAL_ROUNDS, max(len(row["scores"]) for row in rows))
    best_scores = []
    for index in range(max_runs):
        best = 0
        for row in rows:
            if index < len(row["scores"]) and row["scores"][index] is not None:
                best = max(best, row["scores"][index])
        best_scores.append(best)
    return best_scores


def projected_avg(
    scores: list[int | None], fill_score: float, removed_indices: list[int] | None = None
) -> float:
    completed = [score if score is not None else fill_score for score in scores[:TOTAL_ROUNDS]]
    if len(completed) < TOTAL_ROUNDS:
        completed.extend([fill_score] * (TOTAL_ROUNDS - len(completed)))
    if removed_indices is None:
        removed_indices = worst_two_indices(completed)
    kept = [score for index, score in enumerate(completed) if index not in removed_indices]
    return sum(kept) / len(kept)


def needed_fill_score(
    scores: list[int | None],
    target_avg: float,
    removed_indices: list[int] | None = None,
    max_fill: int | None = None,
) -> int | None:
    missing = sum(1 for score in scores[:TOTAL_ROUNDS] if score is None)
    if missing == 0:
        return 0 if projected_avg(scores, 0, removed_indices) >= target_avg else None

    if projected_avg(scores, 0, removed_indices) >= target_avg:
        return 0

    if max_fill is not None:
        if projected_avg(scores, max_fill, removed_indices) < target_avg:
            return None
        high = max_fill
    else:
        high = 1
        while projected_avg(scores, high, removed_indices) < target_avg and high < 100000:
            high *= 2
        if projected_avg(scores, high, removed_indices) < target_avg:
            return None

    if high <= 0:
        return None

    low = 0
    while low + 1 < high:
        mid = (low + high) // 2
        if projected_avg(scores, mid, removed_indices) >= target_avg:
            high = mid
        else:
            low = mid
    return high


def needed_fill_score_normalized(scores: list[int | None], target_avg: float) -> int | None:
    return needed_fill_score(scores, target_avg, None, 100)


def needed_values_for_rows(rows: list[dict], normalized: bool, keepaverage: bool) -> list[int | None]:
    if not rows:
        return []

    leader_fill = rows[0]["raw_avg"] if keepaverage else 0
    target_avg = projected_avg(rows[0]["scores"], leader_fill)
    needed_values = [0]
    if normalized:
        needed_values.extend(
            needed_fill_score_normalized(row["scores"], target_avg) for row in rows[1:]
        )
        return needed_values
    needed_values.extend(needed_fill_score(row["scores"], target_avg) for row in rows[1:])
    return needed_values


def print_scoreboard(normalized: bool = False, relative: bool = False, keepaverage: bool = False) -> int:
    clear_screen()

    rows = load_rows()
    if not rows:
        if HTML_CACHE_FILE.exists():
            print(f"{RED}no scoreboard found in cached page{RESET}")
            return 1
        print(f"{RED}missing score data{RESET}")
        return 1
    if normalized:
        rows = normalize_rows(rows)

    max_name = max(len(row["name"]) for row in rows)
    max_runs = max(TOTAL_ROUNDS, max(len(row["scores"]) for row in rows))
    column_best = column_best_scores(rows)
    score_width = max(
        3,
        max(len(str(score)) for row in rows for score in row["scores"] if score is not None),
    )
    avg_width = max(3, max(len(str(round(row["avg"]))) for row in rows))
    needed_values = needed_values_for_rows(rows, normalized, keepaverage)
    needed_width = max(
        2,
        max(len(str(value)) if value is not None else 1 for value in needed_values),
    )

    for row, needed in zip(rows, needed_values):
        scores = []
        for index, score in enumerate(row["scores"]):
            if score is None:
                scores.append(f"{DIM}{'-':>{score_width}}{RESET}")
                continue
            color = heat_color(score, column_best[index])
            if index in row.get("removed_indices", []):
                scores.append(f"{color}{STRIKE}{score:>{score_width}}{RESET}")
            else:
                scores.append(f"{color}{score:>{score_width}}{RESET}")
        if len(scores) < max_runs:
            scores.extend(f"{DIM}{'-':>{score_width}}{RESET}" for _ in range(max_runs - len(scores)))
        score_text = " ".join(scores)
        if relative:
            if needed is None or row["avg"] == 0:
                needed_text = "-"
            else:
                needed_text = f"{needed / row['avg']:.2f}"
        else:
            needed_text = "-" if needed is None else str(needed)
        print(
            f"{row['place']:>2}. "
            f"{BOLD}{row['name']:<{max_name}}{RESET} "
            f"{score_text} "
            f"  "
            f"{BOLD}{round(row['avg']):>{avg_width}}{RESET} "
            f"({DIM}+{needed_text}{RESET})"
        )
    return 0


def print_bars(normalized: bool = False) -> int:
    clear_screen()

    rows = load_rows()
    if not rows:
        if HTML_CACHE_FILE.exists():
            print(f"{RED}no scoreboard found in cached page{RESET}")
            return 1
        print(f"{RED}missing score data{RESET}")
        return 1
    if normalized:
        rows = normalize_rows(rows)

    max_avg = max(row["avg"] for row in rows)
    heights = []
    colors = []
    for row in rows:
        height = round((row["avg"] / max_avg) * BAR_HEIGHT) if max_avg else 0
        heights.append(max(1, height) if row["avg"] > 0 else 0)
        colors.append(heat_color(row["avg"], max_avg))

    for level in range(BAR_HEIGHT, 0, -1):
        parts = []
        for height, color in zip(heights, colors):
            if height >= level:
                parts.append(f"{color}{BAR_FILL}{RESET}")
            else:
                parts.append(" " * BAR_WIDTH)
        print((" " * BAR_GAP).join(parts))

    print(f"{DIM}{(' ' * BAR_GAP).join('▀' * BAR_WIDTH for _ in rows)}{RESET}")
    print((" " * BAR_GAP).join(f"{row['place']:^{BAR_WIDTH}}" for row in rows))
    print()
    for row in rows:
        print(f"{row['place']}. {row['name']}")

    return 0


def print_help() -> int:
    clear_screen()
    print("python3 main.py [--refresh] [--bars] [--normalized] [--relative] [--keepaverage] [--help]")
    print()
    print("--refresh     paste scoreboard text, then ctrl-d")
    print("--bars        show bar view")
    print("--normalized  normalize each round to the best score in that round")
    print("--relative    show +needed divided by average_without_strikes")
    print("--keepaverage use leader raw average over known runs for missing runs in +needed target")
    print("--help        show this help")
    return 0


def main() -> int:
    args = sys.argv[1:]
    normalized = "--normalized" in args
    relative = "--relative" in args
    keepaverage = "--keepaverage" in args

    if "--help" in args:
        return print_help()
    if "--refresh" in args:
        return refresh_document()
    if "--bars" in args:
        return print_bars(normalized=normalized)

    return print_scoreboard(normalized=normalized, relative=relative, keepaverage=keepaverage)


if __name__ == "__main__":
    raise SystemExit(main())
