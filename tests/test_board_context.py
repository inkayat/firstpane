"""Board badge, task detail and a context-bound unsent reply.

Each test names the break it catches. Expectations are literals. External
commands are the fake transport; nothing writes a backlog or a live home.
"""

import json
import os
import unittest

from firstpane import board
from firstpane.style import YELLOW
from tests.support import CmdResult, FakePi, write_meta
from tests.test_board_layout import LayoutFixture, task

YELLOW_SGR = "38;2;%d;%d;%d" % YELLOW
ASK = "Which layout should the summary use?"
LIFE = "2026-10-29T12:00:00Z#0"
LIFE_NEXT = "2026-10-29T13:00:00Z#1"


def show_text(ident="fp-ask-0001", reason=ASK, body=None, code=None):
    if code == "NOT_FOUND":
        return "code: NOT_FOUND\n"
    purpose = body if body is not None else (
        "Ship a readable summary.\\nCaptain hold set: 2026-10-29T12:00:00Z\\n"
        "Captain hold origin: board\\nThe purpose is a readable summary."
    )
    return (
        f"id: {ident}\n"
        f'title: "Project status summary"\n'
        f'body: "{purpose}"\n'
        f'hold_reason: "{reason}"\n'
        "hold_kind: captain\n"
        "state: held\n"
        'links: "notes https://github.com/inkayat/firstpane/pull/12"\n'
    )


class PredicateTests(unittest.TestCase):
    def test_pending_ids_count_each_firstmate_captain_call_once(self):
        # Break: a repeated id, a manual row, or a Waiting Review row without
        # captain_review changes the badge count.
        fn = getattr(board, "pending_request_ids", None)
        self.assertTrue(callable(fn), "pending_request_ids missing")
        rows = [
            {"id": "a", "source": "firstmate", "captain_review": True},
            {"id": "a", "source": "firstmate", "captain_review": True},
            {"id": "b", "source": "firstmate", "captain_review": True, "state": "todo"},
            {"id": "c", "source": "firstmate", "captain_review": False, "state": "waiting_review"},
            {"id": "d", "source": "manual", "captain_review": True, "state": "waiting_review"},
        ]
        self.assertEqual(fn(rows), ["a", "b"])

    def test_the_agents_warning_counts_linked_waiting_agents_not_tasks_or_names(self):
        # Break: two tasks on one agent count twice, two same-named agents count once,
        # an idle or unlinked ask invents a row, or a blocked agent with no user ask counts.
        fn = getattr(board, "waiting_agent_ids", None)
        self.assertTrue(callable(fn), "waiting_agent_ids missing")
        tasks = [
            {"id": "ask-1", "source": "firstmate", "captain_review": True},
            {"id": "ask-2", "source": "firstmate", "captain_review": True},
            {"id": "ask-3", "source": "firstmate", "captain_review": True},
            {"id": "idle-ask", "source": "firstmate", "captain_review": True},
            {"id": "orphan", "source": "firstmate", "captain_review": True},
            {"id": "amb-1", "source": "firstmate", "captain_review": True},
            {"id": "pause", "source": "firstmate", "captain_review": False},
        ]
        agents = [
            {"pane_id": "pane-a", "task_id": "ask-1", "status": "blocked", "title": "pi"},
            {"pane_id": "pane-a", "task_id": "ask-1", "status": "blocked", "title": "pi again"},
            {"pane_id": "pane-b", "task_id": "ask-3", "status": "blocked", "title": "pi"},
            {"pane_id": "pane-idle", "task_id": "idle-ask", "status": "idle", "title": "pi"},
            {"pane_id": "pane-done", "task_id": "ask-1", "status": "done", "title": "finished"},
            {"pane_id": "pane-work", "task_id": "ask-1", "status": "working", "title": "busy"},
            {"pane_id": "pane-blocked", "task_id": "pause", "status": "blocked", "title": "held"},
            {"pane_id": "pane-amb", "task_id": "amb-1", "status": "blocked", "title": "split"},
            {"pane_id": "pane-guess", "task_id": None, "status": "blocked", "title": "orphan"},
        ]
        metas = [
            {"task_id": "ask-1", "herdr_pane_id": "pane-a"},
            {"task_id": "ask-3", "herdr_pane_id": "pane-b"},
            {"task_id": "idle-ask", "herdr_pane_id": "pane-idle"},
            {"task_id": "orphan", "herdr_pane_id": "pane-missing"},
            {"task_id": "amb-1", "herdr_pane_id": "pane-amb"},
            {"task_id": "amb-2", "herdr_pane_id": "pane-amb"},
            {"task_id": "pause", "herdr_pane_id": "pane-blocked"},
        ]
        self.assertEqual(fn(tasks, agents, metas, {}), ["pane-a", "pane-b"])

    def test_a_decision_key_links_the_agent_to_the_other_open_call(self):
        # Break: only the worker's own id counts, an answered key counts, two open
        # calls count as two agents, or an unrelated call is chosen.
        fn = getattr(board, "waiting_agent_ids", None)
        links = getattr(board, "explicit_request_ids", None)
        self.assertTrue(callable(fn) and callable(links), "decision link missing")
        tasks = [
            {"id": "alter-050-review-package", "source": "firstmate", "captain_review": True},
            {"id": "alter-050-other-call", "source": "firstmate", "captain_review": True},
            {"id": "answered-call", "source": "firstmate", "captain_review": False},
            {"id": "unrelated", "source": "firstmate", "captain_review": True},
        ]
        meta = {
            "task_id": "alter-050-team-lead",
            "herdr_pane_id": "pane-a",
            "decision_keys": ["answered-call", "alter-050-review-package"],
        }
        agents = [{"pane_id": "pane-a", "task_id": "alter-050-team-lead", "status": "blocked"}]
        mine = {"alter-050-review-package": "alter-050-team-lead", "alter-050-other-call": None}
        self.assertEqual(fn(tasks, agents, [meta], mine), ["pane-a"])
        self.assertEqual(links(meta, tasks, mine), ["alter-050-review-package"])
        self.assertEqual(links(meta, tasks, {"alter-050-review-package": "someone-else"}), [])
        self.assertEqual(links(meta, tasks, {}), [])
        self.assertEqual(fn(tasks, agents, [meta], {"alter-050-review-package": "someone-else"}), [])
        self.assertEqual(fn(tasks, agents, [meta], {}), [])
        two = dict(meta, decision_keys=["alter-050-review-package", "alter-050-other-call"])
        self.assertEqual(fn(tasks, agents, [two], mine), ["pane-a"])
        self.assertEqual(links(two, tasks, mine), ["alter-050-review-package", "alter-050-other-call"])
        other = {"alter-050-review-package": "someone-else", "alter-050-other-call": None}
        self.assertEqual(links(two, tasks, other), ["alter-050-other-call"])
        self.assertEqual(fn(tasks, agents, [two], other), ["pane-a"])

    def test_identity_and_show_parse_the_producer_grammar(self):
        # Break: a lifecycle line or a quoted backlog field is guessed instead of parsed.
        parse_id = getattr(board, "parse_identity", None)
        parse_show = getattr(board, "parse_backlog_show", None)
        self.assertTrue(callable(parse_id) and callable(parse_show), "parsers missing")
        self.assertEqual(parse_id(LIFE + "\n"), LIFE)
        self.assertEqual(parse_id("#3\n"), "#3")
        self.assertIsNone(parse_id("not a lifecycle\n"))
        parsed = parse_show(show_text(), "fp-ask-0001")
        self.assertEqual(parsed["record"]["hold_reason"], ASK)
        self.assertIn("The purpose is a readable summary.", parsed["record"]["body"])
        self.assertFalse(parse_show(show_text(code="NOT_FOUND"), "fp-ask-0001")["found"])

    def test_a_zero_exit_without_a_summary_is_not_fresh(self):
        # Break: fm-board exiting 0 is treated as a current count.
        fresh = getattr(board, "list_is_fresh", None)
        self.assertTrue(callable(fresh), "list_is_fresh missing")
        self.assertFalse(fresh(exit_ok=True, reconcile_skipped=False, summary_present=False, loaded_at=0, now=1))
        self.assertFalse(fresh(exit_ok=True, reconcile_skipped=True, summary_present=True, loaded_at=0, now=1))
        self.assertFalse(fresh(exit_ok=True, reconcile_skipped=False, summary_present=True, loaded_at=0, now=181))
        self.assertTrue(fresh(exit_ok=True, reconcile_skipped=False, summary_present=True, loaded_at=0, now=180))


