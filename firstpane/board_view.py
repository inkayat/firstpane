"""Board tab: a read-only view of the fm-board projection.

Refresh is one in-flight read. A failed or slow read keeps the last payload
and says so. This tab does not create, hold, or otherwise write tasks.
"""

import json
from pathlib import Path

from firstpane.agents_view import _fg, _on, _plural, age_label
from firstpane.board import (
    COLUMNS, COLUMN_GLYPH, DONE, attach_projection,
    column_tasks, command_env, fm_board_path, parse_list, reconcile_note,
    summary_facts, unavailable, visible_tasks,
)
from firstpane.board_cards import (
    done_row, files_text, glyph_color, join_lanes, lane_header, lane_rows, lane_widths, more_row, rule, spacious, window,
)
from firstpane.cells import clip, fit, pad, spread, width
from firstpane.fmmeta import load_metas
from firstpane.lineedit import LineEdit
from firstpane.sanitize import sanitize
from firstpane.style import ACCENT, BG, BORDER, BRIGHT, FIELD_BG, INK, MUTED, PINK, SELECTED_BG, SEND_BG, YELLOW

BOARD_CADENCE = 5
LIST_TIMEOUT = 20
WIDE = 100
TALL = 36
MIN_BODY = 7
MIN_LANES = 8
SEARCH_PLACEHOLDER = "Search tasks\u2026"
ACTIVE_FILTERS = (("all", "All tasks"), ("active", "Active tasks"))
# Pieces give up rows in this order until the lanes keep room for a header and a card.
SHRINK = (
    ("more", 0), ("filters", 1), ("composer", 3), ("detail", 2), ("composer", 2), ("counts", 0),
    ("footer", 0), ("detail", 1), ("composer", 1), ("banner", 0), ("detail", 0), ("filters", 0),
)


