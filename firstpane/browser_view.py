"""Browser tab: the selected agent's own browser, where Settings says: drawn inside Firstpane in
the tab body (Inside Firstpane, firstpane/browser.py), or in a new tab of the Ghostty window in
front (Ghostty tab, the default, firstpane/placed.py).

Every agent context (agent identity plus its source worktree) has its own profile and remembered
address; one context's browser is open at a time, in one placement: choosing the tab first ends
the browser open in the other placement (its address is remembered). Choosing the tab opens the
selected context's browser at its remembered address (or blank), or keeps the one already open;
the user types addresses in the browser's own address field. `x` closes it. `a` (or Ctrl-G)
captures the page URL, title and marked selection into the inline message
(firstpane/composer_view.py); a pick made with the browser's own select tool (toolbar icon)
arrives through its agent bridge and opens the same message with the picked element. There is
no global message field.

Inside Firstpane the page fills the tab. A line appears only for an error, a refusal, a capture
in progress, or a frame transport that is not the fast path; shortcut prose is not printed. Keys
stay with Firstpane until the page gets the keyboard (`f`, Enter, or a click on the page, which
is also how text is selected); then every key except Ctrl-G (message FirstMate), Ctrl-O (back to
Firstpane) and Ctrl-C (exit) goes to the page, with its modifiers. The wheel and plain pointer
movement reach the page under the pointer either way.

In a Ghostty tab the Browser tab draws no page: it says whose browser is open there and the keys
that act on it; `f` or Enter brings that tab forward.
"""

from firstpane.browser import who
from firstpane.composer_view import anchor_key
from firstpane.embed import CaptureError
from firstpane.icons import NO_GRAPHICS
from firstpane.pr_view import word_wrap
from firstpane.sanitize import sanitize
from firstpane.settings_view import PLACEMENT_LABELS
from firstpane.style import INK, MUTED, paint, rgb_fg

MOUSE_BUTTONS = ("left", "middle", "right", "none")
IMAGE_COLUMN = 2  # the page image starts in this (1-based) column of the body