class BadgeFixture(LayoutFixture):
    def tab(self, app):
        return app.render().splitlines()[0]

    def plain_tab(self, app):
        from tests.support import strip_sgr
        return strip_sgr(self.tab(app))

    def lists(self):
        return [call for call in self.transport.calls if call["argv"][-2:] == ["list", "--json"]]

    def named(self, script, sub):
        return [
            call for call in self.transport.calls
            if os.path.basename(call["argv"][0]) == script and len(call["argv"]) > 1 and call["argv"][1] == sub
        ]

    def reload_board(self, app):
        app.poll()
        app._refresh_board(force=True)
        app.poll()

    def bind_waiting(self, app, ident="fp-ask-0001", pane_id="pane-a"):
        write_meta(
            self.home, ident, project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id=pane_id,
            endpoint_task_id=ident,
        )
        app.session = "lab"
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [{
            "kind": "agent", "pane_id": pane_id, "task_id": ident, "status": "blocked",
            "title": "pi", "key": ("agent", "firstpane", "t1"), "project_key": "firstpane",
        }]}]
        self.reload_board(app)


class BadgeTests(BadgeFixture):
    def test_no_captain_call_draws_board_without_a_warning(self):
        # Break: an inventory-covered empty list still paints a warning or a question mark.
        for item in self.tasks:
            item["captain_review"] = False
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "reconcile coverage: inventory"),
        ))
        app = self.board()
        label = self.plain_tab(app)
        self.assertIn("Board", label)
        self.assertNotIn("\u26a0", label)
        self.assertNotIn("Board ?", label)

    def test_two_firstmate_calls_badge_the_unfiltered_tab_once_each(self):
        # Break: the badge counts a repeated id, follows a project filter, or omits the warning.
        self.tasks[:] = [
            task("fp-ask-0001", "Summary", "waiting_review", "dotfiles", review=True),
            task("fp-ask-0002", "Other ask", "todo", "alter", review=True),
            task("fp-ask-0001", "Summary duplicate", "waiting_review", "dotfiles", review=True),
            task("fp-wait-0003", "Just waiting", "waiting_review", "dotfiles", review=False),
        ]
        app = self.board()
        label = self.plain_tab(app)
        self.assertNotIn("\u26a0", label)
        self.assertIn("Agents ?", label)
        self.assertNotIn("Board ?", label)
        self.assertIn("2 awaiting your reply", self.text(app))
        app.handle_key("p")
        self.assertNotIn("\u26a0", self.plain_tab(app))
        self.assertIn("2 awaiting your reply", self.text(app))

    def test_manual_waiting_review_is_not_a_captain_call(self):
        # Break: a manual row with captain_review, or Waiting Review alone, paints the badge or the card.
        self.tasks[:] = [
            task("fp-wait-0003", "Just waiting", "waiting_review", review=False),
            {
                "id": "fp-hand-0004", "project": "notes", "title": "Hand filed",
                "state": "waiting_review", "agent": None, "kind": "ship", "source": "manual",
                "since": None, "running": None, "captain_review": True,
                "updated_at": "2026-10-26T00:00:00Z",
            },
        ]
        app = self.board()
        self.assertNotIn("\u26a0", self.plain_tab(app))
        self.assertNotIn("Awaiting reply", self.text(app))

    def test_unknown_and_stale_lists_are_not_a_clear_or_urgent_zero(self):
        # Break: a failed, skipped or summary-less list looks like a current zero or an urgent call.
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(1, "", "list failed"),
        ))
        failed = self.board()
        self.assertIn("Agents ?", self.plain_tab(failed))
        self.assertNotIn("Board ?", self.plain_tab(failed))
        self.assertNotIn("\u26a0", self.plain_tab(failed))
        self.assertNotIn(YELLOW_SGR, self.tab(failed))

        self.transport.handlers.pop(0)
        self.tasks[:] = [
            task("fp-ask-0001", "Summary", "waiting_review", review=True),
            task("fp-ask-0002", "Other ask", "todo", "alter", review=True),
        ]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile skipped (fallback)"),
        ))
        skipped = self.board()
        self.assertIn("Agents ?", self.plain_tab(skipped))
        self.assertNotIn("\u26a0", self.plain_tab(skipped))
        self.assertNotIn(YELLOW_SGR, self.tab(skipped))

        self.transport.handlers.pop(0)
        (self.home / "state" / "home-summary.json").unlink()
        missing = self.board()
        self.assertIn("?", self.plain_tab(missing))
        self.assertNotIn(YELLOW_SGR, self.tab(missing))

    def test_the_warning_sits_on_agents_and_counts_one_linked_waiting_agent(self):
        # Break: the warning stays on Board, counts both asks, or counts an idle agent with the same name.
        self.tasks[:] = [
            task("fp-ask-0001", "Summary", "waiting_review", review=True),
            task("fp-ask-0002", "Other ask", "todo", review=True),
        ]
        write_meta(
            self.home, "fp-ask-0001", project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="pane-a",
            endpoint_task_id="fp-ask-0001",
        )
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))
        app = self.board()
        app.session = "lab"
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [
            {"kind": "agent", "pane_id": "pane-a", "task_id": "fp-ask-0001", "status": "blocked", "title": "pi", "key": ("agent", "firstpane", "t1")},
            {"kind": "agent", "pane_id": "pane-idle", "task_id": None, "status": "idle", "title": "pi", "key": ("agent", "firstpane", "t2")},
        ]}]
        label = self.plain_tab(app)
        self.assertIn("Agents \u26a0 1", label)
        self.assertNotIn("Agents \u26a0 1?", label)
        self.assertNotIn("Board \u26a0", label)
        self.assertNotIn("Board ?", label)
        self.assertIn(YELLOW_SGR, self.tab(app))


