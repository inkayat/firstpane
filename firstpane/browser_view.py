"""Browser tab: opens the selected agent's own browser where Settings says (firstpane/placed.py):
a new tab in the Ghostty window in front (the default) or a Herdr pane beside Firstpane. The tab itself
draws no page; it says whose browser is open where, and the keys that act on it.

Every agent context (agent identity plus its source worktree) has its own profile and remembered
address; one context's browser is open at a time. Choosing the tab opens the selected context's
browser at its remembered address (or blank), or keeps the one already open; the user types
addresses in the browser's own address field. `f` or Enter brings that browser forward, `x`
closes it. `a` (or Ctrl-G) captures the page URL, title and marked selection into the inline
message (firstpane/composer_view.py); a pick made with the browser's own select tool (toolbar
icon) arrives through its agent bridge and opens the same message with the picked element.
There is no global message field.

The in-tab page (firstpane/browser.py's Browser: embed host, frames, page keyboard and mouse)
is not opened from here; its wiring below is dormant until that path is chosen again.
"""

from firstpane.browser import who
from firstpane.composer_view import anchor_key
from firstpane.embed import CaptureError
from firstpane.icons import NO_GRAPHICS
from firstpane.placed import PLACEMENT_LABELS
from firstpane.pr_view import word_wrap
from firstpane.sanitize import sanitize
from firstpane.style import INK, MUTED, paint, rgb_fg

MOUSE_BUTTONS = ("left", "middle", "right", "none")
IMAGE_COLUMN = 2  # the page image starts in this (1-based) column of the body


class BrowserTabMixin:
    # ---- context -----------------------------------------------------------------------
    def _browser_context(self):
        """(context, "") for the selected agent's browser, else (None, why there is none)."""
        target = self.target
        if not target:
            return None, "no agent selected: select an agent on the Agents tab (1, then Enter); each agent has its own browser"
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
        address (or blank)."""
        context, why = self._browser_context()
        if context is None:
            self.status = why
            return
        status = self.placed.open(context, self.browser_placement)
        owner = self.placed.owner()
        shared = self.placed.same_address(context, context["url"]) if owner and owner["id"] == context["id"] else []
        self.status = f"! same address as {who(shared[0])}: one server for both" if shared else status

    # ---- body --------------------------------------------------------------------------
    def _browser_bar(self):
        """The last action's outcome, a kept direct pick, or a capture in progress."""
        parts = []
        if self.status and self.status != NO_GRAPHICS:
            parts.append(self.status)
        elif self._direct_pick_waiting():
            kept = sanitize(self.draft.get("body") or "").split("\n")[0][:80]
            parts.append(f"context kept: {kept}; m previews")
        if self.capture_job is not None:
            parts.append("capturing…")
        return " · ".join(sanitize(part) for part in parts)

    def _browser_body(self, plain, budget):
        lines = []
        width = max(1, self.cols - 1)
        for text, sgr in ((self._browser_bar(), INK), (sanitize(self.placed.describe()), INK),
                          (f"opens in: {PLACEMENT_LABELS[self.browser_placement]} (6 Settings) \u00b7 f brings it forward"
                           " \u00b7 a captures the page \u00b7 x closes it", MUTED)):
            lines += [(" " + paint(part, rgb_fg(sgr), plain), None) for part in (word_wrap(text, width) if text else [])]
        anchor = (self.draft or {}).get("anchor") or {}
        if self.draft is not None and anchor.get("kind") == "browser" and not anchor.get("direct"):
            lines += self._inline_rows(self.draft, plain, 1)
        return [text for text, _ in lines], [hit for _, hit in lines]

    # ---- keys --------------------------------------------------------------------------
    def _browser_list_key(self, key):
        if key in ("f", "enter"):
            context, why = self._browser_context()
            self.status = self.placed.focus(context) if context else why
        elif key in ("a", "ctrl-g"):
            self._start_capture()
        elif key == "x":
            self.status = self.placed.close()
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
        self.capture_job = self.submit(lambda: self.placed.capture(context))
        self._poll_capture()

    def _capture_context(self):
        """(the selected agent's context, None) while this tab shows its browser, else (None, why)."""
        if self.capture_job is not None:
            return None, "capture refused: the last capture is still running"
        context, why = self._browser_context()
        if context is None:
            return None, "capture refused: " + why
        self.placed.refresh()
        owner = self.placed.owner()
        if owner is None or self.placed.state != "open":
            return None, "capture refused: no open browser from this Firstpane"
        if owner["id"] != context["id"]:
            return None, (f"capture refused: {self.placed.where()} shows {sanitize(who(owner))}'s browser, "
                          f"not {sanitize(who(context))}'s")
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

    def pump(self, ready, now):
        self._settle_frames()
        self._settle_icons()
        data = self.icons.pending(self.browser.cell) + self.browser.pump(ready, now)
        self.placed.pump(ready)
        for pick in self.placed.take_picks():
            note = pick[2] if len(pick) > 2 else ""
            if note.strip() and self.screen.startswith("confirm-"):
                self.placed.answer_pick(pick[0], False)
                self.status = "context kept: a preview is already open"
                continue
            self._start_capture(pick)
        if self.focus == "browser" and not self.browser.running:
            self.focus = "list"
        return data