class BrowserTabMixin:
    # ---- context -----------------------------------------------------------------------
    def _browser_context(self):
        """(context, "") for the selected agent's browser, else (None, why there is none)."""
        target = self.target
        if not target:
            return None, "no agent selected: select an agent on the Agents tab (move onto it or press Enter); each agent has its own browser"
        info = self._ws_info(target)
        if info is None:
            return None, f"reading {target.get('label')}'s worktree…"
        ws = info.get("workspace")
        if target.get("task_id"):
            identity = ("fm", target["task_id"])
        else:
            identity = ("herdr", target.get("terminal_id"), target.get("project_key") or "")
        return self.placed.context(identity, target.get("label"), ws.path if ws and ws.path else None), ""

    def _browser_activate(self):
        """Choosing the tab opens the selected agent's browser in the chosen placement: the one
        already open there is kept as it is, otherwise it starts at that agent's remembered
        address (or blank). A browser open in the other placement is closed first."""
        context, why = self._browser_context()
        if context is None:
            self.status = why
            return
        ended = ""
        if self.browser_placement == "embedded":
            if self.placed.record is not None:
                ended = self.placed.close()
                if self.placed.record is not None:
                    self.status = ended
                    return
        else:
            self.browser.close()  # also drops an in-tab start still waiting for the terminal
        context, _ = self._browser_context()  # the address the closed browser was just at
        if self.browser_placement == "embedded":
            refusal = self.browser.open(context)
            status, opened = refusal or ended, not refusal
        else:
            status = self.placed.open(context)
            owner = self.placed.owner()
            opened = bool(owner) and owner["id"] == context["id"]
        shared = self.placed.same_address(context, context["url"]) if opened else []
        self.status = f"! same address as {who(shared[0])}: one server for both" if shared else status

    def _browser_shows(self, context):
        owner = self.browser.owner()
        return owner is not None and context is not None and owner["id"] == context["id"]

    # ---- body --------------------------------------------------------------------------
    def _browser_bar(self):
        """The last action's outcome, a kept direct pick, a capture in progress, and inside
        Firstpane a browser state other than running on the fast path."""
        parts = []
        if self.status and self.status != NO_GRAPHICS:
            parts.append(self.status)
        elif self._direct_pick_waiting():
            kept = sanitize(self.draft.get("body") or "").split("\n")[0][:80]
            parts.append(f"context kept: {kept}; m previews")
        if self.capture_job is not None:
            parts.append("capturing…")
        if self.browser_placement == "embedded":
            described = self.browser.describe()
            if described != "running" and not self.status:
                parts.append(described)
        return " · ".join(sanitize(part) for part in parts)

    def _browser_body(self, plain, budget):
        lines = []
        embedded = self.browser_placement == "embedded"
        bar = self._browser_bar()
        if embedded:
            # One line at most: every change of the page's rows resizes the image.
            lines += [(" " + paint(bar, rgb_fg(INK), plain), None)] if bar else []
        else:
            for text, sgr in ((bar, INK), (sanitize(self.placed.describe()), INK),
                              (f"opens in: {PLACEMENT_LABELS[self.browser_placement]} (, Settings) \u00b7 f brings it forward"
                               " \u00b7 a captures the page \u00b7 x closes it", MUTED)):
                lines += [(" " + paint(part, rgb_fg(sgr), plain), None) for part in (word_wrap(text, max(1, self.cols - 1)) if text else [])]
        anchor = (self.draft or {}).get("anchor") or {}
        if self.draft is not None and anchor.get("kind") == "browser" and not anchor.get("direct"):
            lines += self._inline_rows(self.draft, plain, 1)
        if embedded:
            rows = max(1, budget - len(lines))
            cols = max(1, self.cols - IMAGE_COLUMN + 1)
            self.browser.set_region(rows, cols)
            image = self.browser.body_lines(rows, cols)
            lines += [(" " * (IMAGE_COLUMN - 1) + row, {"kind": "browser", "row": index}) for index, row in enumerate(image)]
        return [text for text, _ in lines], [hit for _, hit in lines]

    # ---- keys --------------------------------------------------------------------------
    def _browser_list_key(self, key):
        if key in ("f", "enter"):
            if self.browser_placement == "embedded":
                self._give_page_keyboard()
            else:
                context, why = self._browser_context()
                self.status = self.placed.focus(context) if context else why
        elif key in ("a", "ctrl-g"):
            self._start_capture()
        elif key == "x":
            self.status = self.browser.close() if self.browser.running else self.placed.close()
        elif key == "m":
            if self._direct_pick_waiting():
                self.composer.set(self.draft.get("body") or "")
                self.focus = "list"
                self._arm_send()
            else:
                drafts = self._agent_drafts("browser")
                if drafts:
                    self._open_target(max(drafts, key=lambda draft: draft["anchor"].get("captured_at") or "")["target"])
        else:
            return False
        return True

    def _give_page_keyboard(self):
        context, why = self._browser_context()
        if context is None:
            self.status = why
        elif not self._browser_shows(context):
            self.status = "no browser in this tab yet; choose the Browser tab to start it"
        elif self.browser.grid is None:
            self.status = "the page is still starting"
        else:
            self.focus = "browser"

    def _browser_key(self, key):
        if key == "ctrl-o" or not self.browser.running or self.browser.grid is None:
            self.focus = "list"
            return
        if key == "ctrl-g":
            self._start_capture()
            return
        notice = self.browser.key(key)
        if notice:
            self.status = notice

    def _browser_mouse(self, kind, button, column, row, pixel=None, mods=None):
        """A press on the page gives it the keyboard and reaches the page; drags and releases
        follow, also past the image edge (clamped to it). Positions are pixels from the image's
        top-left corner (cell centres without pixel mouse reports)."""
        browser = self.browser
        tops = [index for index, hit in enumerate(self.hit_rows) if hit and hit.get("kind") == "browser"]
        if kind == "up":
            self._page_button = None
        if not browser.running or browser.grid is None or not tops:
            return
        if kind == "down":
            self._page_button = button
            if self.focus != "browser":
                self.focus = "browser"
        cw, ch = browser.cell
        rows = len(tops)
        cols = min(browser.region[1], int(browser.grid.get("cols") or browser.region[1]))
        if pixel is not None:
            x = pixel[0] - (IMAGE_COLUMN - 1) * cw
            y = pixel[1] - tops[0] * ch
        else:
            x, y = browser.cell_center(column - IMAGE_COLUMN, row - 1 - tops[0])
        browser.mouse(kind, button, min(max(x, 0), cols * cw - 1), min(max(y, 0), rows * ch - 1), mods)

    # ---- capture -----------------------------------------------------------------------
    def _start_capture(self, pick=None):
        """Read the shown page (URL, title, marked text) into a message to FirstMate. `pick` is a
        select-tool pick (connection, element): the element goes into the message as well, and
        the browser hears whether a message opened for it."""
        context, refusal = self._capture_context()
        if refusal:
            if pick is None and self.capture_job is not None:
                return
            self.status = refusal
            if pick is not None:
                self.placed.answer_pick(pick[0], False)
            return
        self.capture_for = (context["id"], self._agent_target())
        self.capture_pick = pick
        page = self.browser if self.browser.running else self.placed
        self.capture_job = self.submit(lambda: page.capture(context))
        self._poll_capture()

    def _capture_context(self):
        """(the selected agent's context, None) while its browser is open, else (None, why)."""
        if self.capture_job is not None:
            return None, "capture refused: the last capture is still running"
        context, why = self._browser_context()
        if context is None:
            return None, "capture refused: " + why
        self.browser.refresh()
        if self.browser.running:
            owner, where = self.browser.owner(), "this tab"
        else:
            self.placed.refresh()
            owner, where = (self.placed.owner() if self.placed.state == "open" else None), self.placed.where()
        if owner is None:
            return None, "capture refused: no open browser from this Firstpane"
        if owner["id"] != context["id"]:
            return None, f"capture refused: {where} shows {sanitize(who(owner))}'s browser, not {sanitize(who(context))}'s"
        return context, None

    def _poll_capture(self):
        job = self.capture_job
        if job is None or not job.done():
            return
        self.capture_job = None
        bound, self.capture_for = self.capture_for, None
        pick, self.capture_pick = self.capture_pick, None
        note = pick[2] if pick and len(pick) > 2 else ""
        opened = self._capture_done(job, bound, pick[1] if pick else None, note)
        if pick is not None:
            self.placed.answer_pick(pick[0], opened)

    def _keep_direct_note(self, note, element, why, target=None):
        note = sanitize(note).strip()
        if not note:
            self.status = why
            return False
        record = {"kind": "browser", "url": "", "title": "", "selection": "", "direct": True}
        if element:
            record["element"] = sanitize(element)
        owner = target or self._agent_target()
        if owner == self._agent_target():
            self._open_inline(record)
            self.composer.set(note)
            self.draft["body"] = note
            self._save_draft()
            self.focus = "list"
        else:
            key = f"{owner}|{anchor_key(record)}"
            draft = self.drafts.load(key, {})
            if not draft.get("anchor"):
                draft["anchor"] = record
                draft["attachments"] = [record]
            else:
                self._stamp_direct(draft, record)
            draft["body"] = note
            self.drafts.save(draft)
        self.status = f"{why}; context kept: {note.split(chr(10))[0][:80]}; m previews again"
        return True

    def _direct_pick_waiting(self):
        anchor = (self.draft or {}).get("anchor") or {}
        return anchor.get("kind") == "browser" and anchor.get("direct") and bool((self.draft.get("body") or "").strip())

    def _capture_done(self, job, bound, element, note=""):
        """Open the message for a finished capture; True when it opened."""
        context, _ = self._browser_context()
        owner = bound[1] if bound else None
        if context is None or (context["id"], self._agent_target()) != bound:
            why = "capture discarded: the selected agent changed before it finished"
            if self._keep_direct_note(note, element, why, target=owner):
                return True
            self.status = why
            return False
        if self.tab != "browser" or self.screen != "tree":
            why = "capture discarded: the Browser tab was left before it finished; press a there again"
            if self._keep_direct_note(note, element, why, target=owner):
                return True
            self.status = why
            return False
        try:
            value = job.result()
        except CaptureError as error:
            return self._keep_direct_note(note, element, f"capture failed: {sanitize(str(error))}")
        except Exception as error:
            return self._keep_direct_note(note, element, f"capture failed: {sanitize(type(error).__name__)}")
        record = {
            "kind": "browser",
            "url": value.get("url"),
            "title": value.get("title"),
            "selection": value.get("selection") or "",
            "browser_key": value.get("browser_key"),
            "tab_id": value.get("tab_id"),
            "target_id": value.get("target_id"),
            "captured_at": value.get("captured_at"),
        }
        if element:
            record["element"] = sanitize(element)
        note = sanitize(note).strip()
        if note:
            record["direct"] = True
        self._open_inline(record)
        page = record["title"] or record["url"]
        if note:
            self.composer.set(note)
            self.draft["body"] = note
            self._save_draft()
            self.focus = "list"
            self._arm_send()
            if self.screen != "confirm-send":
                kept = note.split("\n")[0][:80]
                self.status = f"{self.status}; context kept: {kept}; m previews again"
            return True
        if element:
            self.status = f"picked an element on {page}: write the message"
        else:
            self.status = f"captured {page} (selection {len(record['selection'])} chars): write the message"
        return True

    # ---- main loop hooks -------------------------------------------------------------
    def io_fds(self):
        return self.browser.fds() + self.placed.fds()

    def io_deadline(self):
        return self.browser.deadline()

    def layout_ready(self, now):
        """Whether the screen just rendered may be written now: a change of where the page
        image is placed waits for a quiet window after the last frame (Browser.place)."""
        tops = [index for index, hit in enumerate(self.hit_rows) if hit and hit.get("kind") == "browser"]
        return self.browser.place((tops[0], len(tops), self.browser.drawn) if tops else None, now)

    def pump(self, ready, now):
        self._settle_frames()
        self._settle_icons()
        data = self.icons.pending(self.browser.cell) + self.browser.pump(ready, now)
        self.placed.pump(ready)
        for pick in self.browser.take_picks() + self.placed.take_picks():
            note = pick[2] if len(pick) > 2 else ""
            if note.strip() and self.screen.startswith("confirm-"):
                self.placed.answer_pick(pick[0], False)
                self.status = "context kept: a preview is already open"
                continue
            self._start_capture(pick)
        if self.focus == "browser" and not self.browser.running:
            self.focus = "list"
        return data