class AgentsEntryTests(BadgeFixture):
    def waiting_row(self, ident="fp-ask-0001", pane_id="pane-a", status="blocked", title="pi"):
        return {
            "kind": "agent", "pane_id": pane_id, "task_id": ident, "status": status, "title": title,
            "key": ("agent", "firstpane", pane_id), "project_key": "firstpane", "text": title,
        }

    def test_enter_opens_the_decision_key_call_not_an_unrelated_task(self):
        # Break: Enter opens the worker id, an answered key, or a different open call.
        worker = "alter-050-team-lead"
        held = "alter-050-review-package"
        question = "Which package should land?"
        self.tasks[:] = [
            task(held, "Review package", "waiting_review", review=True),
            task("unrelated", "Unrelated", "todo", review=True),
        ]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv,
            lambda call: CmdResult(0, show_text(
                held, reason=question,
                body="The package is ready.\\nCaptain hold origin: " + worker,
            )),
        ))
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "open",
            lambda call: CmdResult(0, LIFE + "\n"),
        ))
        write_meta(
            self.home, worker, project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="pane-a",
            endpoint_task_id=worker, decision_keys="answered-call," + held,
        )
        app = self.app()
        app.session = "lab"
        row = self.waiting_row(ident=worker)
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [row]}]
        app.selection_key = row["key"]
        app.board_selected = "unrelated"
        app.handle_key("enter")
        app.render()
        self.assertEqual(app.board_selected, held)
        self.assertIn(question, self.text(app))
        self.assertNotIn("Unrelated", self.text(app))
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))
        app.handle_key("tab")
        request = next(item for item in app.draft["attachments"] if item.get("kind") == "board-request")
        self.assertEqual(request["id"], held)
        self.assertEqual(request["lifecycle"], LIFE)
        self.assertNotEqual(request["id"], worker)

    def test_a_different_hold_origin_does_not_open(self):
        # Break: a decision key whose recorded origin names another task is opened.
        worker = "alter-050-team-lead"
        held = "alter-050-review-package"
        self.tasks[:] = [task(held, "Review package", "waiting_review", review=True)]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv,
            lambda call: CmdResult(0, show_text(
                held, body="Ready.\\nCaptain hold origin: someone-else",
            )),
        ))
        write_meta(
            self.home, worker, project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="pane-a",
            endpoint_task_id=worker, decision_keys=held,
        )
        app = self.app()
        app.session = "lab"
        row = self.waiting_row(ident=worker)
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [row]}]
        app.selection_key = row["key"]
        app.handle_key("enter")
        self.assertEqual(app.tab, "agents")
        self.assertEqual(app.status, "no linked Board task")

    def linked_app(self, keys, origins, ids, failing=()):
        worker = "alter-050-team-lead"
        self.tasks[:] = [task(ident, "Call " + ident, "waiting_review", review=True) for ident in ids]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))

        def show(call):
            ident = call["argv"][2]
            if ident in failing:
                return CmdResult(1, "", "boom")
            origin = origins.get(ident)
            body = "Ready." + ("\\nCaptain hold origin: " + origin if origin else "")
            return CmdResult(0, show_text(ident, body=body))

        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv, show,
        ))
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "open",
            lambda call: CmdResult(0, LIFE + "\n"),
        ))
        write_meta(
            self.home, worker, project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="pane-a",
            endpoint_task_id=worker, decision_keys=",".join(keys),
        )
        app = self.app()
        app.handle_key("1")
        app.session = "lab"
        row = self.waiting_row(ident=worker)
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [row]}]
        app.selection_key = row["key"]
        self.reload_board(app)
        return app

    def test_a_foreign_origin_key_is_not_a_candidate_for_the_count_or_for_enter(self):
        # Break: another worker's call still counts as a second candidate, so Enter refuses a call that is really linked.
        worker = "alter-050-team-lead"
        for ids, keys, origins, want, counted in (
            ([worker, "call-a"], ["call-a"], {"call-a": "someone-else"}, worker, True),
            (["call-a", "call-b"], ["call-a", "call-b"], {"call-a": "someone-else", "call-b": worker}, "call-b", True),
            (["call-a", "call-b"], ["call-a", "call-b"], {"call-b": worker}, None, True),
            (["call-a"], ["call-a"], {"call-a": "someone-else"}, None, False),
            ([worker, "call-a"], ["call-a"], {"call-a": worker}, None, True),
            (["call-a", "call-b"], ["call-a", "call-b"], {"call-a": worker, "call-b": None}, None, True),
        ):
            with self.subTest(ids=ids, keys=keys, origins=origins):
                self.transport.handlers.clear()
                app = self.linked_app(keys, origins, ids)
                self.assertEqual("Agents \u26a0 1" in self.plain_tab(app), counted)
                app.handle_key("enter")
                if want is None:
                    self.assertEqual(app.tab, "agents")
                    self.assertEqual(app.status, "no linked Board task")
                else:
                    self.assertEqual(app.tab, "board")
                    self.assertEqual(app.board_selected, want)

    def test_enter_waits_while_any_decision_key_of_the_agent_is_unread(self):
        # Break: a verified call is opened while another of the agent's open calls is unread and may make it ambiguous.
        worker = "alter-050-team-lead"
        for ids, keys, origins in (
            ([worker, "call-a"], ["call-a"], {"call-a": worker}),
            (["call-a", "call-b"], ["call-a", "call-b"], {"call-a": worker, "call-b": worker}),
        ):
            with self.subTest(ids=ids):
                self.transport.handlers.clear()
                app = self.linked_app(keys, origins, ids, failing=(keys[-1],))
                app.handle_key("enter")
                self.assertEqual(app.tab, "agents")
                self.assertEqual(app.status, "still reading Board links")
                self.assertIn("Agents \u26a0 1?", self.plain_tab(app))
                self.transport.handlers.insert(0, (
                    lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv,
                    lambda call: CmdResult(0, show_text(call["argv"][2], body="Ready.\\nCaptain hold origin: " + worker)),
                ))
                self.reload_board(app)
                app.handle_key("enter")
                self.assertEqual(app.tab, "agents")
                self.assertEqual(app.status, "no linked Board task")

    def test_an_unreadable_origin_is_not_a_link_until_a_later_read_succeeds(self):
        # Break: a failed show is treated as an unrecorded origin and the call is counted or opened unverified.
        app = self.linked_app(["call-a"], {}, ["call-a"], failing=("call-a",))
        self.assertNotIn("\u26a0", self.plain_tab(app))
        self.assertIn("Agents ?", self.plain_tab(app))
        app.handle_key("enter")
        self.assertEqual(app.status, "still reading Board links")
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv,
            lambda call: CmdResult(0, show_text("call-a", body="Ready.")),
        ))
        self.reload_board(app)
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))
        app.handle_key("enter")
        self.assertEqual(app.board_selected, "call-a")

    def test_origins_are_read_off_the_ui_path_and_reused_until_the_task_changes(self):
        # Break: every frame or Enter shells out to show, or a changed hold keeps its old origin.
        app = self.linked_app(["call-a"], {"call-a": "alter-050-team-lead"}, ["call-a"])
        reads = lambda: len(self.named("fm-tasks-axi.sh", "show"))
        self.assertEqual(reads(), 1)
        for _ in range(3):
            app.render()
        self.assertEqual(reads(), 1)
        app.handle_key("enter")
        app.render()
        opened = reads()
        app.handle_key("esc")
        self.reload_board(app)
        self.assertEqual(reads(), opened)
        self.tasks[0]["updated_at"] = "2026-10-29T09:00:00Z"
        self.reload_board(app)
        self.assertEqual(reads(), opened + 1)

    def deferred_app(self, jobs):
        from concurrent.futures import Future
        from firstpane.app import App
        from tests.support import FakeTerminal

        def submit(fn):
            future = Future()
            jobs.append((fn, future))
            return future

        app = App(self.config, self.transport, self.state, self.env, self.clock, FakeTerminal(),
                  rows=32, cols=120, submit=submit, wall=lambda: 1_700_000_100)
        return app

    def run_job(self, jobs, index):
        fn, future = jobs[index]
        future.set_result(fn())

    def test_a_blocked_origin_read_neither_delays_the_board_rows_nor_certifies_a_clear_zero(self):
        # Break: the origin reads run inside the list job, or an unread origin lets the badge say nothing is waiting.
        worker = "alter-050-team-lead"
        self.tasks[:] = [task("call-a", "Call a", "waiting_review", review=True)]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv,
            lambda call: CmdResult(0, show_text("call-a", body="Ready.\\nCaptain hold origin: " + worker)),
        ))
        write_meta(
            self.home, worker, project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="pane-a",
            endpoint_task_id=worker, decision_keys="call-a",
        )
        jobs = []
        app = self.deferred_app(jobs)
        app.refresh(manual=True)
        app.handle_key("1")
        row = self.waiting_row(ident=worker)
        app.session = "lab"
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [row]}]
        app.selection_key = row["key"]
        self.assertEqual(len(jobs), 1)
        self.run_job(jobs, 0)
        app.poll()
        self.assertEqual(self.named("fm-tasks-axi.sh", "show"), [])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(app.board_tasks[0]["id"], "call-a")
        self.assertTrue(app.board_have_list)
        self.assertIn("Agents ?", self.plain_tab(app))
        self.assertNotIn("\u26a0", self.plain_tab(app))
        app.handle_key("enter")
        self.assertEqual(app.tab, "agents")
        self.assertEqual(app.status, "still reading Board links")
        self.run_job(jobs, 1)
        app.poll()
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))
        app.handle_key("enter")
        self.assertEqual(app.board_selected, "call-a")

    def test_a_read_that_lands_after_a_newer_list_is_dropped(self):
        # Break: an origin read for an older list certifies a link in the newer one.
        worker = "alter-050-team-lead"
        self.tasks[:] = [task("call-a", "Call a", "waiting_review", review=True)]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))
        origin = {"value": "someone-else"}
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv,
            lambda call: CmdResult(0, show_text("call-a", body="Ready.\\nCaptain hold origin: " + origin["value"])),
        ))
        write_meta(
            self.home, worker, project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="pane-a",
            endpoint_task_id=worker, decision_keys="call-a",
        )
        jobs = []
        app = self.deferred_app(jobs)
        app.refresh(manual=True)
        row = self.waiting_row(ident=worker)
        app.session = "lab"
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [row]}]
        self.run_job(jobs, 0)
        app.poll()
        stale_origin_job = len(jobs) - 1
        self.tasks[0]["updated_at"] = "2026-10-29T09:00:00Z"
        origin["value"] = worker
        app._refresh_board(force=True)
        self.run_job(jobs, len(jobs) - 1)
        app.poll()
        self.run_job(jobs, stale_origin_job)
        app.poll()
        self.assertEqual(app.board_origins, {})
        self.assertEqual(app.board_origin_unread, ["call-a"])
        self.run_job(jobs, len(jobs) - 1)
        app.poll()
        self.assertEqual(app.board_origins, {"call-a": worker})
        self.assertEqual(app.board_origin_unread, [])

    def test_enter_on_a_waiting_agent_opens_its_linked_task_and_esc_returns(self):
        # Break: Enter stays on Agents, opens a different task, clears the warning, or Esc lands in Board lanes.
        self.tasks[:] = [
            task("fp-ask-0001", "Summary", "waiting_review", review=True),
            task("fp-other", "Unrelated", "todo", review=True),
        ]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "show" and "--full" in argv,
            lambda call: CmdResult(0, show_text("fp-ask-0001")),
        ))
        self.transport.handlers.insert(0, (
            lambda argv: len(argv) > 1 and argv[1] == "open",
            lambda call: CmdResult(0, LIFE + "\n"),
        ))
        app = self.app()
        self.bind_waiting(app)
        row = app.projects[0]["agents"][0]
        app.selection_key = row["key"]
        app.board_selected = "fp-other"
        app.board_column = "todo"
        app.board_project = "alter"
        saved_tabs = set(app.hidden_tabs)
        app.handle_key("enter")
        app.render()
        shown = self.text(app)
        self.assertEqual(app.tab, "board")
        self.assertEqual(app.board_view, "detail")
        self.assertEqual(app.board_selected, "fp-ask-0001")
        self.assertIn(ASK, shown)
        self.assertNotIn("Unrelated", shown)
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))
        self.assertEqual(app.hidden_tabs, saved_tabs)
        app.handle_key("esc")
        self.assertEqual(app.tab, "agents")
        self.assertEqual(app.selection_key, row["key"])
        self.assertEqual(app.board_view, "lanes")
        self.assertEqual(app.board_selected, "fp-other")
        self.assertEqual(app.board_column, "todo")
        self.assertEqual(app.board_project, "alter")
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))

    def test_enter_on_a_waiting_agent_without_a_link_stays_and_says_so(self):
        # Break: Enter invents a task, or a blocked agent with no user ask is described as one.
        self.tasks[:] = [task("fp-other", "Unrelated", "todo", review=True)]
        app = self.app()
        row = self.waiting_row(ident=None, pane_id="pane-blocked")
        row["task_id"] = None
        app.projects = [{"key": "firstpane", "label": "firstpane", "agents": [row]}]
        app.selection_key = row["key"]
        app.render()
        app.handle_key("enter")
        self.assertEqual(app.tab, "agents")
        self.assertEqual(app.board_view, "lanes")
        self.assertIn("no linked Board task", app.status)
        self.assertNotIn("\u26a0", self.plain_tab(app))

    def test_search_enter_and_arrow_keys_do_not_open_board(self):
        # Break: Enter in the search field, or moving between agents, opens a Board task.
        app = self.app()
        self.bind_waiting(app)
        row = app.projects[0]["agents"][0]
        app.selection_key = row["key"]
        app.focus = "search"
        app.handle_key("enter")
        self.assertEqual(app.tab, "agents")
        self.assertEqual(app.focus, "list")
        self.assertEqual(app.board_view, "lanes")
        app.handle_key("j")
        self.assertEqual(app.tab, "agents")
        self.assertEqual(app.board_view, "lanes")


