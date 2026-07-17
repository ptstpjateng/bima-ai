"""Tests for services.doc_resend — auto-resend a WhatsApp document on Meta 131053."""

import asyncio
import logging
import unittest
from unittest.mock import patch

import services.doc_resend as dr


def _run(coro):
    return asyncio.run(coro)


class _Recorder:
    """Stand-in for whatsapp_sender.send_document — records the resend calls."""

    def __init__(self, ret=True):
        self.calls = []
        self.ret = ret

    async def __call__(self, recipient_phone, link, filename, *, caption=None):
        self.calls.append({
            "recipient": recipient_phone, "link": link,
            "filename": filename, "caption": caption,
        })
        return self.ret


class DocResendTests(unittest.TestCase):
    def setUp(self):
        dr._reset_for_tests()
        self._orig_backoff = dr._BACKOFF_SECONDS
        dr._BACKOFF_SECONDS = 0.0  # no real wait in tests

    def tearDown(self):
        dr._BACKOFF_SECONDS = self._orig_backoff
        dr._reset_for_tests()

    async def _drain(self):
        for t in list(dr._resend_tasks):
            await t

    # (a) 131053 → one resend with the stored link/filename/caption.
    def test_resend_on_131053(self):
        rec = _Recorder()

        async def scenario():
            with patch("services.whatsapp_sender.send_document", rec):
                dr.register("62811222333", "https://x/dl/tok", "SK.pdf", "cap")
                self.assertTrue(dr.maybe_resend("62811222333", [131053]))
                await self._drain()

        _run(scenario())
        self.assertEqual(len(rec.calls), 1)
        c = rec.calls[0]
        self.assertEqual(c["recipient"], "62811222333")
        self.assertEqual(c["link"], "https://x/dl/tok")
        self.assertEqual(c["filename"], "SK.pdf")
        self.assertEqual(c["caption"], "cap")

    # 131053 mixed with other codes still resends.
    def test_resend_when_131053_among_codes(self):
        rec = _Recorder()

        async def scenario():
            with patch("services.whatsapp_sender.send_document", rec):
                dr.register("62811222333", "l", "f.pdf", None)
                self.assertTrue(dr.maybe_resend("62811222333", [0, 131053, 5]))
                await self._drain()

        _run(scenario())
        self.assertEqual(len(rec.calls), 1)

    # (b) a non-retryable code does NOT resend.
    def test_non_retryable_code_no_resend(self):
        rec = _Recorder()

        async def scenario():
            with patch("services.whatsapp_sender.send_document", rec):
                dr.register("62811222333", "l", "f.pdf", None)
                self.assertFalse(dr.maybe_resend("62811222333", [131047]))
                self.assertFalse(dr.maybe_resend("62811222333", [470]))
                self.assertFalse(dr.maybe_resend("62811222333", []))
                self.assertFalse(dr.maybe_resend("62811222333", None))
                await self._drain()

        _run(scenario())
        self.assertEqual(rec.calls, [])

    # (c) retry cap: after _MAX_RETRIES resends, a further 131053 is refused.
    def test_retry_cap_no_infinite_loop(self):
        rec = _Recorder()

        async def scenario():
            with patch("services.whatsapp_sender.send_document", rec):
                dr.register("62811222333", "https://x/dl/tok", "SK.pdf", None)
                # Each round: fail → resend → the resend re-registers the SAME
                # link (simulated) which must KEEP the count.
                for _ in range(dr._MAX_RETRIES):
                    self.assertTrue(dr.maybe_resend("62811222333", [131053]))
                    await self._drain()
                    dr.register("62811222333", "https://x/dl/tok", "SK.pdf", None)
                # Cap reached → no more resends, ever.
                self.assertFalse(dr.maybe_resend("62811222333", [131053]))
                await self._drain()

        _run(scenario())
        self.assertEqual(len(rec.calls), dr._MAX_RETRIES)

    # same-link keeps the counter; a different link resets it (loop safety).
    def test_same_link_keeps_count_diff_link_resets(self):
        key = dr._digits("62811222333")
        dr.register("62811222333", "linkA", "a.pdf", None)
        dr._records[key]["retries"] = 2
        dr.register("62811222333", "linkA", "a.pdf", None)  # same link
        self.assertEqual(dr._records[key]["retries"], 2)
        dr.register("62811222333", "linkB", "b.pdf", None)  # new document
        self.assertEqual(dr._records[key]["retries"], 0)

    # (e) a status for a recipient we never sent a doc to is a no-op.
    def test_unknown_recipient_no_resend(self):
        self.assertFalse(dr.maybe_resend("62899999999", [131053]))

    # (f) no running event loop → returns False, never raises, no retry counted.
    def test_no_running_loop_returns_false_no_raise(self):
        dr.register("62811222333", "l", "f.pdf", None)
        # Called synchronously (no running loop): create_task raises internally,
        # is caught, and we return False without counting a retry.
        self.assertFalse(dr.maybe_resend("62811222333", [131053]))
        self.assertEqual(dr._records[dr._digits("62811222333")]["retries"], 0)

    # register only tracks the LAST doc per recipient (bounded per key).
    def test_register_keeps_last_doc(self):
        dr.register("62811222333", "l1", "a.pdf", None)
        dr.register("62811222333", "l2", "b.pdf", None)
        rec = dr._records[dr._digits("62811222333")]
        self.assertEqual(rec["link"], "l2")
        self.assertEqual(rec["filename"], "b.pdf")

    # (g) the download token/filename/caption are NEVER logged — only masked
    # recipient + code + count.
    def test_link_and_filename_never_logged(self):
        rec = _Recorder()
        secret_link = "https://x/dl/SECRET_TOKEN_abc123"
        secret_file = "SK_RAHASIA_000077765.pdf"

        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                try:
                    self.messages.append(record.getMessage())
                except Exception:
                    pass

        cap = _Capture()
        dr.logger.addHandler(cap)
        dr.logger.setLevel(logging.DEBUG)
        try:
            async def scenario():
                with patch("services.whatsapp_sender.send_document", rec):
                    dr.register("62811222333", secret_link, secret_file, "rahasia")
                    dr.maybe_resend("62811222333", [131053])
                    await self._drain()

            _run(scenario())
        finally:
            dr.logger.removeHandler(cap)

        blob = "\n".join(cap.messages)
        self.assertNotIn(secret_link, blob)
        self.assertNotIn("SECRET_TOKEN", blob)
        self.assertNotIn(secret_file, blob)
        self.assertNotIn("rahasia", blob)
        self.assertNotIn("62811222333", blob)  # full phone never logged
        self.assertTrue(cap.messages)  # something WAS logged (masked)

    # env-parsed retryable code set.
    def test_parse_codes(self):
        self.assertEqual(dr._parse_codes("131053"), {131053})
        self.assertEqual(dr._parse_codes("131053, 131000 ,x"), {131053, 131000})
        self.assertEqual(dr._parse_codes(""), {131053})  # default


