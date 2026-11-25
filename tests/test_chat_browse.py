"""chat_browse: the browser on the VPS as Chat tools, on its in-memory fake (no ssh, no internet)."""
try:
    import _tmp  # noqa: F401
except ImportError:
    from tests import _tmp  # noqa: F401
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mac"))
import chat_browse as B  # noqa: E402
import chat_tools as T  # noqa: E402


class BrowseTests(unittest.TestCase):
    def ctx(self, level="box_web", private=False, ask="deny", owner="find me a shop"):
        self.row = {"perm": level, "messages": [{"role": "user", "content": owner}]}
        store = type("Store", (), {"chat": lambda _, cid: self.row})()
        c = T.TurnCtx("chat1", self.row, store, "dsv41-unc", level=level, private=private, fake=True)
        self.asked = []
        c.ask = lambda req: (self.asked.append(req), ask)[1]
        return c

    def tools(self, ctx):
        b = B.toolset(ctx)
        self.fake = b.remote
        return b

    def ops(self):
        return [c["op"] for c in self.fake.calls]

    def test_offered_at_the_web_levels_and_never_private(self):
        for level in ("none", "box"):
            self.assertIsNone(B.toolset(self.ctx(level)))
        for level in ("box_web", "full", "direct"):
            self.assertIsNotNone(B.toolset(self.ctx(level)))
        self.assertIsNone(B.toolset(self.ctx("direct", private=True)))
        self.assertIn("browser_open", [s["function"]["name"] for s in B.toolset(self.ctx()).specs()])

    def test_never_for_kyojin_and_uncensored_at_full_asks_every_step(self):
        c = self.ctx("full")
        c.model_id = "qwen38-kyojin"
        self.assertIsNone(B.toolset(c))
        c = self.ctx("full")  # dsv41-unc (J2: every web use waits for a tap)
        b = self.tools(c)
        r = b.call("browser_open", {"url": "https://shop.example/"}, c)
        self.assertEqual((r["status"], len(self.asked)), ("denied", 1))
        self.assertEqual(self.ops(), [])
        c = self.ctx("full", ask="allow")
        b = self.tools(c)
        b.call("browser_open", {"url": "https://shop.example/"}, c)
        b.call("browser_click", {"n": 4}, c)
        b.call("browser_scroll", {"direction": "down"}, c)
        self.assertEqual(len(self.asked), 2)  # the open and the click; a scroll sends nothing
        c = self.ctx("full")
        c.model_id = "dsv41"  # the official model at Full: only risky clicks ask
        b = self.tools(c)
        self.assertEqual(b.call("browser_open", {"url": "https://shop.example/"}, c)["status"], "ok")
        self.assertEqual(self.asked, [])

    def test_a_page_reads_as_text_and_numbered_elements_fenced_as_web_data(self):
        c = self.ctx()
        r = self.tools(c).call("browser_open", {"url": "shop.example"}, c)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.fake.calls[0]["url"], "https://shop.example")
        self.assertIn('[3] button "Place order"', r["text"])
        self.assertIn('[4] link "About" -> https://shop.example/about', r["text"])
        self.assertIn("Welcome to the shop.", r["text"])
        self.assertIn("<untrusted", r["text"])
        self.assertEqual(r["line"], "Browser: opened https://shop.example")

    def test_a_search_goes_without_a_tap_and_an_order_waits_for_one(self):
        c = self.ctx()
        b = self.tools(c)
        b.call("browser_open", {"url": "https://shop.example/"}, c)
        r = b.call("browser_type", {"n": 1, "text": "red shoes", "submit": True}, c)
        self.assertEqual((r["status"], self.asked), ("ok", []))
        r = b.call("browser_click", {"n": 3}, c)
        self.assertEqual((r["status"], r["decision"]), ("denied", "tap_denied"))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("shop.example", self.asked[0]["command"])
        self.assertIn("Place order", self.asked[0]["command"])
        self.assertNotIn("click", self.ops())  # nothing was clicked

    def test_allowed_tap_clicks(self):
        c = self.ctx(ask="allow")
        b = self.tools(c)
        r = b.call("browser_click", {"n": 3}, c)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.ops(), ["inspect", "click"])

    def test_direct_access_never_asks(self):
        c = self.ctx("direct")
        b = self.tools(c)
        self.assertEqual(b.call("browser_click", {"n": 3}, c)["status"], "ok")
        self.assertEqual(self.asked, [])

    def test_password_fields_are_refused_at_every_level(self):
        for level in ("box_web", "direct"):
            c = self.ctx(level)
            b = self.tools(c)
            r = b.call("browser_type", {"n": 5, "text": "hunter2"}, c)
            self.assertEqual(r["status"], "refused")
            self.assertNotIn("type", self.ops())

    def test_an_email_waits_for_a_tap_below_direct(self):
        c = self.ctx()
        b = self.tools(c)
        r = b.call("browser_type", {"n": 1, "text": "someone.private@example.com"}, c)
        self.assertEqual(r["status"], "refused")
        self.assertEqual(self.ops(), [])
        self.assertIn("someone.private@example.com", self.asked[0]["command"])
        c = self.ctx(ask="allow")
        b = self.tools(c)
        self.assertEqual(b.call("browser_type", {"n": 1, "text": "someone.private@example.com"}, c)["status"], "ok")
        c = self.ctx("direct")
        b = self.tools(c)
        self.assertEqual(b.call("browser_type", {"n": 1, "text": "someone.private@example.com"}, c)["status"], "ok")
        self.assertEqual(self.asked, [])

    def test_look_is_fenced_and_says_it_can_be_wrong(self):
        c = self.ctx()
        b = self.tools(c)
        b.call("browser_open", {"url": "https://shop.example/"}, c)
        r = b.call("browser_look", {"question": "what colour is the buy button?"}, c)
        self.assertIn("red Buy button", r["text"])
        self.assertIn("can miss", r["text"])
        self.assertIn("<untrusted", r["text"])

    def test_the_session_is_the_chats_and_not_its_id(self):
        self.assertEqual(B.session_of("chat1"), B.session_of("chat1"))
        self.assertNotIn("chat1", B.session_of("chat1"))


if __name__ == "__main__":
    unittest.main()