class DetailFixture(BadgeFixture):
    def install_reads(self, ident="fp-ask-0001", identity=LIFE, open_code=0, timed_out=False, show=None):
        text = show if show is not None else show_text(ident)
        self.identities = [identity]

        def show_pred(argv):
            return len(argv) > 1 and argv[1] == "show" and "--full" in argv

        def open_pred(argv):
            return len(argv) > 1 and argv[1] == "open"

        def open_fn(call):
            if timed_out:
                return CmdResult(124, "", "timed out", timed_out=True)
            code = open_code() if callable(open_code) else open_code
            if code != 0:
                return CmdResult(code, "", "open failed")
            life = self.identities.pop(0) if len(self.identities) > 1 else self.identities[0]
            return CmdResult(0, life + "\n", "")

        self.transport.handlers.insert(0, (open_pred, open_fn))
        self.transport.handlers.insert(0, (show_pred, lambda call: CmdResult(0, text, "")))

    def open_detail(self, app):
        app.handle_key("enter")
        app.render()
        return self.text(app)


class DetailTests(DetailFixture):
    def test_enter_opens_every_reachable_task_and_esc_restores_the_lane_place(self):
        # Break: Enter stays on the lanes, or Esc clears the search, filter, column or scroll.
        self.install_reads("fp-side-ef56", show=show_text("fp-side-ef56"))
        app = self.board()
        app.render()
        app.board_search.set("panel")
        app.board_project = "dotfiles"
        app.handle_key("right")
        self.assertEqual((app.board_column, app.board_selected), ("in_progress", "fp-side-ef56"))
        app.render()
        before_scroll = dict(app.board_scroll)
        place = (app.board_project, app.board_search.text, app.board_column, app.board_selected, app.board_active, app.focus)
        frame = self.open_detail(app)
        self.assertNotIn("TODO", frame.splitlines()[2])
        self.assertIn("What this is", frame)
        app.handle_key("down")
        app.handle_key("esc")
        restored = self.text(app)
        self.assertIn("TODO", restored)
        self.assertEqual(
            (app.board_project, app.board_search.text, app.board_column, app.board_selected, app.board_active, app.focus),
            place,
        )
        self.assertEqual(app.board_scroll, before_scroll)
        self.assertEqual(app.board_view, "lanes")

    def test_collapsed_done_enter_still_toggles_and_an_opened_done_row_opens_detail(self):
        # Break: Enter on collapsed Done opens a detail, or an opened Done row cannot be opened.
        self.install_reads("fp-old-0001", show=show_text("fp-old-0001", code="NOT_FOUND"))
        app = self.board()
        for _ in range(3):
            app.handle_key("right")
        self.assertEqual(app.board_column, "done")
        self.assertIsNone(app.board_selected)
        app.handle_key("enter")
        self.assertTrue(app.board_done_open)
        self.assertEqual(getattr(app, "board_view", "lanes"), "lanes")
        app.handle_key("down")
        self.assertEqual(app.board_selected, "fp-old-0001")
        frame = self.open_detail(app)
        self.assertIn("no longer in backlog", frame)

    def test_detail_shows_the_verbatim_question_and_no_invented_options(self):
        # Break: the needs block paraphrases the hold, or prose becomes Approve / Request changes controls.
        self.tasks[:] = [task("fp-ask-0001", "Summary", "waiting_review", review=True)]
        reason = "approve or request changes?"
        self.install_reads(show=show_text(reason=reason))
        app = self.board()
        frame = self.open_detail(app)
        self.assertIn("What FirstMate needs from you", frame)
        self.assertIn(reason, frame)
        self.assertIn("set 2026-10-29T12:00:00Z", frame)
        self.assertIn("The purpose is a readable summary.", frame)
        self.assertNotIn("Captain hold set:", frame)
        self.assertNotIn("[ ] Approve", frame)
        self.assertNotIn("( ) Request changes", frame)
        self.assertNotIn("Request changes", frame.replace(reason, ""))
        self.assertIn("https://github.com/inkayat/firstpane/pull/12", frame)

    def test_a_manual_row_has_no_request_block(self):
        # Break: a hand-filed row is presented as FirstMate's open request.
        self.tasks[:] = [{
            "id": "fp-hand-0004", "project": "notes", "title": "Hand filed",
            "state": "waiting_review", "agent": None, "kind": "ship", "source": "manual",
            "since": None, "running": None, "captain_review": True,
            "updated_at": "2026-10-26T00:00:00Z",
        }]
        self.install_reads("fp-hand-0004", show=show_text("fp-hand-0004"))
        app = self.board()
        frame = self.open_detail(app)
        self.assertIn("Manual board row: FirstMate does not track it", frame)
        self.assertNotIn("What FirstMate needs from you", frame)

    def test_lanes_do_not_read_show_or_open_until_detail_and_not_again_on_redraw(self):
        # Break: every card or every repaint starts a show or open subprocess.
        self.tasks[:] = [task("fp-ask-0001", "Summary", "waiting_review", review=True)]
        self.install_reads()
        app = self.board()
        for _ in range(10):
            app.render()
        self.assertEqual(self.named("fm-tasks-axi.sh", "show"), [])
        self.assertEqual(self.named("fm-captain-hold.sh", "open"), [])
        self.open_detail(app)
        self.assertEqual(len(self.named("fm-tasks-axi.sh", "show")), 1)
        self.assertEqual(len(self.named("fm-captain-hold.sh", "open")), 1)
        self.assertEqual(self.named("fm-tasks-axi.sh", "show")[0]["env"]["FM_HOME"], str(self.home))
        self.assertEqual(self.named("fm-tasks-axi.sh", "show")[0]["env"]["FIRSTMATE_ROOT"], str(self.tool))
        for _ in range(5):
            app.render()
        self.assertEqual(len(self.named("fm-tasks-axi.sh", "show")), 1)
        self.tasks[0]["updated_at"] = "2026-10-27T00:00:00Z"
        app.handle_key("r")
        app.render()
        self.assertEqual(len(self.named("fm-tasks-axi.sh", "show")), 2)

    def test_refresh_follows_a_moved_id_and_detail_keeps_a_gone_snapshot(self):
        # Break: a refresh drops the selection, or a disappeared detail pretends the task is still listed.
        self.tasks[:] = [
            task("fp-ask-0001", "Summary", "waiting_review", review=True),
            task("fp-ask-0002", "Neighbor", "waiting_review", "alter"),
        ]
        self.install_reads()
        app = self.board()
        app.render()
        self.assertEqual(app.board_selected, "fp-ask-0001")
        self.tasks[0]["state"] = "done"
        app.handle_key("r")
        app.render()
        self.assertEqual(app.board_selected, "fp-ask-0001")
        self.assertEqual(app.board_column, "done")
        self.assertTrue(app.board_done_open)
        app.handle_key("enter")
        app.render()
        self.tasks[:] = [task("fp-ask-0002", "Neighbor", "waiting_review", "alter")]
        app.handle_key("r")
        gone = self.text(app)
        self.assertIn("No longer on the board", gone)
        self.assertEqual(app.board_view, "detail")
        app.handle_key("esc")
        self.assertEqual(app.board_selected, "fp-ask-0002")

    def test_hostile_detail_text_cannot_reach_the_terminal(self):
        # Break: an OSC or CSI in the hold, body, status line or PR URL is drawn raw.
        self.tasks[:] = [task("fp-ask-0001", "Summary", "waiting_review", review=True)]
        hostile = "layout\x1b]0;owned\x07?"
        body = "purpose\x1b[31mred"
        show = (
            "id: fp-ask-0001\n"
            'title: "Summary"\n'
            'body: "purpose\x1b[31mred"\n'
            'hold_reason: "layout\x1b]0;owned\x07?"\n'
            "hold_kind: captain\n"
            'links: "https://github.com/inkayat/firstpane/pull/9\x1b]0;x\x07"\n'
        )
        self.install_reads(show=show)
        (self.home / "state" / "fp-ask-0001.status").write_text("working [at=1700000000]: edit\x1b[2J\n")
        app = self.board()
        frame = app.render() if False else None
        app.handle_key("enter")
        frame = app.render()
        self.assertNotIn("\x1b]", frame)
        self.assertNotIn("\x1b[31m", frame)
        self.assertNotIn("\x1b[2J", frame)
        self.assertNotIn("owned", frame)
        text = self.text(app)
        self.assertIn("layout?", text)
        self.assertIn("purposered", text)
        self.assertIn("edit", text)
        self.assertIn("history", text)


