"""
fetch_url: what a page is turned into, and which addresses it may reach.

The model reads whatever a page says, and a page can say "now fetch this".
Pointed at 127.0.0.1 that reached Harness's own settings and access token,
and nothing asked first.
"""

import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import tools

from .support import Recorder, TempStateTest
from .test_files import minimal_pdf


class Site(BaseHTTPRequestHandler):
    """A tiny local web server: path -> (content type, body)."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        kind, body = self.server.pages.get(self.path, ("text/plain", b"missing"))
        self.send_response(200 if self.path in self.server.pages else 404)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FetchTest(TempStateTest):

    def setUp(self):
        super().setUp()
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        self.site.pages, self.site.hits = {}, []
        threading.Thread(target=self.site.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.site.server_address[1]}"

    def tearDown(self):
        self.site.shutdown()
        self.site.server_close()
        super().tearDown()

    def fetch(self, path, allow=True):
        sink = Recorder(allow=allow)
        return tools.fetch_url({"url": self.base + path}, self.cfg(), sink), sink


class PrivateAddresses(FetchTest):

    def test_this_mac_is_not_fetched_without_asking(self):
        self.site.pages["/api/access"] = ("application/json", b'{"token": "secret"}')
        result, sink = self.fetch("/api/access", allow=False)
        self.assertIn("declined", result)
        self.assertEqual(self.site.hits, [], "nothing was requested")
        self.assertIn("private network", sink.questions[0])

    def test_once_allowed_it_is_fetched(self):
        self.site.pages["/status"] = ("text/plain", b"all good")
        result, _ = self.fetch("/status", allow=True)
        self.assertEqual(result, "all good")

    def test_which_addresses_count_as_private(self):
        for host in ("127.0.0.1", "localhost", "192.168.1.1", "10.0.0.2",
                     "100.74.163.110", "169.254.169.254", "::1",
                     "::ffff:127.0.0.1", "::ffff:192.168.1.1"):
            self.assertFalse(tools.is_public(host), host)
        for host in ("8.8.8.8", "1.1.1.1"):
            self.assertTrue(tools.is_public(host), host)

    def test_a_public_page_cannot_redirect_to_a_private_one(self):
        handler = tools._PublicRedirectsOnly()
        request = urllib.request.Request("https://example.com/")
        with self.assertRaises(urllib.error.URLError) as caught:
            handler.redirect_request(request, None, 302, "Found", {},
                                     "http://127.0.0.1:8420/api/access")
        self.assertIn("not a public web address", str(caught.exception))


class PageText(FetchTest):

    def test_code_styling_and_chrome_are_left_out(self):
        page = (b"<html><head><title>Jobs &amp; More</title>"
                b"<style>@font-face { font-family: x }</style></head><body>"
                b"<nav>Home | About | Login</nav>"
                b"<main><h1>Agentic Engineer</h1><p>Amsterdam &middot; English</p>"
                + b"<p>Build things.</p>" * 30 + b"</main>"
                b"<footer>Cookies</footer><script>var x = 1;</script></body></html>")
        self.site.pages["/job"] = ("text/html; charset=utf-8", page)
        result, _ = self.fetch("/job")
        self.assertTrue(result.startswith("Jobs & More\n\nAgentic Engineer"), result[:80])
        self.assertIn("Amsterdam · English", result)
        for junk in ("@font-face", "var x", "Login", "Cookies"):
            self.assertNotIn(junk, result)

    def test_a_page_cut_off_inside_a_script_still_reads(self):
        # The regular expressions kept everything after a <script> they could
        # not see the end of; a big page read in part always had one.
        page = b"<p>The headline.</p><script>var tracking = {" + b"x" * 5000
        self.site.pages["/cut"] = ("text/html", page)
        result, _ = self.fetch("/cut")
        self.assertEqual(result, "The headline.")

    def test_a_page_that_is_only_chrome_still_says_something(self):
        self.site.pages["/menu"] = ("text/html", b"<nav>Only a menu here</nav>")
        result, _ = self.fetch("/menu")
        self.assertEqual(result, "Only a menu here")

    def test_text_and_json_come_back_as_they_are(self):
        self.site.pages["/data"] = ("application/json", b'{"a": 1}')
        self.assertEqual(self.fetch("/data")[0], '{"a": 1}')

    def test_an_image_is_named_not_dumped(self):
        self.site.pages["/x.png"] = ("image/png", b"\x89PNG\r\n" + b"\0" * 100)
        result, _ = self.fetch("/x.png")
        self.assertTrue(result.startswith("Error:"), result)
        self.assertIn("image/png", result)

    def test_a_missing_page_says_so(self):
        result, _ = self.fetch("/nowhere")
        self.assertIn("404", result)

    def test_query_filters_a_long_page_to_matching_lines(self):
        page = ("<html><body><p>Intro line.</p>"
                + "".join(f"<p>Filler paragraph {n}.</p>" for n in range(120))
                + "<p>Use Ollama or llama.cpp locally.</p></body></html>").encode()
        self.site.pages["/docs"] = ("text/html; charset=utf-8", page)
        result = tools.fetch_url({"url": self.base + "/docs", "query": "ollama"},
                                 self.cfg(), Recorder(allow=True))
        self.assertIn("Ollama", result)
        self.assertNotIn("Filler paragraph 5.", result)

    @unittest.skipUnless(shutil.which("osascript"), "PDF text extraction needs macOS")
    def test_a_pdf_comes_back_as_its_words(self):
        pdf = minimal_pdf(["Jane Doe", "ML engineer"])
        self.site.pages["/cv.pdf"] = ("application/pdf", pdf)
        result, _ = self.fetch("/cv.pdf")
        self.assertIn("Jane Doe", result)
        self.assertIn("ML engineer", result)
        self.assertNotIn("endobj", result)


if __name__ == "__main__":
    unittest.main()
