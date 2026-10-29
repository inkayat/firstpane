"""Firstpane panel model. Herdr, git, and gh are reached only through the transport.

Layout: a tab row (Agents, Review, Board, Terminal, Browser) and a header Settings control.
Review shows the selected agent's workspace or a pull request. Messages to FirstMate are written
inline and pasted unsent. The Terminal tab is a new empty shell in the selected agent's directory.
"""

import json
import os
import secrets
import time
from concurrent.futures import Future
from pathlib import Path

from firstpane.agents_view import AgentsMixin, agent_identity, humanize
from firstpane.board_view import BoardMixin
from firstpane.browser import Browser
from firstpane.browser_view import BrowserTabMixin
from firstpane.cells import fit, width, wrap
from firstpane.changes_view import HIDDEN_HEADERS, ChangesMixin
from firstpane.composer_view import ComposerMixin, note_outcome
from firstpane.drafts import DraftStore
from firstpane.frames import FrameMedium
from firstpane.fmmeta import display_labels, load_metas
from firstpane.gitremote import choose_remote, parse_git_remote_url
from firstpane.icons import NO_GRAPHICS, HarnessIcons
from firstpane.input import InputDecoder
from firstpane.lineedit import LineEdit
from firstpane.placed import PlacedBrowser
from firstpane.pr_view import SECTIONS as PR_SECTIONS, PullRequestsMixin, checks_label, file_section, review_label
from firstpane.review import ReviewSupervisor, build_request, mark_findings, patch_files, patch_line_paths, review_scope, selected_skills, skill_catalog, validate_result
from firstpane.sanitize import sanitize
from firstpane.scope import path_in_scope, scope_root
from firstpane.sessionname import session_from_socket_path
from firstpane.settings_view import SettingsMixin, migrate_ui_state
from firstpane.style import INK, MUTED, PINK, YELLOW, frame_lines, no_color, paint, rgb_fg
from firstpane.tabs import SETTINGS_KEY, SOURCE_KEY, TAB_LABELS, TAB_SHORT, TABS
from firstpane.terminal_view import TerminalMixin
from firstpane.workspace import FileEntry, parse_diff, read_diff

SNAPSHOT_TIMEOUT = 3
CELL_QUERY = "\x1b[16t"
PIXEL_MOUSE_QUERY = "\x1b[?1016$p"
PIXEL_MOUSE_MODE = 1016
GIT_TIMEOUT = 3
LIST_TIMEOUT = 10
VIEW_TIMEOUT = 15
DIFF_TIMEOUT = 20
COMMENT_TIMEOUT = 30
NOTE_TIMEOUT = 20
LOGIN_TIMEOUT = 10
PR_CADENCE = 120
SNAPSHOT_CADENCE = 2
LIST_LIMIT = 21
SHOW_LIMIT = 20
DIFF_CAP = 200_000
PROJECT_LIMIT = 200
GIT_RETRY = 30
SNAPSHOT_FAILED = "snapshot failed"
SOCKET_ERRORS = (TimeoutError, OSError, ConnectionError, ValueError)
TERMINAL_OUTCOMES = ("posted", "queued", "already-sent")
PR_FIELDS = ("pr_rows", "truncated", "choice", "fetched_at", "remote_loaded", "pr_reason", "pr_error")
TARGET_FIELDS = (
    "kind", "project_key", "project_label", "label", "harness", "status", "pane_id", "terminal_id",
    "workspace_id", "tab_id", "task_id", "focusable", "recorded", "herdr_checkout", "cwd_hint",
    "text",
)


def _canonical(path):
    resolved = Path(os.path.realpath(path))
    if resolved.name == ".git":
        return str(resolved.parent)
    return str(resolved)


def _has_agent(row):
    return bool(row.get("agent") or row.get("name"))


def _checkout(workspaces, workspace_id):
    """The Herdr-reported checkout path of a workspace, if Herdr detected one."""
    workspace = workspaces.get(workspace_id) or {}
    worktree = workspace.get("worktree") if isinstance(workspace.get("worktree"), dict) else None
    return worktree.get("checkout_path") if worktree else None


def _inline(fn):
    future = Future()
    future.set_result(fn())
    return future


def _pixel(event, index):
    """The pixel position a decoded mouse event carries in SGR-pixel mode, else None."""
    return event[index] if len(event) > index else None