class ReplyTests(DetailFixture):
    def ready(self, identity=LIFE, open_code=0, timed_out=False, reason=ASK):
        self.tasks[:] = [task("fp-ask-0001", "Summary", "waiting_review", "dotfiles", review=True)]
        (self.home / "data" / "board" / "state.json").write_text(json.dumps({
            "schema": "fm-board.v1",
            "tasks": {"fp-ask-0001": {"hold_reason": reason}},
        }))
        self.install_reads(identity=identity, open_code=open_code, timed_out=timed_out, show=show_text(reason=reason))
        self.transport.snapshot["panes"] = [
            row for row in self.transport.snapshot["panes"] if row.get("pane_id") != "w5:p1"
        ]
        self.transport.pane_get.pop("w5:p1", None)
        self.primary = FakePi(self.transport, self.home)
        app = self.board()
        self.open_detail(app)
        app.handle_key("tab")
        return app

    def type_send(self, app, text="use the lanes"):
        for char in text:
            app.handle_key(char)
        app.handle_key("enter")
        return app

    def test_a_transferred_reply_leaves_the_badge_until_a_later_list(self):
        # Break: pasting into FirstMate, or opening the task, clears the warning before the record changes.
        app = self.ready()
        self.bind_waiting(app)
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))
        before = self.plain_tab(app)
        self.type_send(app)
        self.assertEqual(app.screen, "confirm-send")
        self.assertEqual(self.primary.received, [])
        app.handle_key("y")
        self.assertEqual(len(self.primary.received), 1)
        self.assertEqual(self.primary.submitted, [])
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))
        self.assertEqual(self.plain_tab(app), before)
        note = self.text(app)
        self.assertIn("Press Enter there", note)
        self.assertIn("request stays open", note)
        self.tasks[0]["captain_review"] = False
        app.handle_key("r")
        self.assertNotIn("\u26a0", self.plain_tab(app))

    def test_viewing_drafting_and_cancelling_do_not_resolve_or_write(self):
        # Break: Enter, typing, preview or cancel records an answer or drops the count.
        app = self.ready()
        count = self.plain_tab(app)
        app.handle_key("esc")
        app.handle_key("enter")
        app.render()
        app.handle_key("tab")
        self.type_send(app, "still thinking")
        app.handle_key("n")
        self.assertEqual(app.composer.text, "still thinking")
        self.assertEqual(self.primary.received, [])
        self.assertEqual(self.plain_tab(app), count)
        self.assertEqual(self.writers(), [])

    def test_a_changed_lifecycle_is_refused_and_b_rebinds(self):
        # Break: a stale approval is pasted, or a new lifecycle is bound without an explicit key.
        self.tasks[:] = [task("fp-ask-0001", "Summary", "waiting_review", review=True)]
        self.install_reads()
        self.identities = [LIFE, LIFE_NEXT, LIFE_NEXT, LIFE_NEXT]
        self.primary = FakePi(self.transport, self.home)
        app = self.board()
        self.open_detail(app)
        app.handle_key("tab")
        self.type_send(app, "approve the lanes")
        self.assertEqual(app.screen, "tree")
        self.assertIn("request changed", app.status)
        self.assertEqual(self.primary.received, [])
        self.assertEqual(app.composer.text, "approve the lanes")
        app.handle_key("ctrl-y")
        self.assertEqual(app.screen, "confirm-send")
        self.assertIn("lifecycle " + LIFE_NEXT, app.pending["body"])
        self.assertEqual(self.primary.received, [])

    def request_kinds(self, app):
        return [item.get("kind") for item in app.draft["attachments"]]

    def test_letters_after_a_refusal_are_text_and_never_rebind_or_drop_the_request(self):
        # Break: typing b or p after a refusal rebinds the request or strips it and arms a send.
        state = {"n": 0}

        def open_code():
            state["n"] += 1
            return 0 if state["n"] == 1 else 1

        app = self.ready(open_code=open_code)
        self.type_send(app, "pls")
        self.assertIn("resolved or released", app.status)
        for char in " b p please":
            app.handle_key(char)
        self.assertEqual(app.screen, "tree")
        self.assertEqual(app.composer.text, "pls b p please")
        self.assertIn("board-request", self.request_kinds(app))
        app.handle_key("ctrl-o")
        self.assertEqual(app.screen, "tree")
        self.assertIn("board-request", self.request_kinds(app))
        self.assertEqual(self.primary.received, [])

    def test_ctrl_o_sends_without_the_request_only_while_the_refusal_still_describes_the_draft(self):
        # Break: the plain-send key survives an edit, Esc or a reopened draft, or never works.
        state = {"n": 0}

        def open_code():
            state["n"] += 1
            return 0 if state["n"] == 1 else 1

        app = self.ready(open_code=open_code)
        self.type_send(app, "go")
        app.handle_key("esc")
        self.assertIsNone(app.draft)
        app.handle_key("tab")
        app.handle_key("ctrl-o")
        self.assertEqual(app.screen, "tree")
        self.assertIn("board-request", self.request_kinds(app))
        app.handle_key("enter")
        self.assertIn("Ctrl-O", app.status)
        app.handle_key("ctrl-o")
        self.assertEqual(app.screen, "confirm-send", app.status)
        self.assertNotIn("board-request", self.request_kinds(app))
        self.assertEqual(app.composer.text, "go")
        self.assertEqual(self.primary.received, [])

    def test_ctrl_y_rebinds_only_before_the_text_changes_and_the_text_is_kept(self):
        # Break: a stale rebind offer binds a request the Captain has not reviewed, or drops the typed text.
        self.tasks[:] = [task("fp-ask-0001", "Summary", "waiting_review", review=True)]
        self.install_reads()
        self.identities = [LIFE, LIFE_NEXT, LIFE_NEXT, LIFE_NEXT]
        self.primary = FakePi(self.transport, self.home)
        app = self.board()
        self.open_detail(app)
        app.handle_key("tab")
        self.type_send(app, "approve")
        self.assertIn("Ctrl-Y", app.status)
        for char in " b":
            app.handle_key(char)
        app.handle_key("ctrl-y")
        self.assertEqual(app.screen, "tree")
        request = next(item for item in app.draft["attachments"] if item.get("kind") == "board-request")
        self.assertEqual(request["lifecycle"], LIFE)
        self.assertEqual(app.composer.text, "approve b")
        app.handle_key("enter")
        self.assertIn("Ctrl-Y", app.status)
        app.handle_key("esc")
        app.handle_key("tab")
        app.handle_key("ctrl-y")
        self.assertEqual(app.screen, "tree")
        request = next(item for item in app.draft["attachments"] if item.get("kind") == "board-request")
        self.assertEqual(request["lifecycle"], LIFE)

    def test_closed_unknown_and_timeout_keep_the_text(self):
        # Break: a closed, unverifiable or timed-out request is still pasted as that request.
        for code, words, timed_out in (
            (1, "resolved or released", False),
            (2, "Could not verify", False),
            (3, "Not in this FirstMate home", False),
            (124, "timed out", True),
        ):
            with self.subTest(code=code, timed_out=timed_out):
                drafts = self.state / "drafts"
                if drafts.is_dir():
                    for path in drafts.glob("*"):
                        path.unlink()
                self.transport.handlers.clear()
                self.transport.handlers.append((
                    lambda argv: argv[-2:] == ["list", "--json"],
                    lambda call: CmdResult(0, json.dumps(self.tasks), ""),
                ))
                seen = {"n": 0}

                def open_code(seen=seen, code=code, timed_out=timed_out):
                    seen["n"] += 1
                    if seen["n"] == 1:
                        return 0
                    return 0 if timed_out else code

                app = self.ready(open_code=open_code, timed_out=False)
                if timed_out:
                    self.transport.handlers.insert(0, (
                        lambda argv: len(argv) > 1 and argv[1] == "open",
                        lambda call: CmdResult(124, "", "timed out", timed_out=True),
                    ))
                self.type_send(app, "keep this")
                self.assertEqual(app.screen, "tree")
                self.assertIn(words, app.status)
                self.assertEqual(app.composer.text, "keep this")
                self.assertEqual(self.primary.received, [])

    def test_a_draft_for_another_home_is_refused(self):
        # Break: a draft captured for one home is pasted into another.
        app = self.ready()
        app.handle_key("k")
        app.handle_key("e")
        app.handle_key("e")
        app.handle_key("p")
        for item in app.draft["attachments"]:
            if item.get("kind") == "board-request":
                item["home"] = "/other/home"
        app.handle_key("enter")
        self.assertIn("wrong", app.status)
        self.assertIn("home", app.status)
        self.assertEqual(app.composer.text, "keep")
        self.assertEqual(self.primary.received, [])

    def test_the_bound_message_names_the_task_lifecycle_and_reply_id(self):
        # Break: the paste omits the request identity, so FirstMate cannot tell which call the words answer.
        app = self.ready()
        self.type_send(app, "use the lanes")
        body = app.pending["body"]
        self.assertIn("board task: fp-ask-0001\n", body)
        self.assertIn("project: dotfiles\n", body)
        self.assertIn("state: waiting_review\n", body)
        self.assertIn("firstmate home: " + os.path.realpath(self.home) + "\n", body)
        self.assertIn("request: open captain call fp-ask-0001 lifecycle " + LIFE + "\n", body)
        self.assertIn("asked: " + ASK + "\n", body)
        self.assertIn("reply id: " + app.draft["request_id"] + "\n", body)
        self.assertIn("recorded only when FirstMate records it", body)
        self.assertNotIn("not a dispatch", body)

    def test_y_twice_pastes_once_and_a_second_send_into_a_full_box_is_refused(self):
        # Break: a double confirm pastes twice, or a retry overwrites text already in the box.
        app = self.ready()
        self.type_send(app, "use the lanes")
        app.handle_key("y")
        app.handle_key("y")
        self.assertEqual(len(self.primary.received), 1)
        self.assertEqual(self.primary.submitted, [])
        app.handle_key("tab")
        for char in "again":
            app.handle_key(char)
        app.handle_key("enter")
        self.assertEqual(app.screen, "tree")
        self.assertIn("not transferred", app.status)
        self.assertEqual(len(self.primary.received), 1)

    def test_show_and_open_are_not_writers_and_answer_still_is(self):
        # Break: the read-only detail commands are refused, or an answer command is allowed through.
        self.app()
        self.transport.calls.append({"argv": [str(self.tool / "bin" / "fm-tasks-axi.sh"), "show", "fp-ask-0001", "--full"]})
        self.transport.calls.append({"argv": [str(self.tool / "bin" / "fm-captain-hold.sh"), "open", "fp-ask-0001", "--identity"]})
        self.assertEqual(self.writers(), [])
        self.transport.calls.append({"argv": [str(self.tool / "bin" / "fm-captain-hold.sh"), "answer", "fp-ask-0001"]})
        self.transport.calls.append({"argv": [str(self.tool / "bin" / "fm-tasks-axi.sh"), "answers", "fp-ask-0001"]})
        self.assertEqual(len(self.writers()), 2)