class TestGiveUpTellsTheOfficer(unittest.TestCase):
    """Giving up silently leaves a lie standing.

    Live 2026-07-17, Kabid 6285…6789: the copilot said "saya kirim sebagai PDF
    (Rekomtek_000077769.pdf)" at 09:45:41 — that text goes out immediately —
    then the media failed 131053 at :51, :57, :46:03 and doc_resend gave up. The
    officer was left looking at a claim that a document had been sent, with no
    document and no correction. A promise left standing after the action failed
    is the same defect as never having tried.
    """

    def setUp(self):
        dr._reset_for_tests()
        self.sent: list = []

        async def fake_send_text(recipient, body, **kw):
            self.sent.append({"to": recipient, "body": body})
            return True

        self.p = patch("services.whatsapp_sender.send_text", new=fake_send_text)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        dr._reset_for_tests()

    async def _exhaust(self, to="628999111222", link="https://x/dl/tok1",
                       name="Rekomtek_000077769.pdf"):
        dr.register(to, link, name)
        # Two failures schedule the two allowed retries...
        dr.maybe_resend(to, [131053])
        dr.maybe_resend(to, [131053])
        # ...the third hits the cap and must announce it.
        dr.maybe_resend(to, [131053])
        await asyncio.sleep(0.05)

    def test_officer_is_told_when_we_give_up(self):
        _run(self._exhaust())
        self.assertEqual(len(self.sent), 1, "the officer was never told")
        body = self.sent[0]["body"]
        self.assertIn("Rekomtek_000077769.pdf", body)
        self.assertIn("gagal terkirim", body)
        # It must not imply the draft was lost — it was made, just not delivered.
        self.assertIn("sudah selesai dibuat", body)

    def test_notice_is_latched_not_spammed(self):
        _run(self._exhaust())
        # More failed statuses for the SAME document must not re-announce.
        dr.maybe_resend("628999111222", [131053])
        dr.maybe_resend("628999111222", [131053])
        _run(asyncio.sleep(0.05))
        self.assertEqual(len(self.sent), 1, "spammed the officer with notices")

    def test_a_new_document_resets_the_latch(self):
        _run(self._exhaust())
        self.assertEqual(len(self.sent), 1)
        # A genuinely NEW document (different link) must be able to announce too.
        _run(self._exhaust(link="https://x/dl/tok2", name="SK_000077770.pdf"))
        self.assertEqual(len(self.sent), 2, "a new document could not announce")
        self.assertIn("SK_000077770.pdf", self.sent[1]["body"])

    def test_no_notice_while_retries_remain(self):
        dr.register("628999111222", "https://x/dl/tokA", "a.pdf")
        dr.maybe_resend("628999111222", [131053])   # attempt 1/2 — still hopeful
        _run(asyncio.sleep(0.01))
        self.assertEqual(self.sent, [], "apologised while a retry was still pending")

    def test_non_retryable_code_never_announces(self):
        dr.register("628999111222", "https://x/dl/tokB", "b.pdf")
        dr.maybe_resend("628999111222", [131047])   # 24h window — not ours
        _run(asyncio.sleep(0.01))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