class App(AgentsMixin, BoardMixin, ComposerMixin, ChangesMixin, BrowserTabMixin, PullRequestsMixin, SettingsMixin, TerminalMixin):
    def __init__(self, config, transport, state_dir, env, clock, terminal, rows=24, cols=100, submit=_inline, wall=time.time):
        self.config = config
        self.transport = transport
        self.state_dir = Path(state_dir)
        self.env = env
        self.clock = clock
        self.terminal = terminal
        self.submit = submit
        self.wall = wall
        self.rows = rows
        self.cols = cols
        self.session = session_from_socket_path(config.socket_path)
        self.projects = []
        self.other_agents = []
        self.selection_key = None
        self.folded_projects = set()
        self.folded_prs = set()
        self.pr_selection_key = None
        self.hit_rows = []
        self.last_agent = {}
        self.last_snapshot_at = None
        self.status = ""
        self.screen = "tree"
        self.pending = None
        self.detail_text = ""
        self.logins = {}
        self._common_cache = {}
        self._git_cache = {}
        self._fetches = {}
        self.decoder = InputDecoder()
        self.tab = "agents"
        self.review_source = "workspace"
        self.review_skills = []
        self.review_findings = []
        self.review_supervisor = None
        self.review_origin = None
        self.review_jumps = []
        self.review_menu = None
        self.review_focus = False
        self._finding_goal = None
        self.revision_oid = None
        self.revision_parent = None
        self.settings_open = False
        self._parked_diff = {"workspace": None, "pr": None}
        self._keyboard = "list"
        self._page_button = None
        self.target = None
        self.search = LineEdit(multiline=False)
        self.status_filter = "all"
        self._status_seen = {}
        self.agent_ws = {}
        self._ws_jobs = {}
        self.changes_cursor = 0
        self.changes_pick = None
        self.changes_scope = "all"
        self.changes_menu = None
        self._bar_spans = {}
        self.diff_view = None
        self._diff_job = None
        self._last_press = None
        self.git_job = None
        self.git_action = ""
        self.capture_job = None
        self.capture_for = None
        self.capture_pick = None
        self.frames = FrameMedium(clock)
        self.icons = HarnessIcons(clock, enabled=not no_color(env))
        self.pixel_mouse = None
        self.cell_known = False
        self._body_budget = rows
        self._tab_spans = []
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.drafts = DraftStore(self.state_dir / "drafts")
        self.composer = LineEdit()
        self.draft = None
        self.draft_conflict = False
        self._init_board()
        self._init_settings()
        pane_slug = "".join(char if char.isalnum() else "-" for char in (config.herdr_pane_id or "pane"))
        browser_dir = self.state_dir / "browser" / pane_slug
        self.browser = Browser(getattr(config, "terminal_browser_root", None), browser_dir, env)
        self.placed = PlacedBrowser(getattr(config, "terminal_browser_root", None), browser_dir, transport, env, clock=wall)
        self._init_prs()
        self._init_terminal()
        self._load_ui()

    def refresh(self, manual=False):
        self.poll()
        self._settle_frames()
        self._settle_icons()
        self.browser.refresh()
        self.placed.refresh()
        now = self.clock()
        due = (
            manual
            or self.last_snapshot_at is None
            or now - self.last_snapshot_at >= SNAPSHOT_CADENCE
        )
        if due:
            self._load_snapshot()
            self.last_snapshot_at = now
        self._refresh_prs(force=manual)
        self._schedule_workspaces(force=manual)
        if manual and self.tab == "board" and self.board_view == "detail":
            self.board_detail_force = True
        self._refresh_board(force=manual and self.tab == "board")
        self._sync_draft()
        self._save_ui()

    # ---- terminal queries and replies ------------------------------------------------
    def terminal_queries(self):
        """What Firstpane asks its pane's terminal at start: the cell size, whether it reports
        mouse positions in pixels, and which kitty media it accepts for browser frames."""
        return CELL_QUERY + PIXEL_MOUSE_QUERY + self.frames.queries()

    def _settle_frames(self):
        if self.browser.medium is None and self.frames.poll() is not None:
            self.status = self.browser.set_medium(self.frames.result) or self.status

    def _settle_icons(self):
        """Say once that harness icons cannot show here (rows then name their harness)."""
        if self.icons.poll() and not self.status:
            self.status = NO_GRAPHICS

    def cell_size(self, width, height):
        """The pane's cell size in pixels: from the terminal's answer, or from the pixel size
        the pane reports with its rows and columns (which follows client and font changes)."""
        if width <= 0 or height <= 0:
            return
        self.cell_known = True
        self.browser.set_cell(width, height)
        self._sync_pixel_mouse()

    def _sync_pixel_mouse(self):
        """Pixel mouse reports once the terminal supports them and the cell size is known."""
        cell = self.browser.cell if self.cell_known else None
        if self.pixel_mouse and cell is not None and self.decoder.pixel_cell != cell:
            self.decoder.pixel_cell = cell
            self.terminal.pixel_mouse(True)

    @property
    def focus(self):
        return self._keyboard

    @focus.setter
    def focus(self, value):
        """Keyboard focus. `browser` hands keys (with kitty keyboard reporting, so modifiers
        survive) to the page; leaving it takes them back."""
        page = value == "browser"
        if page != (self._keyboard == "browser"):
            self.browser.set_focus(page)
            self.terminal.page_keyboard(page)
        self._keyboard = value

    # ---- input -----------------------------------------------------------------------
    def handle_bytes(self, data):
        if getattr(self, "terminal_keyboard", False) and self.tab == "terminal":
            self._terminal_raw(data)
            return
        for event in self.decoder.feed(data):
            kind = event[0]
            if kind == "key":
                self.handle_key(event[1])
            elif kind == "paste":
                self.handle_paste(event[1])
            elif kind == "mouse":
                self.handle_mouse(event[1], event[2], event[3], pressed=event[4], pixel=_pixel(event, 5))
            elif kind == "shift-click":
                self.handle_mouse(0, event[1], event[2], gesture="shift", pixel=_pixel(event, 3))
            elif kind == "drag":
                self.handle_mouse(0, event[1], event[2], gesture="drag", pixel=_pixel(event, 3))
            elif kind == "hover":
                self.handle_hover(event[1], event[2], pixel=_pixel(event, 3))
            elif kind == "wheel":
                self.handle_wheel(event[1], event[2], event[3], pixel=_pixel(event, 4))
            elif kind == "cell":
                self.cell_size(event[1], event[2])
            elif kind == "mode" and event[1] == PIXEL_MOUSE_MODE:
                self.pixel_mouse = event[2] in (1, 2, 3)
                self._sync_pixel_mouse()
            elif kind == "graphics":
                self.frames.answer(event[1], event[2])
                self.icons.answer(event[1], event[2])
                self._settle_frames()

    def _hit(self, column, row):
        if column < 1 or column > self.cols or row < 1 or row > len(self.hit_rows):
            return None
        return self.hit_rows[row - 1]

    def handle_mouse(self, button, column, row, pressed=True, gesture="press", pixel=None):
        """A left press, Shift-click (`shift`) or a move with the button held (`drag`)."""
        hit = self._hit(column, row)
        if hit and hit.get("kind") == "browser" and self.screen == "tree":
            if gesture == "drag":
                self._browser_mouse("move", "left", column, row, pixel)
            elif gesture == "shift":
                shift = {"shift": True, "alt": False, "ctrl": False, "super": False}
                for kind in ("down", "up"):
                    self._browser_mouse(kind, "left", column, row, pixel, shift)
            else:
                self._browser_mouse("down" if pressed else "up", ("left", "middle", "right", "none")[button & 3],
                                    column, row, pixel)
            return
        if self._page_button is not None and self.tab == "browser" and self.screen == "tree" \
                and (gesture == "drag" or not pressed):
            self._browser_mouse("move" if gesture == "drag" else "up", self._page_button, column, row, pixel)
            return
        if not pressed:
            self._page_button = None
            return
        if gesture != "drag":
            self.status = ""
        if button != 0 or not hit:
            return
        kind = hit.get("kind")
        if kind == "tabs":
            if gesture == "press":
                for start, end, name in self._tab_spans:
                    if start <= column - 1 < end:
                        if name == "settings":
                            self._open_settings()
                        else:
                            self._set_tab(name)
            return
        if self.screen != "tree":
            return
        if kind == "terminal":
            if gesture == "press":
                self._terminal_take_keyboard()
            return
        if kind in ("review-source", "review-menu"):
            if gesture == "press":
                self._review_frame_mouse(hit, column)
            return
        if kind in ("changes-bar", "changes-menu", "diffline", "diff-scroll"):
            self._changes_mouse(hit, column, gesture)
            return
        if gesture != "press":
            return
        self.changes_menu = None
        if self.settings_open and kind in ("setting-choice", "setting-tab"):
            self._settings_mouse(hit)
            return
        if self.tab == "board" and kind in ("board-filter", "board-done", "board-agent", "board-row", "board-card", "board-stop", "board-composer", "board-back", "board-review"):
            if gesture == "press":
                self._board_mouse(hit, column)
                self._save_ui()
            return
        if kind == "context-footer":
            if gesture == "press":
                self._context_footer_click(hit, column)
            return
        if kind in ("searchbar", "composer"):
            self._agents_bar_click(kind, hit, column)
            return
        if kind == "draft":
            self._open_target(hit["target"])
            return
        if kind and kind.startswith("pr-") and kind != "pr-project":
            self._pr_mouse(hit, column)
            self._save_ui()
            return
        if kind not in ("project", "pr-project", "agent", "pr", "more", "other"):
            return
        matches = [item for item in self._rows() if item["key"] == hit["key"]]
        if len(matches) != 1:
            self.status = "click ignored: stale target"
            return
        self._close_inline()
        self.focus = "list"
        self._set_cursor(matches[0]["key"])
        self._activate(matches[0])
        self._save_ui()

    def handle_hover(self, column, row, pixel=None):
        """The pointer moving with no button held reaches the page under it (hover effects, the
        select tool's target); it never takes the keyboard."""
        hit = self._hit(column, row)
        if hit and hit.get("kind") == "browser" and self.screen == "tree":
            self._browser_mouse("move", "none", column, row, pixel)

    def handle_wheel(self, direction, column, row, pixel=None):
        hit = self._hit(column, row)
        if hit and hit.get("kind") == "browser" and self.screen == "tree":
            self._browser_mouse("scroll" + direction, "none", column, row, pixel)
            return
        if self.focus == "list":
            self.handle_key("k" if direction == "up" else "j")

    def handle_paste(self, text):
        if self.screen.startswith("confirm-"):
            return
        if self.focus == "composer":
            self._composer_paste(text)
        elif self.focus == "search":
            self._search_field().insert(text, paste=True)
        elif self.focus == "browser":
            self.browser.paste(text)
        else:
            self.status = "paste ignored outside a message"

    def handle_key(self, key):
        if key == "ctrl-c":
            raise KeyboardInterrupt  # Ctrl-C reported as a key while the page has the keyboard
        self.status = ""
        self.poll()
        if self.screen.startswith("confirm-"):
            self._confirm_key(key)
            self._save_ui()
            return
        if self.focus == "composer":
            self._composer_key(key)
            return
        if self.focus == "search":
            self._search_key(key)
            return
        if self.focus == "browser":
            self._browser_key(key)
            return
        self._list_key(key)
        self._save_ui()

    def _confirm_key(self, key):
        if key == "y":
            action = {
                "confirm-comment": self._send_comment,
                "confirm-note": self._send_note,
                "confirm-open": self._open_browser,
                "confirm-send": self._send_message,
                "confirm-git": self._run_git,
                "confirm-resolve": self._resolve_thread,
                "confirm-terminal": self._close_terminal,
                "confirm-review": self._start_review,
            }.get(self.screen)
            if action:
                action()
            return
        if key in ("j", "down", "k", "up"):
            self._scroll(1 if key in ("j", "down") else -1)
            return
        self.screen = "tree"
        self.pending = None

    def _scroll(self, step):
        self.pending = self.pending or {}
        self.pending["scroll"] = max(0, self.pending.get("scroll", 0) + step)

    def _on(self, name):
        """Whether `name` is the surface taking keys. Review routes to the workspace or PR source."""
        if self.settings_open:
            return name == "settings"
        if name == "changes":
            return self.tab == "review" and self.review_source == "workspace"
        if name == "prs":
            return self.tab == "review" and self.review_source == "pr"
        return self.tab == name

    def _review_focused(self):
        """Review is showing the diff without the agent header or the message composer."""
        return bool(getattr(self, "review_focus", False)) and self.screen == "tree" and not self.settings_open and (self._on("changes") or self._on("prs"))

    def _set_review_source(self, source, record=True):
        """Switch Workspace / Pull Request without clearing the other source's position."""
        if source not in ("workspace", "pr") or source == self.review_source:
            return
        if record:
            self._record_jump()
        self._parked_diff[self.review_source] = self.diff_view
        self.review_source = source
        self.diff_view = self._parked_diff.get(source)
        self.changes_menu = None
        self.pr_menu = None
        self.review_menu = None
        self._refresh_prs(force=False)

    def _review_revision(self):
        """What the shown diff is pinned to: a commit, the uncommitted state, or a PR head."""
        if self.review_source == "pr":
            return self.pr_commit_oid or "head"
        return self.revision_oid or "uncommitted"

    def _review_origin(self):
        """Where a review result belongs; a result is shown only while this still holds."""
        pr = self._current_pr() if self.review_source == "pr" else None
        return (
            tuple(self.target["key"]) if self.target else None,
            self.review_source,
            self._review_revision(),
            tuple(pr["key"]) if pr else None,
        )

    def _open_settings(self):
        self.settings_open = True
        self._settings_shown()

    def _review_view(self):
        """The shown diff when it is the one the current source, revision and PR pin; else {}."""
        view = self.diff_view or {}
        if not view.get("patch") or view.get("revision") != self._review_revision():
            return {}
        if self.review_source == "pr":
            pr = self._current_pr()
            if pr is None or view.get("pr") != tuple(pr["key"]):
                return {}
        return view

    def _arm_review(self):
        if getattr(self.config, "review", None) is None:
            self.status = "no review command configured"
            return
        supervisor = self.review_supervisor
        if supervisor is not None and supervisor.active():
            self.status = "a review is already running"
            return
        view = self._review_view()
        if not view:
            self.status = "nothing to review"
            return
        catalog = skill_catalog(self.config.review.skills, view.get("workspace"))
        if not selected_skills(self.review_skills, catalog):
            self.status = "select a skill first"
            return
        names = ", ".join(Path(path).name for path in selected_skills(self.review_skills, catalog))
        self.pending = {"kind": "review"}
        self.screen = "confirm-review"
        self.detail_text = "\n".join([
            "Run review?",
            " ".join(self.config.review.command),
            f"Scope: {self._review_scope_text()}",
            f"Skills: {names}",
            f"Times out after {self.config.review.timeout_seconds} s",
            "y starts it, any other key cancels",
        ])

    def _uncommitted_patch(self, view):
        """Every changed file of the chosen uncommitted scope as one patch, not just the open file."""
        texts = []
        for entry in self._change_entries():
            text, error = read_diff(self.transport.run, view["workspace"], entry, self.env)
            if error:
                raise ValueError(f"cannot read {entry.path}: {error}")
            texts.append(text if text.endswith("\n") or not text else text + "\n")
        return "".join(texts)

    def _start_review(self):
        self.screen = "tree"
        view = self._review_view()
        review = getattr(self.config, "review", None)
        if review is None or not view:
            self.status = "nothing to review"
            self.pending = None
            return
        pr = self._current_pr() if self.review_source == "pr" else None
        try:
            scope, revision = review_scope(
                source=self.review_source,
                mode=self.changes_scope,
                head=view.get("head"),
                commit=self.revision_oid,
                parent=self.revision_parent,
                pr=pr,
                pr_commit=self.pr_commit_oid,
            )
            catalog = skill_catalog(review.skills, view.get("workspace"))
            patch = self._uncommitted_patch(view) if scope == "uncommitted" else view["patch"]
            request = build_request(
                run_id=secrets.token_hex(16),
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.wall())),
                skills=[{"name": Path(path).name, "path": path, "skill_md_sha256": next(row["sha"] for row in catalog if row["path"] == path)} for path in selected_skills(self.review_skills, catalog)],
                repository={"path": None if self.review_source == "pr" else view.get("workspace"), "remote": None},
                scope=scope,
                revision=revision,
                files=patch_files(patch),
                patch_text=patch,
            )
        except ValueError as exc:
            self.status = str(exc)
            self.pending = None
            return
        popen = getattr(self, "review_popen", None)
        if popen is None:
            import subprocess
            popen = subprocess.Popen
        self.review_supervisor = ReviewSupervisor(self.state_dir, popen, clock=self.clock)
        env = {key: value for key, value in self.env.items() if not str(key).startswith("HERDR_")}
        cwd = view.get("workspace") if self.review_source == "workspace" else None
        self.review_supervisor.start(review.command, request, cwd=cwd, env=env, timeout=review.timeout_seconds)
        self.review_scope_name = request["scope"]
        self.review_head = (request.get("revision") or {}).get("head") or (request.get("revision") or {}).get("commit")
        self.review_origin = self._review_origin()
        self.review_findings = []
        self.review_blobs = {row["path"]: row.get("new_blob") for row in request["files"]}
        self.status = "review running"
        self.pending = None

    def _set_tab(self, name):
        if name == "settings":
            self._open_settings()
            return
        self.settings_open = False
        if self.tab == "terminal" and name != "terminal":
            self._terminal_release()
        if name != self.tab:
            self._close_inline()
            self.changes_menu = None
            self.review_menu = None
            self.tab = name
            if self.focus in ("search", "browser"):
                self.focus = "list"
            self._refresh_prs(force=False)
            if name == "board":
                self._refresh_board(force=self.board_loaded_at is None)
            if name == "terminal":
                self.terminal_keyboard = False
        self._show_browser(name == "browser")
        if name == "browser":
            self._browser_activate()

    def _show_browser(self, shown):
        """The page is drawn in the tab, and the pointer's plain motion is asked for, only while
        the Browser tab is shown."""
        self.browser.set_visible(shown)
        self.terminal.hover_mouse(shown)

    def _search_field(self):
        return self.board_search if self.tab == "board" else self.search

    def _search_key(self, key):
        field = self._search_field()
        if key == "esc":
            field.set("")
            self.focus = "list"
        elif key in ("enter", "tab"):
            self.focus = "list"
        elif not field.key(key) and len(key) == 1 and key.isprintable():
            field.insert(key)

    def _list_key(self, key):
        if self.screen in ("detail", "diff"):
            if key == "esc":
                self.screen = "tree"
                self.pending = None
            elif key in ("j", "down", "k", "up"):
                self._scroll(1 if key in ("j", "down") else -1)
            return
        if self.tab == "review" and not self.settings_open and self._review_frame_key(key):
            return
        if key == "esc":
            if self.settings_open:
                self.settings_open = False
            elif self._on("changes"):
                self._changes_key("esc")
            elif self._on("prs"):
                self._pr_key("esc")
            elif self.tab == "agents":
                self._clear_agent_filters()
            elif self.tab == "board":
                if self.board_view == "detail":
                    if not self._return_agents_board():
                        self.board_view = "lanes"
                else:
                    self.board_search.set("")
            return
        if key == SETTINGS_KEY:
            if self.settings_open:
                self.settings_open = False
            else:
                self._open_settings()
            return
        if key == SOURCE_KEY and self.tab == "review" and not self.settings_open:
            self._set_review_source("pr" if self.review_source == "workspace" else "workspace")
            return
        if key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            name = self._tab_for_number(key)
            if name:
                self._set_tab(name)
            return
        if key == "r":
            self.refresh(manual=True)
            return
        if self.settings_open:
            self._settings_key(key)
            return
        if self._on("prs") and self._pr_key(key):
            return
        if self._on("changes"):
            if key == "a":
                self._announce()
            else:
                self._changes_key(key)
            return
        if self.tab == "browser":
            self._browser_list_key(key)
            return
        if self.tab == "terminal":
            self._terminal_key(key)
            return
        if self.tab == "board":
            self._board_key(key)
            return
        rows = self._rows()
        index = self._selected_index(rows)
        if key == "a":
            self._announce()
        elif key == "/" and self.tab == "agents":
            self.focus = "search"
        elif key == "f" and self.tab == "agents":
            self._cycle_filter()
        elif key in ("tab", "m") and self.tab == "agents":
            self._open_agent_composer(rows[index] if rows else None)
        elif key in ("j", "down", "k", "up") and rows:
            index = min(index + 1, len(rows) - 1) if key in ("j", "down") else max(index - 1, 0)
            self._set_cursor(rows[index]["key"])
            self._remember(rows[index])
            if rows[index]["kind"] == "agent":
                self._select_target(rows[index], force=False)
        elif key == "enter" and rows:
            row = rows[index]
            if self.tab == "agents" and row.get("kind") == "agent" and row.get("status") == "blocked":
                self._select_target(row)
                self._open_linked_board(row)
            else:
                self._activate(row)
        elif key == "g" and rows:
            if rows[index]["kind"] == "agent":
                self._focus(rows[index])
        elif key == "d" and rows:
            self._open_diff(rows[index])
        elif key == "o" and rows:
            self._arm_browser(rows[index])
        elif key == "c" and rows:
            self._arm_comment(rows[index])
        elif key == "m" and rows and self._on("prs"):
            self._arm_note(rows[index])

    # ---- layout ----------------------------------------------------------------------
    def render(self):
        self.poll()
        plain = no_color(self.env)
        if not self.settings_open and self.tab == "agents" and self.screen == "tree":
            return self._agents_frame(plain)
        if not self.settings_open and self.tab == "board" and self.screen == "tree":
            return self._board_frame(plain)
        rows = self.rows
        mode = "full" if rows >= 14 else ("medium" if rows >= 8 else "small")
        separator = (paint("\u2500" * self.cols, rgb_fg(MUTED), plain), None)
        top = [(self._tab_row(plain), {"kind": "tabs", "key": ("tabs",)})]
        # Focus mode keeps the tab row and spends the agent header's rows on the diff.
        if not self._review_focused():
            if mode == "full" and self._on("prs"):
                top.append((self._header_lines(plain, False)[0], None))
                if self._current_pr() is None:
                    top.append(separator)
            elif mode == "full" and not self.settings_open:
                top += [(line, None) for line in self._header_lines(plain, True)] + [separator]
            elif mode == "medium" and not self.settings_open:
                top.append((self._header_lines(plain, False)[0], None))
        while len(top) >= rows and len(top) > 1:
            top.pop()
        footer = self._context_footer(plain) if self.screen == "tree" else []
        while footer and len(top) + len(footer) >= rows:
            footer = footer[1:]
        budget = max(1, rows - len(top) - len(footer))
        status_line = bool(self.status) and not (self.tab == "browser" and self.screen == "tree")
        body, hits = self._body(plain, max(1, budget - (1 if status_line else 0)))
        body, hits = body[:budget], hits[:budget]
        if status_line and len(body) < budget:
            body = body + [""] * (budget - 1 - len(body)) + [self.status]
            hits = hits + [None] * (budget - len(hits))
        body += [""] * (budget - len(body))
        hits += [None] * (budget - len(hits))
        lines = [text for text, _ in top] + body + [text for text, _ in footer]
        self.hit_rows = ([item for _, item in top] + hits + [hit for _, hit in footer])[:rows]
        return frame_lines([fit(line, self.cols) for line in lines[:rows]], self.cols, plain)

    def _tab_row(self, plain):
        visible = self._visible_tabs()
        narrow = self.cols < 60 or sum(len(TAB_LABELS[name]) + 2 for name in visible) + 4 > self.cols
        parts = []
        spans = []
        column = 1
        for name in visible:
            label = TAB_LABELS[name] if name == self.tab or not narrow else TAB_SHORT[name]
            badge = ""
            urgent = False
            if name == "agents":
                badge, urgent = self._agents_badge(label == TAB_SHORT[name])
            shown = "1;4;" + rgb_fg(INK) if name == self.tab else rgb_fg(MUTED)
            painted = paint(label, shown, plain)
            if badge:
                badge_sgr = rgb_fg(YELLOW) if urgent else rgb_fg(MUTED)
                painted += paint(badge, badge_sgr, plain)
            parts.append(painted)
            shown_width = width(label + badge)
            spans.append((column, column + shown_width, name))
            column += shown_width + 2
        left = " " + "  ".join(parts)
        gear = "Set" if narrow else "Settings"
        gap = max(1, self.cols - width(left) - len(gear))
        start = width(left) + gap
        spans.append((start, start + len(gear), "settings"))
        self._tab_spans = spans
        return left + (" " * gap) + gear

    def _body(self, plain, budget):
        if self.screen.startswith("confirm-") or self.screen in ("detail", "diff"):
            text = {
                "confirm-comment": self._confirm_comment,
                "confirm-note": self._confirm_note,
                "confirm-open": self._confirm_open,
                "confirm-send": self._confirm_send,
                "confirm-git": self._confirm_git,
                "confirm-resolve": self._confirm_resolve,
                "confirm-terminal": lambda plain: self.detail_text,
                "confirm-review": lambda plain: self.detail_text,
            }.get(self.screen, lambda plain: self.detail_text)(plain)
            lines = [part for line in (text.splitlines() or [""]) for part in wrap(line, self.cols)]
            scroll = self.pending.get("scroll", 0) if isinstance(self.pending, dict) else 0
            lines = lines[scroll : scroll + budget]
            return lines, [None] * len(lines)
        if self.settings_open:
            return self._settings_body(plain, budget)
        if self._on("changes") or self._on("prs"):
            head = [self._review_source_row(plain)]
            if self.review_menu is not None and self.focus == "list":
                head += self._review_menu_rows(plain, max(1, budget - 6))
            show = self._changes_body if self._on("changes") else self._prs_body
            lines, hits = show(plain, max(1, budget - len(head)))
            return [text for text, _ in head] + list(lines), [hit for _, hit in head] + list(hits)
        if self.tab == "browser":
            return self._browser_body(plain, budget)
        if self.tab == "terminal":
            return self._terminal_body(plain, budget)
        return [""], [None]

    def _tree_lines(self, rows, budget, lines, hits, plain):
        self._body_budget = max(1, budget)
        index = self._selected_index(rows)
        window, selected_at = self._window(rows, index)
        lines.extend(self._paint_row(row, offset == selected_at, plain) for offset, row in enumerate(window))
        hits.extend(window)
        return lines, hits

    def _tree_budget(self):
        return self._body_budget

    def _header_index(self, rows, index):
        project_key = rows[index].get("project_key")
        for candidate in range(index, -1, -1):
            row = rows[candidate]
            if row["kind"] == "pr-project" and row.get("project_key") == project_key:
                return candidate
        return index

    def _window(self, rows, index):
        budget = self._tree_budget()
        if len(rows) <= budget:
            return rows, index
        header = self._header_index(rows, index)
        if index == header or budget < 2:
            start = min(max(0, index - budget + 1), max(0, len(rows) - budget))
            window = rows[start : start + budget]
            return window, index - start
        after = rows[header + 1 :]
        selected = index - header - 1
        rest = budget - 1
        start = min(max(0, selected - rest + 1), max(0, len(after) - rest))
        chunk = after[start : start + rest]
        return [rows[header]] + chunk, 1 + selected - start

    def _paint_row(self, row, selected, plain):
        marker = ">" if selected else " "
        if row["kind"] == "pr-project":
            return marker + paint(row["text"], "1;" + rgb_fg(PINK), plain)
        return marker + "  " + row["text"]

    def _fit(self, text):
        return fit(text, self.cols)

    def _in_scope(self, path):
        return path_in_scope(path, scope_root(self.config), self.config.project_allowlist)

    def _fm_in_scope(self, meta):
        """A FirstMate-bound report is in scope by its project. With the default scope (no
        project_root or allowlist), a project that is a task-private source clone under this
        FirstMate home's data/ also counts, but only when the report's own worktree is a
        checkout of that clone; such a clone is listed on Agents only (`agents_only`), never in
        Pull Requests. Callers check first that the binding is live and unique."""
        if self._in_scope(meta["project"]):
            return True
        if self.config.project_root or self.config.project_allowlist or not self.config.firstmate_home:
            return False
        if not path_in_scope(meta["project"], str(Path(self.config.firstmate_home) / "data")):
            return False
        common = self._common_dir(meta.get("worktree"))
        return bool(common) and _canonical(common) == _canonical(meta["project"])

    def _pr_projects(self):
        """The Pull Requests inventory: every project except a private clone shown on Agents only."""
        return [project for project in self.projects if not project.get("agents_only")]

    def _generic_in_scope(self, agent, pane, workspaces):
        workspace = workspaces.get(agent.get("workspace_id")) or {}
        worktree = workspace.get("worktree") if isinstance(workspace.get("worktree"), dict) else None
        if worktree:
            return self._in_scope(worktree.get("repo_root") or worktree.get("checkout_path"))
        cwd = agent.get("foreground_cwd") or pane.get("foreground_cwd") or agent.get("cwd") or pane.get("cwd")
        return self._in_scope(cwd)

    def _rows(self):
        return self._pr_rows() if self._on("prs") else self._agent_tree_rows()

    def _agent_tree_rows(self):
        rows = []
        for project in self.projects:
            agents = [row for row in project["agents"] if self._agent_matches(project, row)]
            if not agents:
                continue
            rows.append(self._project_row(project, len(agents)))
            if project["key"] not in self.folded_projects:
                rows.extend(agents)
        others = [row for row in self.other_agents if self._agent_matches({"label": "[other]"}, row)]
        if others:
            rows.append(
                {
                    "kind": "other",
                    "key": ("other",),
                    "project_key": "",
                    "label": "[other]",
                }
            )
            rows.extend(others)
        return rows

    def _pr_rows(self):
        rows = []
        for project in self._pr_projects():
            state = self._pr_state(project)
            if state not in ("prs", "error"):
                continue
            rows.append(self._pr_project_row(project, state))
            if state == "prs" and project["key"] not in self.folded_prs:
                rows.extend(project["pr_rows"])
                if project.get("truncated"):
                    rows.append(
                        {
                            "kind": "more",
                            "key": ("more", project["key"]),
                            "project_key": project["key"],
                            "text": "+more",
                        }
                    )
        return rows

    def _pr_state(self, project):
        """loading (never answered), error, skipped (no queryable GitHub remote), empty, or prs."""
        if project.get("fetched_at") is None:
            return "loading"
        if project.get("pr_error"):
            return "error"
        choice = project.get("choice")
        if choice is not None and not choice.slug:
            return "skipped"
        return "prs" if project["pr_rows"] else "empty"

    def _pr_summary(self):
        """What the list cannot show: projects still loading, failing, or never queried."""
        states = [self._pr_state(project) for project in self._pr_projects()]
        loading, failed, skipped = states.count("loading"), states.count("error"), states.count("skipped")
        parts = []
        if loading:
            parts.append(f"loading \u00b7 {len(states) - loading} of {len(states)} projects checked")
        elif not states:
            parts.append("no projects")
        elif not failed and "prs" not in states:
            parts.append("no open pull requests")
        if failed:
            parts.append(f"{failed} failed")
        if skipped:
            parts.append(f"{skipped} not queried (no allowed GitHub remote)")
        return " \u00b7 ".join(parts)

    def _mark(self, open_):
        return "\u25be" if open_ else "\u25b8"

    def _project_row(self, project, count):
        return {
            "kind": "project",
            "key": ("project", project["key"]),
            "project_key": project["key"],
            "label": project["label"],
            "agent_count": count,
        }

    def _pr_project_row(self, project, state):
        if state == "error":
            text = "! " + project["label"] + "  " + project["pr_error"]
        else:
            text = self._mark(project["key"] not in self.folded_prs) + " " + project["label"]
            choice = project.get("choice")
            if choice and choice.notice:
                text += "  " + choice.notice
        return {
            "kind": "pr-project",
            "key": ("pr-project", project["key"]),
            "project_key": project["key"],
            "text": text,
            "state": state,
        }

    def _cursor(self):
        return self.pr_selection_key if self._on("prs") else self.selection_key

    def _set_cursor(self, key):
        if self._on("prs"):
            self.pr_selection_key = key
        else:
            self.selection_key = key

    def _selected_index(self, rows):
        if not rows:
            return 0
        cursor = self._cursor()
        for index, row in enumerate(rows):
            if row["key"] == cursor:
                return index
        if cursor and len(cursor) > 1:
            project_key = self._group_of(cursor)
            for index, row in enumerate(rows):
                if row.get("project_key") == project_key:
                    return index
        return 0

    def _group_of(self, key):
        """The project an agent row is listed under (a clone's agents sit under the project it was
        made from); the key's own project when no listed agent has that key."""
        for project in self.projects:
            if any(row["key"] == key for row in project["agents"]):
                return project["key"]
        return key[1]

    def _remember(self, row):
        if row["kind"] == "agent" and row.get("project_key"):
            self.last_agent[self._group_of(row["key"])] = row["key"]

    def _activate(self, row):
        if row["kind"] == "project":
            key = row["project_key"]
            if key in self.folded_projects:
                self.folded_projects.remove(key)
                restored = self.last_agent.get(key)
                if restored:
                    self.selection_key = restored
            else:
                self.folded_projects.add(key)
            return
        if row["kind"] == "pr-project":
            key = row["project_key"]
            if row["state"] == "error":
                self._fetch_prs(self._project(key), force=True)
            elif key in self.folded_prs:
                self.folded_prs.remove(key)
            else:
                self.folded_prs.add(key)
            return
        if row["kind"] == "pr":
            self._choose_pr(row)
            return
        if row["kind"] == "agent":
            self._select_target(row)

    def _select_target(self, row, force=True):
        """Moving onto an agent, Enter or a click selects it in this panel; `g` focuses its pane.
        A move (`force` False) re-reads no workspace that is not due, so a held arrow key starts no git burst."""
        if self.target and tuple(self.target["key"]) == tuple(row["key"]):
            return
        self._close_inline()
        self.target = dict(row)
        self.pr_choice = None
        self.diff_view = None
        self._parked_diff = {"workspace": None, "pr": None}
        self.revision_oid = None
        self.revision_parent = None
        self.pr_commit_oid = None
        self.pr_diff_open = False
        self.review_findings = []
        self.review_jumps = []
        self.changes_cursor = 0
        self.changes_pick = None
        self.changes_menu = None
        self._schedule_workspaces(force=force)

    def _verify_live(self, row):
        """Re-read the pane and its FirstMate claim: None, or closed / moved / unproven."""
        if not row.get("focusable"):
            return "unproven"
        try:
            response = self.transport.socket_call(
                "pane.get", {"pane_id": row["pane_id"]}, SNAPSHOT_TIMEOUT
            )
        except SOCKET_ERRORS:
            return "closed"
        result = response.get("result") if isinstance(response, dict) else None
        pane = result.get("pane") if isinstance(result, dict) else None
        if not isinstance(pane, dict):
            return "closed"
        same = (
            pane.get("pane_id") == row["pane_id"]
            and pane.get("terminal_id") == row["terminal_id"]
            and pane.get("workspace_id") == row["workspace_id"]
            and pane.get("tab_id") == row["tab_id"]
        )
        if not same:
            return "moved"
        if row.get("task_id"):
            meta = self._meta(row["task_id"])
            if (
                not meta
                or meta["herdr_pane_id"] != pane["pane_id"]
                or meta["herdr_workspace_id"] != pane["workspace_id"]
                or meta["herdr_tab_id"] != pane["tab_id"]
                or meta["herdr_session"] != self.session
                or meta.get("worktree") != row.get("recorded")
            ):
                return "unproven"
        return None

    def _focus(self, row):
        reason = self._verify_live(row)
        if reason:
            self.status = "not focused: " + reason
            self.refresh(manual=True)
            return
        try:
            focused = self.transport.socket_call(
                "pane.focus", {"pane_id": row["pane_id"]}, SNAPSHOT_TIMEOUT
            )
        except SOCKET_ERRORS:
            focused = None
        if not isinstance(focused, dict) or focused.get("error"):
            self.status = "not focused: closed"
            self.refresh(manual=True)

    def _load_snapshot(self):
        try:
            response = self.transport.socket_call("session.snapshot", {}, SNAPSHOT_TIMEOUT)
        except SOCKET_ERRORS as exc:
            self.status = SNAPSHOT_FAILED + ": " + sanitize(str(exc))
            return
        result = (response or {}).get("result") if isinstance(response, dict) else None
        snapshot = None
        if isinstance(result, dict) and isinstance(result.get("snapshot"), dict):
            snapshot = result["snapshot"]
        elif isinstance(response, dict) and isinstance(response.get("snapshot"), dict):
            snapshot = response["snapshot"]
        elif isinstance(response, dict) and "panes" in response and "workspaces" in response:
            snapshot = response
        if not isinstance(snapshot, dict):
            self.status = SNAPSHOT_FAILED
            return
        if self.status.startswith(SNAPSHOT_FAILED):
            self.status = ""
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot):
        panes = {
            row.get("pane_id"): row
            for row in snapshot.get("panes") or []
            if isinstance(row, dict) and row.get("pane_id")
        }
        agents = [
            row
            for row in snapshot.get("agents") or []
            if isinstance(row, dict) and row.get("pane_id")
        ]
        workspaces = {
            row.get("workspace_id"): row
            for row in snapshot.get("workspaces") or []
            if isinstance(row, dict)
        }
        own = self.config.herdr_pane_id
        metas = load_metas(self.config.firstmate_home)
        claims = {}
        for meta in metas:
            claims[meta["herdr_pane_id"]] = claims.get(meta["herdr_pane_id"], 0) + 1
        live = [
            meta for meta in metas
            if self._binding_is_live(meta, panes, agents, claims, own)
            and self._fm_in_scope(meta)
        ]
        bound = {meta["herdr_pane_id"] for meta in live}
        groups = {}
        private = set()
        for meta in live:
            pane = panes.get(meta["herdr_pane_id"])
            agent = next((row for row in agents if row.get("pane_id") == meta["herdr_pane_id"]), None)
            key = _canonical(meta["project"])
            if not self._in_scope(meta["project"]):
                private.add(key)
            groups.setdefault(key, {"git_path": key, "agents": []})
            row = self._fm_agent(meta, key, pane, agent)
            row["herdr_checkout"] = _checkout(workspaces, pane.get("workspace_id"))
            groups[key]["agents"].append(row)
        shared = set()
        other = []
        for agent in agents:
            pane_id = agent.get("pane_id")
            if pane_id == own or pane_id in bound or not _has_agent(agent):
                continue
            pane = panes.get(pane_id, agent)
            if not self._generic_in_scope(agent, pane, workspaces):
                continue
            workspace = workspaces.get(agent.get("workspace_id")) or {}
            worktree = workspace.get("worktree") if isinstance(workspace.get("worktree"), dict) else None
            if worktree and worktree.get("repo_key"):
                key = _canonical(worktree["repo_key"])
                git_path = worktree.get("repo_root") or worktree.get("checkout_path") or key
                groups.setdefault(key, {"git_path": git_path, "agents": []})
                shared.add(key)
                row = self._generic_agent(agent, pane, key, "worktree")
                row["herdr_checkout"] = worktree.get("checkout_path")
                groups[key]["agents"].append(row)
                continue
            cwd = agent.get("foreground_cwd") or pane.get("foreground_cwd") or agent.get("cwd") or pane.get("cwd")
            common = self._common_dir(cwd) if cwd else None
            fallback = agent.get("cwd") or pane.get("cwd")
            if common is None and fallback and fallback != cwd:
                common = self._common_dir(fallback)
            if not common:
                other.append(self._generic_agent(agent, pane, None, "none"))
                continue
            key = _canonical(common)
            groups.setdefault(key, {"git_path": self._checkout_from_common(common, cwd), "agents": []})
            shared.add(key)
            row = self._generic_agent(agent, pane, key, "cwd")
            row["cwd_hint"] = cwd
            groups[key]["agents"].append(row)
        for key in self._discover_projects():
            groups.setdefault(key, {"git_path": key, "agents": []})
            shared.add(key)
        # A private clone is listed on Agents only; it neither joins Pull Requests nor changes how
        # the projects there are labelled.
        agents_only = private - shared
        # A private clone Git proves was made from a listed project is listed under it; clones
        # Git proves share a source that is not listed share one heading. Their rows keep the
        # clone as project_key: their workspace, Git actions and messages use that.
        listed = [key for key in groups if key not in agents_only]
        siblings = {}
        for key in sorted(agents_only):
            origin = self._clone_origin(key, listed)
            if not origin:
                continue
            kind, source = origin
            matches = [source] if kind == "path" and source in listed else (
                self._listed_with_remote(source, listed) if kind == "url" else [])
            if len(matches) == 1:
                groups[matches[0]]["agents"].extend(groups.pop(key)["agents"])
            elif not matches:
                siblings.setdefault(origin, []).append(key)
        for keys in siblings.values():
            for key in keys[1:]:
                groups[keys[0]]["agents"].extend(groups.pop(key)["agents"])
        agents_only = {key for key in agents_only if key in groups}
        label_of = display_labels([key for key in groups if key not in agents_only])
        every = display_labels(list(groups))
        label_of.update({key: every[key] for key in agents_only})
        projects = []
        for key, label in sorted(label_of.items(), key=lambda item: item[1]):
            group = groups[key]
            previous = self._project(key) or {}
            project = {
                "key": key,
                "label": sanitize(label),
                "git_path": group["git_path"],
                "agents": sorted(group["agents"], key=lambda row: row["text"]),
                "pr_rows": [],
                "truncated": False,
                "agents_only": key in agents_only,
            }
            for row in project["agents"]:
                row["project_label"] = project["label"]
            for name in PR_FIELDS:
                if name in previous:
                    project[name] = previous[name]
            projects.append(project)
        self.projects = projects
        self.other_agents = other
        live_target = self._target_row()
        if live_target is not None:
            self.target = dict(live_target)
        if self.selection_key is None:
            first = next((project for project in projects if project["agents"]), None)
            if first:
                self.selection_key = first["agents"][0]["key"]

    def _discover_projects(self):
        if self.config.project_allowlist:
            candidates = [Path(item).expanduser() for item in self.config.project_allowlist]
        else:
            root = scope_root(self.config)
            if not root:
                return []
            try:
                candidates = sorted(Path(root).iterdir())[:PROJECT_LIMIT]
            except OSError:
                return []
        found = []
        for path in candidates:
            if path.name.startswith(".") or not (path / ".git").exists():
                continue
            if self._in_scope(str(path)):
                found.append(_canonical(str(path)))
        return found

    def _binding_is_live(self, meta, panes, agents, claims, own):
        pane_id = meta.get("herdr_pane_id")
        if not pane_id or pane_id == own or claims.get(pane_id, 0) != 1:
            return False
        if not self.session or meta.get("herdr_session") != self.session:
            return False
        pane = panes.get(pane_id)
        if not pane:
            return False
        if pane.get("workspace_id") != meta.get("herdr_workspace_id"):
            return False
        if pane.get("tab_id") != meta.get("herdr_tab_id"):
            return False
        agent = next((row for row in agents if row.get("pane_id") == pane_id), None)
        occupant = agent or pane
        return bool(occupant.get("agent") or occupant.get("name"))

    def _agent_title(self, *sources):
        """The title Herdr reports for the agent, as one clean line ('' when it reports none)."""
        for source in sources:
            for name in ("title", "terminal_title_stripped"):
                value = " ".join(sanitize((source or {}).get(name)).split())
                if value:
                    return value
        return ""

    def _status_change(self, terminal_id, status, source):
        """When this pane last saw the agent's status change, or None. Herdr reports no
        timestamp, so an agent first seen in its current state has no known age."""
        signature = (status, (source or {}).get("state_change_seq"))
        seen = self._status_seen.get(terminal_id)
        if seen is None:
            changed_at = None
        elif seen[0] == signature:
            changed_at = seen[1]
        else:
            changed_at = self.clock()
        self._status_seen[terminal_id] = (signature, changed_at)
        return changed_at

    def _fm_agent(self, meta, project_key, pane, agent):
        task_id = meta["task_id"]
        occupant = agent or pane
        status = occupant.get("agent_status") or pane.get("agent_status") or "unknown"
        name = "  " + sanitize(occupant.get("name") or occupant.get("agent"))
        prefix = f"{sanitize(task_id)}  "
        text = f"{prefix}{status}{name}"
        runtime, model, effort = agent_identity(occupant.get("agent") or pane.get("agent"), meta.get("harness"), meta.get("model"), meta.get("effort"))
        return {
            "kind": "agent",
            "key": ("agent", project_key, pane.get("terminal_id")),
            "project_key": project_key,
            "text": text,
            "status": status,
            "title": self._agent_title(agent, pane) or humanize(sanitize(task_id)),
            "changed_at": self._status_change(pane.get("terminal_id"), status, occupant),
            "pane_id": meta["herdr_pane_id"],
            "terminal_id": pane.get("terminal_id"),
            "workspace_id": pane.get("workspace_id"),
            "tab_id": pane.get("tab_id"),
            "task_id": task_id,
            "focusable": True,
            "label": sanitize(task_id),
            "harness": sanitize(occupant.get("name") or occupant.get("agent")),
            "runtime": runtime,
            "model": model,
            "effort": effort,
            "recorded": meta.get("worktree"),
            "herdr_checkout": None,
            "cwd_hint": None,
        }

    def _generic_agent(self, agent, pane, project_key, provenance):
        status = agent.get("agent_status") or "unknown"
        label = sanitize(agent.get("name") or agent.get("agent"))
        prefix = f"{label}  "
        text = f"{prefix}{status}  {provenance}"
        terminal_id = agent.get("terminal_id") or (pane or {}).get("terminal_id")
        key = ("other-agent", terminal_id) if project_key is None else ("agent", project_key, terminal_id)
        runtime, model, effort = agent_identity(agent.get("agent") or (pane or {}).get("agent"))
        return {
            "kind": "agent",
            "key": key,
            "project_key": project_key or "",
            "text": text,
            "status": status,
            "title": self._agent_title(agent, pane) or label,
            "changed_at": self._status_change(terminal_id, status, agent),
            "pane_id": agent.get("pane_id"),
            "terminal_id": terminal_id,
            "workspace_id": agent.get("workspace_id"),
            "tab_id": agent.get("tab_id"),
            "task_id": None,
            "focusable": bool(agent.get("pane_id") and terminal_id),
            "label": label,
            "harness": sanitize(agent.get("agent") or ""),
            "runtime": runtime,
            "model": model,
            "effort": effort,
            "recorded": None,
            "herdr_checkout": None,
            "cwd_hint": None,
            "project_label": "[other]",
        }

    def _common_dir(self, cwd):
        if not cwd:
            return None
        now = self.clock()
        cached = self._common_cache.get(cwd)
        if cached and (cached[0] is not None or now - cached[1] < GIT_RETRY):
            return cached[0]
        result = self.transport.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            timeout=GIT_TIMEOUT,
            cwd=str(self.state_dir),
        )
        common = None
        if result.code == 0 and (result.stdout or "").strip():
            common = os.path.realpath((result.stdout or "").strip())
        self._common_cache[cwd] = (common, now)
        return common

    def _git_text(self, path, *args):
        """Stripped stdout of `git -C path args`, or None when Git fails (retried later)."""
        now = self.clock()
        cached = self._git_cache.get((path, args))
        if cached and (cached[0] is not None or now - cached[1] < GIT_RETRY):
            return cached[0]
        result = self.transport.run(["git", "-C", path, *args], timeout=GIT_TIMEOUT, cwd=str(self.state_dir))
        text = (result.stdout or "").strip() if result.code == 0 else None
        self._git_cache[(path, args)] = (text, now)
        return text

    def _clone_source(self, path):
        """Where Git says the repository at `path` came from: its origin URL, else (no origin)
        what its first HEAD reflog entry, `clone: from <source>`, names. None when unknown."""
        origin = self._git_text(path, "config", "--default", "", "--get", "remote.origin.url")
        if origin is None or origin:
            return origin
        log = self._git_text(path, "reflog", "show", "--format=%gs", "HEAD")
        first = (log or "").splitlines()[-1:] or [""]
        return first[0].removeprefix("clone: from ") if first[0].startswith("clone: from ") else None

    def _clone_origin(self, path, listed, depth=4):
        """What the clone at `path` was made from, as ("path", Git common dir) or ("url", remote),
        or None when Git does not prove it. A local source is matched by its Git common dir (a
        clone of another of this home's clones is followed to where that came from, unless the
        source is listed); a name or shared history alone proves nothing."""
        source = self._clone_source(path)
        if not source or depth == 0:
            return None
        if source.startswith("/"):
            common = self._common_dir(source)
            key = _canonical(common) if common else None
            if not key:
                return None
            if key not in listed and key != path and path_in_scope(key, str(Path(self.config.firstmate_home) / "data")):
                return self._clone_origin(key, listed, depth - 1) or ("path", key)
            return ("path", key)
        remote = parse_git_remote_url(source)
        return ("url", remote) if remote else None

    def _listed_with_remote(self, remote, listed):
        return [key for key in listed if parse_git_remote_url(self._git_text(
            key, "config", "--default", "", "--get", f"remote.{self.config.remotes.get(key, 'origin')}.url")) == remote]

    def _checkout_from_common(self, common, cwd):
        path = Path(common)
        if path.name == ".git":
            return str(path.parent)
        return cwd or common

    def _project(self, key):
        for project in self.projects:
            if project["key"] == key:
                return project
        return None

    def _meta(self, task_id):
        for meta in load_metas(self.config.firstmate_home):
            if meta["task_id"] == task_id:
                return meta
        return None

    def _refresh_prs(self, force):
        """Fetch the selected agent's repository only, while the Pull Request source is showing."""
        if not self._on("prs"):
            return
        project = self._agent_project()
        if project is not None:
            self._fetch_prs(project, force=force)
        current = self._current_pr()
        if current:
            self._fetch_pr_detail(current, force=force)

    def _fetch_prs(self, project, force):
        if project is None or project["key"] in self._fetches:
            return
        now = self.clock()
        if (
            not force
            and project.get("fetched_at") is not None
            and now - project["fetched_at"] < PR_CADENCE
        ):
            return
        git_path = project["git_path"]
        key = project["key"]
        loaded = project.get("remote_loaded", False)
        cached = (project.get("choice"), project.get("pr_reason"))
        preferred = self.config.remotes.get(key, "origin")
        self._fetches[key] = self.submit(
            lambda: self._load_prs(key, git_path, preferred, loaded, cached, now)
        )
        self.poll()

    def poll(self):
        for key, future in list(self._fetches.items()):
            if not future.done():
                continue
            del self._fetches[key]
            project = self._project(key)
            if project is None:
                continue
            try:
                update = future.result()
            except Exception:
                update = {"fetched_at": self.clock(), "pr_error": "PRs unavailable: gh failed"}
            project.update(update)
        self._poll_workspaces()
        self._poll_capture()
        self._poll_pr_details()
        self._poll_board()
        self._poll_origins()
        self._poll_board_detail()
        self._poll_review()
        if hasattr(self, "_terminal_pump"):
            self._terminal_pump()

    def _poll_review(self):
        supervisor = self.review_supervisor
        if supervisor is None:
            return
        outcome = supervisor.tick()
        if not outcome or supervisor.run_dir is None:
            return
        self.review_supervisor = None
        self.status = outcome
        result_path = supervisor.run_dir / "stdout.json"
        if outcome != "complete" or not result_path.is_file():
            return
        request_path = supervisor.run_dir / "request.json"
        try:
            request = json.loads(request_path.read_text())
            outcome_name, result, error = validate_result(
                result_path.read_bytes(), request, request.get("patch", {}).get("text", "")
            )
        except (OSError, json.JSONDecodeError):
            self.status = "review output unreadable"
            return
        if result is None:
            self.status = error or outcome_name
            return
        self.review_findings = result["findings"]
        self.status = f"review {outcome_name}: {len(self.review_findings)} findings"

    def _load_prs(self, key, git_path, preferred, loaded, cached, now):
        update = {"fetched_at": now, "pr_error": None}
        choice, reason = cached
        if not loaded:
            remotes = self.transport.run(
                ["git", "-C", git_path, "remote", "-v"],
                timeout=GIT_TIMEOUT,
                cwd=str(self.state_dir),
            )
            if remotes.code != 0:
                choice, reason = None, "PRs unavailable: git remote failed"
            else:
                choice = choose_remote(
                    remotes.stdout, preferred=preferred, allowed_hosts=self.config.github_hosts
                )
                reason = choice.reason
        update.update(remote_loaded=choice is not None, choice=choice, pr_reason=reason)
        if not choice:
            update["pr_error"] = reason
            return update
        if not choice.slug:
            return update
        result = self.transport.run(
            [
                "gh",
                "pr",
                "list",
                "-R",
                choice.slug,
                "--state",
                "open",
                "--limit",
                str(LIST_LIMIT),
                "--json",
                "number,title,isDraft,reviewDecision,statusCheckRollup,headRefName,updatedAt",
            ],
            env=self._gh_env(),
            timeout=LIST_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.timed_out or result.code != 0:
            update["pr_error"] = "PRs unavailable: gh failed"
            return update
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, list):
            update["pr_error"] = "PRs unavailable: gh returned unreadable output"
            return update
        shown = []
        for item in payload[:SHOW_LIMIT]:
            row = self._pr_row(choice, item, key)
            if row:
                shown.append(row)
        update.update(pr_rows=shown, truncated=len(payload) >= LIST_LIMIT)
        return update

    def _pr_row(self, choice, item, project_key):
        if not isinstance(item, dict) or not isinstance(item.get("number"), int):
            return None
        number = item["number"]
        draft = "draft" if item.get("isDraft") else "not-draft"
        title = sanitize(item.get("title") or "")
        text = (
            f"#{number}  {draft}  checks {checks_label(item.get('statusCheckRollup'))}  "
            f"review {review_label(item.get('reviewDecision'))}  {title}"
        )
        return {
            "kind": "pr",
            "key": ("pr", project_key, choice.host, choice.owner, choice.repo, number),
            "project_key": project_key,
            "text": text,
            "number": number,
            "host": choice.host,
            "owner": choice.owner,
            "repo": choice.repo,
            "slug": choice.slug,
            "title": title,
            "head": sanitize(item.get("headRefName") or ""),
        }

    def _selected_pr(self, row):
        if not row or row.get("kind") != "pr":
            return None
        return row

    def _open_detail(self, row):
        pr = self._selected_pr(row)
        if not pr:
            return
        result = self.transport.run(
            [
                "gh",
                "pr",
                "view",
                str(pr["number"]),
                "-R",
                pr["slug"],
                "--json",
                "number,title,author,state,isDraft,headRefName,baseRefName,body,reviewDecision,statusCheckRollup,updatedAt",
            ],
            env=self._gh_env(),
            timeout=VIEW_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.timed_out or result.code != 0:
            self.status = "PR detail unavailable"
            return
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            self.status = "PR detail unavailable"
            return
        url = f"https://{pr['host']}/{pr['owner']}/{pr['repo']}/pull/{pr['number']}"
        author = ""
        if isinstance(payload.get("author"), dict):
            author = sanitize(payload["author"].get("login") or "")
        body = sanitize(payload.get("body") or "")
        self.detail_text = "\n".join(
            [
                f"PR {pr['slug']}#{pr['number']}",
                url,
                sanitize(payload.get("title") or ""),
                author,
                body,
            ]
        )
        self.screen = "detail"
        self.pr_selection_key = row["key"]

    def _open_pr_review(self, pr, path=None):
        """The PR's whole diff, or the pinned PR commit's, in the shared diff viewport; with
        `path`, the cursor starts on that file's first changed line."""
        head = (((self._pr_detail(pr) or {}).get("view")) or {}).get("head_oid")
        if not head:
            self.status = "PR head unknown: wait for the PR to load"
            return
        commit = self.pr_commit_oid
        if commit:
            argv = ["gh", "api", "--hostname", pr["host"], "-H", "Accept: application/vnd.github.diff", f"repos/{pr['owner']}/{pr['repo']}/commits/{commit}"]
        else:
            argv = ["gh", "pr", "diff", str(pr["number"]), "-R", pr["slug"], "--color", "never"]
        result = self.transport.run(argv, env=self._gh_env(), timeout=DIFF_TIMEOUT, cwd=str(self.state_dir))
        if result.timed_out or result.code != 0:
            self.status = "PR diff unavailable"
            return
        text = sanitize(result.stdout or "")
        if len(text) > DIFF_CAP:
            text = text[:DIFF_CAP] + "\n[diff truncated]\n"
        parsed = parse_diff(text)
        paths = patch_line_paths(text)
        kept = [(line, path) for line, path in zip(parsed, paths) if not (line.kind == "h" and line.text.startswith(HIDDEN_HEADERS))]
        lines = [line for line, _ in kept]
        info = self._ws_info(self.target) if self.target else None
        ws = (info or {}).get("workspace")
        same_project = bool(ws and ws.path and (self.target or {}).get("project_key") == pr["project_key"])
        self.diff_view = {
            "key": ("pr", tuple(pr["key"]), commit),
            "pr": tuple(pr["key"]),
            "entry": FileEntry("pr", "P", f"PR #{pr['number']}" + (f" commit {commit[:7]}" if commit else "")),
            "lines": lines,
            "cursor": next((index for index, line in enumerate(lines) if line.kind in "+-" and path in (None, kept[index][1])), 0),
            "mark": None,
            "top": 0,
            "xoff": 0,
            "error": "" if lines else "no line changes to show",
            "workspace": ws.path if same_project else None,
            "head": head,
            "branch": None,
            "patch": text,
            "paths": [path for _, path in kept],
            "revision": commit or "head",
        }
        self.pr_diff_open = True

    def _open_diff(self, row, path=None):
        """The PR's diff in the pane; with `path`, only that file's part of it."""
        pr = self._selected_pr(row)
        if not pr:
            return
        result = self.transport.run(
            ["gh", "pr", "diff", str(pr["number"]), "-R", pr["slug"], "--color", "never"],
            env=self._gh_env(),
            timeout=DIFF_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.timed_out or result.code != 0:
            self.status = "PR diff unavailable"
            return
        text = sanitize(result.stdout or "")
        title = f"Diff {pr['slug']}#{pr['number']}"
        if path is not None:
            text = file_section(text, path)
            if text is None:
                self.status = f"no diff for {path} in the PR diff"
                return
            title += f" {path}"
        if len(text) > DIFF_CAP:
            text = text[:DIFF_CAP] + "\n[diff truncated]\n"
        self.detail_text = title + "\n" + text
        self.screen = "diff"
        self.pr_selection_key = row["key"]

    def _arm_browser(self, row):
        pr = self._selected_pr(row)
        if not pr:
            return
        url = f"https://{pr['host']}/{pr['owner']}/{pr['repo']}/pull/{pr['number']}"
        self.pending = {"url": url, "kind": "open"}
        self.screen = "confirm-open"

    def _confirm_open(self, plain):
        url = (self.pending or {}).get("url") or ""
        return f"Open {url} in the browser?\ny opens, any other key cancels"

    def _open_browser(self):
        url = (self.pending or {}).get("url")
        self.screen = "tree"
        self.pending = None
        if not url or not url.startswith("https://"):
            self.status = "browser open refused"
            return
        self.transport.browser_open(url)

    def _arm_comment(self, row):
        pr = self._selected_pr(row)
        if not pr:
            return
        draft = self._draft("comment", pr)
        path = self._draft_path(draft)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("")
        body = self._edit(path)
        if body is None:
            return
        path.write_text(body)
        draft["body"] = body
        self._write_draft(draft)
        login = self.logins.get(pr["host"])
        if not login:
            login = self._fetch_login(pr["host"])
            if login:
                self.logins[pr["host"]] = login
        self.pending = {"draft_id": draft["id"], "pr": pr, "body": body, "login": login}
        self.screen = "confirm-comment"

    def _confirm_comment(self, plain):
        pending = self.pending or {}
        pr = pending.get("pr") or {}
        login = pending.get("login") or "unknown"
        target = f"{pr.get('slug')}#{pr.get('number')}"
        body = pending.get("body") or ""
        return (
            f"Post public comment as @{login} to {target}\n"
            "---\n"
            f"{body}\n"
            "---\n"
            "y posts, any other key keeps the draft"
        )

    def _send_comment(self):
        pending = self.pending or {}
        pr = pending.get("pr") or {}
        draft = self._read_draft(pending.get("draft_id"))
        path = self._draft_path(draft) if draft else None
        self.screen = "tree"
        self.pending = None
        if not draft or path is None or not pr:
            self.status = "comment outcome: unknown"
            return
        body = sanitize(path.read_text() if path.exists() else pending.get("body") or "")
        path.write_text(body)
        result = self.transport.run(
            [
                "gh",
                "pr",
                "comment",
                str(pr["number"]),
                "-R",
                pr["slug"],
                "--body-file",
                str(path),
            ],
            env=self._gh_env(),
            timeout=COMMENT_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.timed_out or result.code != 0:
            draft["outcome"] = "unknown"
            self._write_draft(draft)
            self.status = "comment outcome: unknown"
            return
        draft["outcome"] = "posted"
        self._write_draft(draft)
        self.status = "comment outcome: posted"

    def _arm_note(self, row):
        reason = self._note_block_reason()
        if reason:
            self.status = reason
            return
        pr = self._selected_pr(row)
        if not pr:
            self.status = "select a pull request first"
            return
        draft = self._draft("note", pr)
        path = self._draft_path(draft)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("")
        body = self._edit(path)
        if body is None:
            return
        path.write_text(body)
        draft["body"] = body
        self._write_draft(draft)
        project = self._project(pr["project_key"])
        label = project["label"] if project else pr["project_key"]
        self.pending = {"draft_id": draft["id"], "pr": pr, "body": body, "label": label}
        self.screen = "confirm-note"

    def _confirm_note(self, plain):
        pending = self.pending or {}
        pr = pending.get("pr") or {}
        url = f"https://{pr.get('host')}/{pr.get('owner')}/{pr.get('repo')}/pull/{pr.get('number')}"
        composed = self._note_text(url, pr, pending.get("label") or "", pending.get("body") or "")
        return (
            f"Queue note to FirstMate at {self.config.firstmate_home}\n"
            "---\n"
            f"{composed}\n"
            "---\n"
            "y sends, any other key keeps the draft"
        )

    def _note_text(self, url, pr, label, user_text):
        return (
            f"Firstpane: {url} ({pr.get('owner')}/{pr.get('repo')} #{pr.get('number')}, "
            f"project {label})\n\n{user_text}"
        )

    def _send_note(self):
        pending = self.pending or {}
        draft = self._read_draft(pending.get("draft_id"))
        pr = pending.get("pr") or {}
        self.screen = "tree"
        self.pending = None
        reason = self._note_block_reason()
        if reason or not draft or not pr:
            self.status = reason or "note outcome: not-saved"
            return
        url = f"https://{pr['host']}/{pr['owner']}/{pr['repo']}/pull/{pr['number']}"
        user_text = sanitize(draft.get("body") or "")
        stdin = self._note_text(url, pr, pending.get("label") or "", user_text)
        script = str(Path(self.config.firstmate_root) / "bin" / "fm-inbox.sh")
        result = self.transport.run(
            [script, "note", "--request-id", draft["request_id"], "--json", "-"],
            stdin=stdin,
            env=self._inbox_env(),
            timeout=NOTE_TIMEOUT,
            cwd=str(self.state_dir),
        )
        self._apply_note_result(draft, result)

    def _apply_note_result(self, draft, result):
        outcome, note_id = note_outcome(result)
        if note_id:
            draft["note_id"] = note_id
        draft["outcome"] = outcome
        self.status = f"note outcome: {outcome}"
        self._write_draft(draft)
        self.pending = {"draft_id": draft["id"], "outcome": draft["outcome"], "note_id": draft.get("note_id")}

    def _announce(self):
        if self._note_block_reason():
            return
        draft = self._unannounced_note()
        if not draft:
            return
        script = str(Path(self.config.firstmate_root) / "bin" / "fm-inbox.sh")
        result = self.transport.run(
            [script, "announce", "--json", draft["note_id"]],
            env=self._inbox_env(),
            timeout=NOTE_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.code == 0:
            draft["outcome"] = "queued"
            self.status = "note outcome: queued"
            if draft.get("kind") == "message":
                if not self.drafts.save(draft):
                    self.drafts.archive(draft)
                if draft.get("id") == (self.draft or {}).get("id"):
                    self._drop_inline()
            else:
                self._write_draft(draft)
        else:
            self.status = "note outcome: saved-unannounced"

    def _note_block_reason(self):
        home = self.config.firstmate_home
        root = self.config.firstmate_root
        if not home:
            return "FirstMate not configured"
        if not root:
            return "FirstMate tool root is not configured"
        try:
            if os.path.realpath(home) == os.path.realpath(root):
                return "FirstMate home and tool root must be different"
        except OSError:
            return "FirstMate home and tool root must be different"
        script = Path(root) / "bin" / "fm-inbox.sh"
        if not script.is_file():
            return "FirstMate inbox tool is missing"
        return None

    def _fetch_login(self, host):
        result = self.transport.run(
            ["gh", "api", "--hostname", host, "user", "--jq", ".login"],
            env=self._gh_env(),
            timeout=LOGIN_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.timed_out or result.code != 0:
            return None
        login = sanitize((result.stdout or "").strip().splitlines()[0] if result.stdout else "")
        if login and all(char.isalnum() or char == "-" for char in login):
            return login
        return None

    def _gh_env(self):
        return {**self.env, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}

    def _inbox_env(self):
        return {**self.env, "FM_HOME": self.config.firstmate_home}

    def _draft(self, kind, pr):
        target = f"{kind}:{pr['slug']}#{pr['number']}"
        existing = self._find_draft(target)
        if existing:
            return existing
        draft_id = secrets.token_hex(8)
        draft = {
            "id": draft_id,
            "kind": kind,
            "target": target,
            "request_id": "firstpane-" + draft_id,
            "body": "",
            "outcome": None,
            "note_id": None,
        }
        self._write_draft(draft)
        return draft

    def _draft_path(self, draft):
        return self.state_dir / "drafts" / f"{draft['id']}.md"

    def _write_draft(self, draft):
        directory = self.state_dir / "drafts"
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f".{draft['id']}.{os.getpid()}.tmp"
        temp.write_text(json.dumps(draft))
        os.replace(temp, directory / f"{draft['id']}.json")

    def _read_draft(self, draft_id):
        if not draft_id:
            return None
        path = self.state_dir / "drafts" / f"{draft_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _find_draft(self, target):
        directory = self.state_dir / "drafts"
        if not directory.is_dir():
            return None
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(data, dict)
                and data.get("target") == target
                and data.get("outcome") not in TERMINAL_OUTCOMES
            ):
                return data
        return None

    def _unannounced_note(self):
        directory = self.state_dir / "drafts"
        if not directory.is_dir():
            return None
        found = []
        for path in directory.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                modified = path.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(data, dict)
                and data.get("kind") in ("note", "message")
                and data.get("outcome") == "saved-unannounced"
                and data.get("note_id")
            ):
                found.append((modified, data))
        return max(found, key=lambda item: item[0])[1] if found else None


    def _edit(self, path):
        if hasattr(self.terminal, "disable_mouse"):
            self.terminal.disable_mouse()
        self.terminal.leave_raw()
        try:
            try:
                code = self.transport.open_editor(str(path), self.terminal)
            except Exception:
                self.status = "editor cancelled, draft kept"
                return None
        finally:
            self.terminal.enter_raw()
        if code != 0:
            self.status = "editor cancelled, draft kept"
            return None
        try:
            return sanitize(path.read_text())
        except OSError:
            self.status = "editor cancelled, draft kept"
            return None

    def _load_ui(self):
        path = self.state_dir / "ui-state.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.folded_projects = set(data.get("folded_projects") or [])
        self.folded_prs = set(data.get("folded_prs") or [])
        selection = data.get("selection")
        if isinstance(selection, list):
            self.selection_key = tuple(selection)
        pr_selection = data.get("pr_selection")
        if isinstance(pr_selection, list):
            self.pr_selection_key = tuple(pr_selection)
        pr_choice = data.get("pr_choice")
        if isinstance(pr_choice, list):
            self.pr_choice = tuple(pr_choice)
        if data.get("pr_section") in PR_SECTIONS:
            self.pr_section = data["pr_section"]
        self.pr_resolved_open = data.get("pr_resolved_open") is True
        last = data.get("last_agent") or {}
        if isinstance(last, dict):
            self.last_agent = {key: tuple(value) for key, value in last.items() if isinstance(value, list)}
        self._load_settings(data)
        saved, source, hidden = migrate_ui_state(data, TABS)
        self.hidden_tabs = hidden
        self.review_source = source
        skills = data.get("review_skills")
        if isinstance(skills, list):
            self.review_skills = [item for item in skills if isinstance(item, str)]
        self.tab = saved
        if not self._visible_tabs():
            self.settings_open = True
        self._show_browser(self.tab == "browser")
        if isinstance(data.get("board_project"), str):
            self.board_project = data["board_project"]
        if data.get("board_active") in ("all", "active"):
            self.board_active = data["board_active"]
        if isinstance(data.get("board_column"), str):
            self.board_column = data["board_column"]
        if isinstance(data.get("board_selected"), str) or data.get("board_selected") is None:
            self.board_selected = data.get("board_selected") or self.board_selected
        self.board_done_open = data.get("board_done_open") is True
        target = data.get("target")
        if isinstance(target, dict) and isinstance(target.get("key"), list):
            target["key"] = tuple(target["key"])
            self.target = target

    def _save_ui(self):
        path = self.state_dir / "ui-state.json"
        target = None
        if self.target:
            target = {name: value for name, value in self.target.items() if name in TARGET_FIELDS}
            target["key"] = list(self.target["key"])
        payload = {
            "folded_projects": sorted(self.folded_projects),
            "folded_prs": sorted(self.folded_prs),
            "selection": list(self.selection_key) if self.selection_key else None,
            "pr_selection": list(self.pr_selection_key) if self.pr_selection_key else None,
            "pr_choice": list(self.pr_choice) if self.pr_choice else None,
            "pr_section": self.pr_section,
            "pr_resolved_open": self.pr_resolved_open,
            "last_agent": {key: list(value) for key, value in self.last_agent.items()},
            "tab": self.tab,
            "review_source": self.review_source,
            "review_skills": list(self.review_skills),
            "target": target,
            "board_project": self.board_project,
            "board_active": self.board_active,
            "board_column": self.board_column,
            "board_selected": self.board_selected,
            "board_done_open": self.board_done_open,
            "browser_placement": self.browser_placement,
            "hidden_tabs": [name for name in TABS if name in self.hidden_tabs],
        }
        temp = path.with_name(f".ui-state.{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload))
        os.replace(temp, path)