class CoverageTests(BadgeFixture):
    def test_a_configured_home_is_uncertain_before_any_successful_read(self):
        # Break: a configured home with no list yet looks like a verified zero.
        from concurrent.futures import Future
        from firstpane.app import App
        from tests.support import FakeTerminal
        held = []

        def submit(fn):
            future = Future()
            held.append(future)
            return future

        app = App(
            self.config, self.transport, self.state, self.env, self.clock, FakeTerminal(),
            rows=32, cols=120, submit=submit, wall=lambda: 1_700_000_100,
        )
        app.refresh(manual=True)
        self.assertEqual(app.tab, "agents")
        self.assertIn("Agents ?", self.plain_tab(app))
        self.assertNotIn("Board ?", self.plain_tab(app))
        self.assertNotIn(YELLOW_SGR, self.tab(app))
        self.assertNotIn("\u26a0", self.plain_tab(app))
        self.assertEqual(len(held), 1)
        app.refresh()
        self.assertEqual(len(held), 1)

    def test_a_successful_list_without_inventory_coverage_is_not_verified_current(self):
        # Break: exit 0 plus a present summary paints a yellow current warning or a clear zero.
        self.tasks[:] = [
            task("fp-ask-0001", "Summary", "waiting_review", review=True),
            task("fp-ask-0002", "Other ask", "todo", "alter", review=True),
        ]
        app = self.board()
        self.bind_waiting(app)
        self.assertIn("Agents \u26a0 1?", self.plain_tab(app))
        self.assertNotIn("Board \u26a0", self.plain_tab(app))
        self.assertNotIn(YELLOW_SGR, self.tab(app))
        for item in self.tasks:
            item["captain_review"] = False
        app.handle_key("r")
        app.render()
        self.assertIn("Agents ?", self.plain_tab(app))
        self.assertNotIn("Board ?", self.plain_tab(app))
        self.assertNotIn("\u26a0", self.plain_tab(app))
        self.assertNotIn(YELLOW_SGR, self.tab(app))

    def test_only_the_inventory_coverage_line_allows_a_verified_current_badge(self):
        # Break: any other stderr line, including silence, is treated as full inventory coverage.
        fn = getattr(board, "coverage_is_inventory", None)
        self.assertTrue(callable(fn), "coverage_is_inventory missing")
        self.assertFalse(fn(""))
        self.assertFalse(fn("reconcile skipped (fallback)"))
        self.assertFalse(fn("reconcile coverage: fallback"))
        self.assertTrue(fn("reconcile coverage: inventory\n"))
        self.assertTrue(fn("fm-board: reconcile coverage: inventory\n"))
        self.tasks[:] = [task("fp-ask-0001", "Summary", "waiting_review", review=True)]
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, json.dumps(self.tasks), "fm-board: reconcile coverage: inventory"),
        ))
        app = self.board()
        self.bind_waiting(app)
        self.assertIn("Agents \u26a0 1", self.plain_tab(app))
        self.assertNotIn("Agents \u26a0 1?", self.plain_tab(app))
        self.assertNotIn("Board \u26a0", self.plain_tab(app))
        self.assertIn(YELLOW_SGR, self.tab(app))