class BoardMixin:
    def _init_board(self):
        self.board_tasks = []
        self.board_error = None
        self.board_stderr = None
        self.board_summary = {"present": False, "lines": []}
        self.board_loaded_at = None
        self.board_source = None
        self.board_generated_at = None
        self.board_job = None
        self.board_job_kind = None
        self.board_project = ""
        self.board_active = "all"
        self.board_column = "todo"
        self.board_selected = None
        self.board_done_open = False
        self.board_detail = False
        self.board_search = LineEdit(multiline=False)
        self.board_scroll = {}

    def _board_env(self):
        return command_env(self.env, self.config.firstmate_home, self.config.firstmate_root)

    def _refresh_board(self, force=False):
        if self.tab != "board" and not force:
            return
        if self.board_job is not None:
            return
        reason = unavailable(self.config.firstmate_home, self.config.firstmate_root, self.env)
        if reason:
            self.board_error = reason
            return
        now = self.clock()
        due = force or self.board_loaded_at is None or now - self.board_loaded_at >= BOARD_CADENCE
        if not due:
            return
        self.board_job_kind = "list"
        self.board_job = self.submit(self._load_board)

    def _load_board(self):
        started = self.wall()
        env = self._board_env()
        script = fm_board_path(self.env)
        result = self.transport.run(
            [script, "list", "--json"],
            env=env,
            timeout=LIST_TIMEOUT,
            cwd=str(self.state_dir),
        )
        summary = self._read_summary()
        if result.timed_out:
            return {
                "kind": "list",
                "error": "list refresh timed out",
                "stderr": result.stderr,
                "summary": summary,
            }
        if result.code != 0:
            return {"kind": "list", "error": f"list failed ({result.code})", "stderr": result.stderr, "summary": summary}
        tasks = parse_list(result.stdout)
        if tasks is None:
            return {"kind": "list", "error": "list returned no JSON", "stderr": result.stderr, "summary": summary}
        tasks = attach_projection(tasks, self._read_projection())
        return {
            "kind": "list",
            "tasks": tasks,
            "stderr": result.stderr,
            "summary": summary,
            "source": "list",
        }

    def _read_summary(self):
        path = Path(self.config.firstmate_home) / "state" / "home-summary.json"
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return summary_facts(None, self.wall())
        return summary_facts(payload, self.wall())

    def _read_projection(self):
        path = Path(self.config.firstmate_home) / "data" / "board" / "state.json"
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _poll_board(self):
        job = self.board_job
        if job is None or not job.done():
            return
        self.board_job = None
        kind = self.board_job_kind
        self.board_job_kind = None
        try:
            result = job.result()
        except Exception as exc:
            result = {"kind": kind, "error": sanitize(str(exc))[:240]}
        self._apply_list(result)

    def _apply_list(self, result):
        self.board_loaded_at = self.clock()
        self.board_summary = result.get("summary") or self.board_summary
        self.board_stderr = reconcile_note(result.get("stderr"))
        if result.get("error") or "tasks" not in result:
            self.board_error = result.get("error") or "list refresh failed"
            return
        first = self.board_source is None
        self.board_error = None
        self.board_tasks = result.get("tasks") or []
        self.board_source = result.get("source") or "list"
        self.board_generated_at = None
        if first and self.board_selected is None and self.board_column != DONE[0]:
            self._board_first_selection()

    def _board_first_selection(self):
        """The first card of the current lane (else the first lane with one), once, on first load."""
        grouped, _ = self._board_grouped()
        keys = [key for key, _ in self._board_lanes(grouped)]
        order = ([self.board_column] if self.board_column in keys else []) + keys
        for key in order:
            if grouped.get(key):
                self._select_board(grouped[key][0]["id"], key)
                return

    def _board_shown(self):
        return visible_tasks(
            self.board_tasks,
            self.board_project,
            self.board_search.text,
            self.board_active == "active",
        )

    def _board_lanes(self, grouped):
        """TODO, Working and Waiting Review always; Blocked only while it holds a task."""
        return [(key, label) for key, label in COLUMNS if key != "blocked" or grouped.get(key)]

    def _board_stops(self, grouped):
        return [key for key, _ in self._board_lanes(grouped)] + [DONE[0]]

    def _board_grouped(self):
        return column_tasks(self._board_shown())

    def _select_board(self, task_id, column=None):
        if self.board_selected != task_id:
            self._close_inline()
        self.board_selected = task_id
        if column:
            self.board_column = column
        self.board_detail = False

    def _board_card(self):
        return next((task for task in self.board_tasks if task.get("id") == self.board_selected), None)

    def _move_board(self, key):
        grouped, other = self._board_grouped()
        stops = self._board_stops(grouped)
        if other and "other" not in stops:
            stops = stops + ["other"]
        if not stops:
            return
        if self.board_column not in stops:
            self.board_column = stops[0]
        column = self.board_column
        cards = other if column == "other" else grouped.get(column, [])
        index = next((i for i, task in enumerate(cards) if task.get("id") == self.board_selected), 0)
        if key in ("left", "right", "h", "l"):
            step = -1 if key in ("left", "h") else 1
            self._board_goto_lane(stops[(stops.index(column) + step) % len(stops)], index)
            return
        if not cards:
            return
        if key in ("down", "j"):
            index = min(index + 1, len(cards) - 1)
        elif key in ("up", "k"):
            index = max(index - 1, 0)
        self._select_board(cards[index]["id"], column)

    def _board_goto_lane(self, column, index=0):
        """Move to a lane, selecting the card at `index` (clamped); a collapsed Done or an empty
        lane selects none."""
        grouped, other = self._board_grouped()
        cards = other if column == "other" else grouped.get(column, [])
        if cards and not (column == DONE[0] and not self.board_done_open):
            self._select_board(cards[min(index, len(cards) - 1)]["id"], column)
        else:
            self._select_board(None, column)

    def _board_live_rows(self):
        """Live agent row of each task FirstMate bound to exactly one meta and Herdr still shows."""
        by_task = {}
        for meta in load_metas(self.config.firstmate_home):
            by_task.setdefault(meta.get("task_id"), []).append(meta)
        live = {}
        for project in self.projects:
            for agent in project.get("agents") or []:
                metas = by_task.get(agent.get("task_id"))
                if metas and len(metas) == 1 and agent.get("pane_id") == metas[0].get("herdr_pane_id"):
                    live.setdefault(agent["task_id"], agent)
        return live

    def _board_agent_row(self, task_id):
        return self._board_live_rows().get(task_id) if task_id else None

    def _board_go_agent(self):
        row = self._board_agent_row(self.board_selected)
        if row is None:
            card = self._board_card()
            self.status = "agent not open in Herdr" if card and card.get("agent") else "no agent assigned"
            return
        self._focus(row)

    # ---- filing ----------------------------------------------------------------------
    def _open_board_composer(self):
        card = self._board_card()
        if card is None:
            self.status = "select a task to write about"
            return
        target = "board:" + card["id"]
        record = {
            "kind": "board",
            "id": card["id"],
            "title": card.get("title") or card["id"],
            "project": card.get("project") or "",
            "state": card.get("state") or "",
            "hold_reason": card.get("hold_reason"),
        }
        draft = self.drafts.load(target, {"task_id": card["id"], "project_label": card.get("project")})
        others = [item for item in draft.get("attachments") or [] if item.get("kind") != "board"]
        draft["attachments"] = [record] + others
        self._open_draft(draft)

    # ---- keys and mouse --------------------------------------------------------------
    def _board_key(self, key):
        if key == "/":
            self.focus = "search"
            return True
        if key == "f":
            names = [name for name, _ in ACTIVE_FILTERS]
            self.board_active = names[(names.index(self.board_active) + 1) % len(names)]
            return True
        if key == "p":
            projects = [""]
            for task in self.board_tasks:
                name = task.get("project") or ""
                if name and name not in projects:
                    projects.append(name)
            index = projects.index(self.board_project) if self.board_project in projects else 0
            self.board_project = projects[(index + 1) % len(projects)]
            return True
        if key == "g":
            self._board_go_agent()
            return True
        if key in ("tab", "m"):
            self._open_board_composer()
            return True
        if key == "enter":
            if self.board_column == DONE[0] and not self.board_selected:
                self.board_done_open = not self.board_done_open
            else:
                self.board_detail = not self.board_detail
            return True
        if key in ("left", "right", "up", "down", "h", "j", "k", "l"):
            self._move_board(key)
            return True
        return False

    def _board_mouse(self, hit, column):
        kind = hit.get("kind")
        if kind == "board-filter":
            if column >= hit.get("active_at", 10**9):
                self._board_key("f")
            elif column >= hit.get("search_at", 0):
                self.focus = "search"
            else:
                self._board_key("p")
            return
        if kind == "board-done":
            self.board_column = DONE[0]
            self.board_done_open = not self.board_done_open
            self._select_board(None, DONE[0])
            return
        if kind == "board-agent":
            if column >= hit.get("go_at", 0):
                self._board_go_agent()
            return
        if kind == "board-composer":
            self._board_composer_click(hit, column)
            return
        if kind == "board-stop":
            for start, end, key in hit.get("spans") or []:
                if start <= column <= end:
                    self._board_goto_lane(key)
                    return
            return
        if kind == "board-row":
            for start, end, task_id, state in hit.get("spans") or []:
                if start <= column <= end:
                    if task_id:
                        self._select_board(task_id, state)
                    elif state != self.board_column:
                        self._board_goto_lane(state)  # empty space in a lane: go there, never deselect
                    return
        if kind == "board-card":
            self._select_board(hit.get("id"), hit.get("state"))

    def _board_editing(self):
        return self.focus == "composer" and self.draft is not None and str(self.draft.get("target") or "").startswith("board:")

    def _board_composer_click(self, hit, column):
        """Chip x closes the open editor (the draft stays saved); Send previews the transfer;
        anywhere else in the message block opens the selected task's message."""
        if hit.get("part") == "chip" and self._board_editing() and column >= hit.get("dismiss", 10**9):
            self._close_inline()
            return
        if hit.get("part") == "field" and column >= hit.get("send_at", 10**9):
            if self._board_card() is None:
                self.status = "select a task to write about"
                return
            if not self._board_editing():
                self._open_board_composer()
            if self.draft is not None and self.composer.text.strip():
                self._arm_send()
            else:
                self.status = "nothing to send: write the message first"
            return
        self._open_board_composer()

    # ---- drawing ---------------------------------------------------------------------
    def _board_layout(self, banner, reasons):
        """Rows each piece takes. Pieces shrink and then drop, in this order, until the lanes
        have room for a header and a card; a status message keeps the footer row."""
        layout = {
            "filters": 3 if self.rows >= TALL else 1, "counts": 1, "banner": 1 if banner else 0,
            "done": 1 if self.cols >= WIDE else 0, "detail": 3, "more": reasons, "composer": 5, "footer": 1,
        }
        for name, rows in SHRINK:
            if self.rows - 1 - sum(layout.values()) >= MIN_BODY:
                break
            if name == "footer" and self.status:
                continue
            layout[name] = min(layout[name], rows)
        return layout

    def _board_frame(self, plain):
        banner = self._board_banner(plain)
        card = self._board_card()
        reasons = self._board_reasons(card) if self.board_detail and card else []
        layout = self._board_layout(banner, len(reasons))
        live = self._board_live_rows()
        top = [(self._tab_row(plain), {"kind": "tabs", "key": ("tabs",)})]
        if layout["filters"] == 3:
            top.extend(self._board_filter_box(plain))
        elif layout["filters"]:
            top.append(self._board_filter_line(plain))
        if layout["counts"]:
            top.append((self._board_counts(plain), None))
        if layout["banner"] and banner:
            top.append((banner, None))
        bottom = []
        if layout["done"]:
            bottom.append(self._board_done_line(plain))
        bottom.extend(self._board_detail_lines(plain, layout["detail"], reasons[: layout["more"]], live))
        bottom.extend(self._board_composer_lines(plain, layout["composer"]))
        if layout["footer"]:
            bottom.append((self._board_footer(plain), None))
        budget = max(1, self.rows - len(top) - len(bottom))
        body = self._board_body(plain, budget, live)
        body += [("", None)] * (budget - len(body))
        lines = (top + body + bottom)[: self.rows]
        self.hit_rows = [hit for _, hit in lines]
        painted = []
        for text, _hit in lines:
            line = pad(text, self.cols) if plain else _on(pad(text, self.cols), BG, False)
            painted.append(fit(line, self.cols))
        return "\n".join(painted)

    def _board_filter_line(self, plain):
        project = self.board_project or "All projects"
        active = dict(ACTIVE_FILTERS)[self.board_active]
        focused = self.focus == "search"
        left = _fg(clip(project, 18) + " \u25be", ACCENT, plain)
        search_at = 2 + width(left) + 2
        right = _fg(active + " \u25be", ACCENT, plain)
        limit = max(4, self.cols - search_at - width(right) - 6)
        field = _fg("\u2315", ACCENT if focused else MUTED, plain) + " " + self._field_text(
            self.board_search.text, SEARCH_PLACEHOLDER, focused, self.board_search.cursor, limit, plain)
        text = spread(" " + left + "  " + field, right + " ", self.cols)
        return text, {"kind": "board-filter", "key": ("board-filter",), "search_at": search_at, "active_at": self.cols - width(right)}

    def _board_filter_box(self, plain):
        """Three-row filters: project, search, active, each in its own box."""
        project = self.board_project or "All projects"
        project_text = clip(project, 20) + " \u25be"
        project_w = max(16, width(project_text) + 4)
        active_w = width(max(dict(ACTIVE_FILTERS).values(), key=len)) + 6
        search_w = self.cols - project_w - active_w - 4
        if search_w < 14:
            return [self._board_filter_line(plain)]
        focused = self.focus == "search"
        field = _fg("\u2315", ACCENT if focused else MUTED, plain) + " " + self._field_text(
            self.board_search.text, SEARCH_PLACEHOLDER, focused, self.board_search.cursor, search_w - 6, plain)
        active_text = dict(ACTIVE_FILTERS)[self.board_active] + " \u25be"
        boxes = (
            (project_w, _fg(project_text, ACCENT, plain), BORDER),
            (search_w, field, ACCENT if focused else BORDER),
            (active_w, _fg(active_text, ACCENT, plain), BORDER),
        )
        rows = ["", "", ""]
        for cells, inner, tint in boxes:
            side = _fg("\u2502", tint, plain)
            middle = side + " " + inner
            middle += " " * max(0, cells - 1 - width(middle)) + side
            for index, part in enumerate((
                _fg("\u256d" + "\u2500" * (cells - 2) + "\u256e", tint, plain), middle,
                _fg("\u2570" + "\u2500" * (cells - 2) + "\u256f", tint, plain),
            )):
                rows[index] += " " + part
        hit = {
            "kind": "board-filter", "key": ("board-filter",),
            "search_at": project_w + 3, "active_at": project_w + search_w + 4,
        }
        return [(row, hit) for row in rows]

    def _board_counts(self, plain):
        shown = self._board_shown()
        projects = {task.get("project") or "" for task in shown if task.get("project")}
        left = " " + _fg(f"{_plural(len(shown), 'task')} \u00b7 {_plural(len(projects), 'project')}", MUTED, plain)
        review = sum(1 for task in shown if task.get("captain_review"))
        if not review:
            return left
        right = _fg("\u25cf", YELLOW, plain) + " " + _fg(f"{review} awaiting reply", MUTED, plain) + " "
        return spread(left, right, self.cols)

    def _board_banner(self, plain):
        parts = []
        if self.board_error:
            parts.append(self.board_error)
        if self.board_stderr:
            parts.append(self.board_stderr)
        parts.extend((self.board_summary or {}).get("lines") or [])
        if self.board_source == "board-data.js" and self.board_generated_at:
            parts.append("board-data.js " + str(self.board_generated_at))
        elif self.board_loaded_at is not None and self.board_error:
            age = age_label(max(0, self.clock() - self.board_loaded_at))
            parts.append("showing last data from " + age)
        if not parts and not self.board_tasks and self.board_loaded_at is None and self.board_job:
            parts.append("Loading board")
        if not parts:
            return ""
        return " " + _fg(clip(" \u00b7 ".join(parts), self.cols - 2), YELLOW if self.board_error else MUTED, plain)

    def _board_facts(self, live):
        """(live agent?, changed-files text) of a card, from the agent rows already read."""
        def facts(task):
            row = live.get(task.get("id"))
            if row is None:
                return False, None
            info = self._ws_info(row)
            status = info.get("status") if info else None
            return True, files_text(status.changed if status else None)
        return facts

    def _board_body(self, plain, budget, live):
        reason = unavailable(self.config.firstmate_home, self.config.firstmate_root, self.env)
        if reason and not self.board_tasks:
            return [(" " + _fg(reason, YELLOW, plain), None)]
        if self.board_loaded_at is None and self.board_job and not self.board_tasks:
            return [(" " + _fg("Loading board", MUTED, plain), {"kind": "status"})]
        grouped, other = self._board_grouped()
        if self.cols < WIDE:
            return self._board_narrow(grouped, other, plain, budget, live)
        extra = self._board_extra(other, plain, max(0, budget - MIN_LANES))
        rows = self._board_wide(grouped, plain, budget - len(extra), live)
        return (rows + extra)[:budget]

    def _board_extra(self, other, plain, room):
        """Opened Done cards (one line each) and tasks in a state the board does not map."""
        entries = []
        if self.board_done_open:
            for task in self._done_cards():
                selected = task.get("id") == self.board_selected
                entries.append((
                    " " + done_row(task, selected, self.cols - 2, plain),
                    {"kind": "board-card", "id": task.get("id"), "state": DONE[0]}, selected,
                ))
        if other:
            entries.append((" " + _fg(f"Other {len(other)}: state kept, not mapped", YELLOW, plain), None, False))
            for task in other:
                label = sanitize(task.get("state") or "unknown")
                selected = task.get("id") == self.board_selected
                text = _fg(f"{task.get('id')} {label}", BRIGHT if selected else INK, plain)
                entries.append((" " + text, {"kind": "board-card", "id": task.get("id"), "state": "other"}, selected))
        if not entries or room <= 0:
            return []
        index = next((i for i, entry in enumerate(entries) if entry[2]), None)
        shown, above, below = window(entries, index, room)
        rows = [(text, hit) for text, hit, _ in shown]
        if above or below:
            rows.append((" " + more_row(above, below, self.cols - 2, plain), None))
        return rows

    def _board_wide(self, grouped, plain, budget, live):
        lanes = self._board_lanes(grouped)
        cells = lane_widths(self.cols, len(lanes))
        facts = self._board_facts(live)
        room = max(1, budget - 2)
        padded = spacious([len(grouped.get(key, [])) for key, _ in lanes], room)
        built = []
        for (key, label), size in zip(lanes, cells):
            cards = grouped.get(key, [])
            selected = self.board_selected if self.board_column == key else None
            rows, start = lane_rows(cards, selected, size, room, plain, self.board_scroll.get(key, 0), facts, padded)
            self.board_scroll[key] = start
            head = [(text, None) for text in lane_header(key, label, len(cards), size, plain)]
            built.append((key, head + rows))
        joined = join_lanes(built, cells, plain)
        return [(text, {"kind": "board-row", "spans": spans}) for text, spans in joined][:budget]

    def _board_stop_strip(self, stops, grouped, other, plain):
        """One line naming every lane the narrow board can show; the current one in full."""
        parts, spans, column = [], [], 2
        for key in stops:
            if key == "other":
                glyph, label, count = "?", "Other", len(other)
            elif key == DONE[0]:
                glyph, label, count = COLUMN_GLYPH[key], DONE[1], len(self._done_cards())
            else:
                glyph, label, count = COLUMN_GLYPH[key], dict(COLUMNS)[key], len(grouped.get(key, []))
            current = key == self.board_column
            text = f"{glyph} {label} {count}" if current else f"{glyph} {count}"
            color = glyph_color(key) if key != "other" else YELLOW
            paint_text = _fg(glyph, color, plain) + " " + (_fg(f"{label} {count}", INK, plain, bold=True) if current else _fg(str(count), MUTED, plain))
            if current:
                paint_text = _fg("[", ACCENT, plain) + paint_text + _fg("]", ACCENT, plain)
                text = f"[{text}]"
            parts.append(paint_text)
            spans.append((column, column + width(text) - 1, key))
            column += width(text) + 2
        return " " + "  ".join(parts), {"kind": "board-stop", "spans": spans}

    def _board_narrow(self, grouped, other, plain, budget, live):
        stops = self._board_stops(grouped) + (["other"] if other else [])
        column = self.board_column if self.board_column in stops else stops[0]
        self.board_column = column
        strip, hit = self._board_stop_strip(stops, grouped, other, plain)
        rows = [(strip, hit), (" " + rule(self.cols - 2, plain), None)]
        room = max(1, budget - 2)
        cells = self.cols - 2
        if column == DONE[0]:
            if not self.board_done_open and not self.board_selected:
                rows.append((" " + _fg("collapsed; Enter or left/right", MUTED, plain), {"kind": "board-done"}))
                return rows[:budget]
            cards = self._done_cards()
            shown, above, below = window(cards, next((i for i, task in enumerate(cards) if task.get("id") == self.board_selected), None), room)
            for task in shown:
                rows.append((" " + done_row(task, task.get("id") == self.board_selected, cells, plain),
                             {"kind": "board-card", "id": task.get("id"), "state": column}))
            if above or below:
                rows.append((" " + more_row(above, below, cells, plain), None))
            if not cards:
                rows.append((" " + _fg("No tasks", MUTED, plain), None))
            return rows[:budget]
        if column == "other":
            for task in other:
                label = sanitize(task.get("state") or "unknown")
                selected = task.get("id") == self.board_selected
                rows.append((" " + _fg(f"{task.get('id')} {label}", BRIGHT if selected else INK, plain),
                             {"kind": "board-card", "id": task.get("id"), "state": "other"}))
            return rows[:budget]
        lane, start = lane_rows(grouped.get(column, []), self.board_selected, cells, room, plain,
                                self.board_scroll.get(column, 0), self._board_facts(live))
        self.board_scroll[column] = start
        for text, task_id in lane:
            rows.append((" " + text, {"kind": "board-card", "id": task_id, "state": column} if task_id else None))
        return rows[:budget]

    def _done_cards(self):
        return [task for task in self._board_shown() if task.get("state") == "done"]

    def _board_done_line(self, plain):
        count = len(self._done_cards())
        mark = "\u25be" if self.board_done_open else "\u25b8"
        focused = self.board_column == DONE[0] and not self.board_selected
        text = _fg("\u258c", ACCENT, plain) if focused else " "
        text += _fg(f"{mark} {DONE[1]} ({count})", BRIGHT if focused else INK, plain, bold=focused)
        text = pad(text, self.cols)
        return (_on(text, SELECTED_BG, plain) if focused else text), {"kind": "board-done", "key": ("board-done",)}

    def _board_reasons(self, card):
        """Labelled lines the detail shows on Enter: only what the projection carries."""
        rows = []
        for label, key in (("Reason", "hold_reason"), ("Now", "running"), ("Note", "note")):
            text = " ".join(sanitize(card.get(key) or "").split())
            if text:
                rows.append((label, text))
        return rows

    def _board_detail_lines(self, plain, tier, reasons, live):
        if not tier:
            return []
        rows = [(" " + rule(self.cols - 2, plain), None)] if tier >= 3 else []
        card = self._board_card()
        if card is None:
            rows.append((" " + _fg("No task selected", MUTED, plain), None))
            return rows
        ident = sanitize(card.get("id") or "")
        title = sanitize(card.get("title") or "")
        agent_row = live.get(card.get("id"))
        go = "Go to agent \u2197" if agent_row else ("Agent not open" if card.get("agent") else "No agent assigned")
        go_paint = _fg(go, ACCENT, plain) if agent_row else _fg(go, MUTED, plain)
        room = max(4, self.cols - width(go) - 6)
        head = " " + _fg(clip(f"{ident} \u00b7 {title}" if title else ident, room), BRIGHT, plain, bold=True)
        rows.append((spread(head, go_paint + " ", self.cols), {"kind": "board-agent", "id": card.get("id"), "go_at": self.cols - width(go)}))
        if tier >= 2:
            project = sanitize(card.get("project") or "") or "no project"
            agent = sanitize(card.get("agent") or "")
            branch = sanitize(self._branch_of(agent_row) or "") if agent_row else ""
            left = " " + _fg(project, PINK, plain)
            if agent:
                left += _fg(" / ", MUTED, plain) + _fg(agent, INK, plain)
            if branch:
                left += _fg(" \u00b7 ", MUTED, plain) + _fg(branch, MUTED, plain)
            state = card.get("state") or "unknown"
            label = dict(COLUMNS).get(state) or (DONE[1] if state == DONE[0] else sanitize(state))
            kind = sanitize(card.get("kind") or "")
            right = _fg(label + (" \u00b7 " + kind if kind else ""), glyph_color(state), plain)
            if not any(task.get("id") == card.get("id") for task in self._board_shown()):
                right += _fg(" \u00b7 hidden by filters", MUTED, plain)
            right += " "
            rows.append((fit(spread(left, right, self.cols), self.cols), None))
        for label, text in reasons:
            rows.append((" " + _fg(label + "  ", MUTED, plain) + _fg(clip(text, self.cols - width(label) - 5), INK, plain), None))
        return rows

    def _board_draft(self):
        card = self._board_card()
        if card is None:
            return None
        target = "board:" + card["id"]
        if self.draft is not None and self.draft.get("target") == target:
            return self.draft
        return self.drafts.find(target)

    def _board_composer_text(self):
        draft = self._board_draft()
        if draft is None:
            return ""
        if draft is self.draft:
            return self.composer.text
        return draft.get("body") or ""

    def _board_composer_lines(self, plain, tier):
        """Fixed message block for the selected task: label, context chip, field with Send, hint."""
        if not tier:
            return []
        card = self._board_card()
        draft = self._board_draft()
        text = self._board_composer_text()
        editing = self._board_editing()
        rows = []
        if tier >= 5:
            rows.append((" " + rule(self.cols - 2, plain), None))
            label = " " + _fg("Write to FirstMate", ACCENT, plain, bold=True)
            right = _fg("Selected task context" if card else "No task selected", MUTED, plain) + " "
            rows.append((spread(label, right, self.cols), {"kind": "board-composer", "part": "label"}))
        if tier >= 2:
            if card:
                ident, title = sanitize(card.get("id")), sanitize(card.get("title"))
                chip = clip(ident + (" \u00b7 " + title if title else ""), self.cols - 8) + (" \u00d7" if editing else "")
                chip_text = " [" + chip + "]" if plain else " " + _on(_fg(" " + chip + " ", INK, plain), FIELD_BG, plain)
                rows.append((chip_text, {"kind": "board-composer", "part": "chip", "dismiss": width(chip) + 1}))
            else:
                rows.append((" " + _fg("Select a task to write to FirstMate", MUTED, plain), {"kind": "board-composer", "part": "label"}))
        placeholder = "Write about the selected task" if card else "Select a task to write to FirstMate"
        button = " Send \u21b5 "
        box = max(8, self.cols - width(button) - 3)
        field = self._field_text(text, placeholder, editing, self.composer.cursor if editing else 0, max(1, box - 2), plain)
        inner = " " + field
        inner += " " * max(0, box - width(inner))
        active = bool(card) and bool(text.strip())
        if plain:
            send = "[" + button.strip() + "]"
        else:
            send = _on(_fg(button, BRIGHT if active else MUTED, plain, bold=active), SEND_BG if active else FIELD_BG, plain)
        rows.append((" " + (inner if plain else _on(inner, FIELD_BG, plain)) + send,
                     {"kind": "board-composer", "part": "field", "send_at": 2 + box}))
        if tier >= 3:
            hint = "Goes into FirstMate's message box with the task context; you press Enter there"
            if draft and draft.get("outcome"):
                hint = f"last send: {draft['outcome']} \u00b7 " + hint
            rows.append((" " + _fg(clip(hint, self.cols - 2), MUTED, plain), {"kind": "board-composer", "part": "hint"}))
        return rows

    def _board_footer(self, plain):
        if self.status:
            return " " + _fg(sanitize(self.status), INK, plain)
        if self.focus == "composer":
            hints = [("Enter", "Preview and send"), ("Esc", "Back")]
        elif self.focus == "search":
            hints = [("Enter", "Keep"), ("Esc", "Clear")]
        else:
            hints = [("\u2190\u2192", "Column"), ("\u2191\u2193", "Task"), ("Enter", "Detail"), ("Tab", "Message"),
                     ("g", "Agent"), ("/", "Search"), ("p", "Project"), ("f", "Filter")]
        out, used = "", 1
        for key, word in hints:
            piece = (2 if out else 0) + width(key) + 1 + width(word)
            if used + piece > self.cols - 1:
                break
            out += ("  " if out else "") + _fg(key, INK, plain) + " " + _fg(word, MUTED, plain)
            used += piece
        return " " + out