class CadenceTests(BadgeFixture):
    def test_agents_lists_the_board_once_per_minute_and_a_hidden_board_lists_nothing(self):
        # Break: hiding Board stops the Agents reader, or hiding Agents and Board keeps reading.
        app = self.app()
        app.render()
        self.assertEqual(len(self.lists()), 1)
        app.handle_key("3")
        app.render()
        self.assertEqual(len(self.lists()), 1)
        app.handle_key("1")
        app.refresh()
        self.clock.t += 59
        app.refresh()
        self.assertEqual(len(self.lists()), 1)
        self.clock.t += 2
        app.refresh()
        self.assertEqual(len(self.lists()), 2)
        app.hidden_tabs.add("board")
        app.refresh()
        before = len(self.lists())
        self.clock.t += 60
        app.refresh()
        self.assertEqual(len(self.lists()), before + 1)
        app.hidden_tabs.add("agents")
        app.refresh()
        before = len(self.lists())
        self.clock.t += 180
        app.refresh()
        self.assertEqual(len(self.lists()), before)

    def test_a_list_already_in_flight_is_not_started_again(self):
        # Break: two board reads run at once.
        from concurrent.futures import Future
        app = self.app()
        app.handle_key("3")
        app.render()
        app.handle_key("1")
        held = []

        def submit(fn):
            future = Future()
            held.append((fn, future))
            return future

        app.submit = submit
        app.board_job = None
        app.board_loaded_at = 0
        self.clock.t += 100
        app.refresh()
        app.refresh()
        self.assertEqual(len(held), 1)
