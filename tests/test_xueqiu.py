import base64
import io
import json
import os
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import post_xueqiu_status as post
import xueqiu_waf_refresh as refresh


class XueqiuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = self.base / ".env"
        self.env.write_text("OTHER_SETTING=keep\n")
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.base_patch = patch.object(post, "BASE_DIR", self.base)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        post.save_cookie("xq_a_token=old; xq_id_token=id; u=123; removable=old")

    def response(self, body, cookies=()):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = body.encode()
        response.headers = Message()
        for cookie in cookies:
            response.headers.add_header("Set-Cookie", cookie)
        return response

    def test_token_rotation_is_saved_and_used_for_post(self):
        token = self.response('{"session_token":"session"}', ["xq_a_token=new; Domain=.xueqiu.com; Path=/; HttpOnly"])
        published = self.response('{"id":12345}')
        with patch.object(post.request, "urlopen", side_effect=[token, published]) as open_url:
            post.post_status("test")
        req = open_url.call_args_list[1].args[0]
        self.assertIn("xq_a_token=new", req.get_header("Cookie"))
        self.assertEqual(req.get_header("X-csrf-token"), "new")
        saved = post.dotenv_values(self.env)
        self.assertIn("xq_a_token=new", base64.b64decode(saved["XUEQIU_COOKIE_B64"]).decode())
        self.assertEqual(saved["OTHER_SETTING"], "keep")

    def test_expired_and_foreign_cookies(self):
        headers = Message()
        headers.add_header("Set-Cookie", "removable=; Max-Age=0")
        headers.add_header("Set-Cookie", "xq_a_token=foreign; Domain=notxueqiu.com")
        post.save_response_cookies(headers)
        self.assertNotIn("removable", post.get_xueqiu_cookie())
        self.assertIn("xq_a_token=old", post.get_xueqiu_cookie())

    def test_cookie_write_failure_does_not_hide_post_success(self):
        response = self.response('{"id":123}', ["xq_a_token=new"])
        with patch.object(post.request, "urlopen", return_value=response), patch.object(post, "save_cookie", side_effect=OSError), patch("sys.stderr", new=io.StringIO()):
            status, _ = post.call_xueqiu(post.DEFAULT_API_URL, {}, post.get_xueqiu_cookie())
        self.assertEqual(status, 200)
        self.assertIn("xq_a_token=new", post.get_xueqiu_cookie())

    def test_plain_cookie_is_replaced_and_browser_agent_saved(self):
        self.env.write_text("XUEQIU_COOKIE=stale\nOTHER_SETTING=keep\n")
        os.environ["XUEQIU_COOKIE"] = "stale"
        post.save_cookie("u=123; xq_a_token=new", "browser-agent")
        self.assertNotIn("XUEQIU_COOKIE", os.environ)
        self.assertNotIn("XUEQIU_COOKIE", post.dotenv_values(self.env))
        self.assertEqual(os.environ["XUEQIU_USER_AGENT"], "browser-agent")

    def test_http_200_error_is_not_success(self):
        with patch.object(post.request, "urlopen", return_value=self.response('{"error_code":"400016","error_description":"登录失效"}')):
            with self.assertRaises(post.XueqiuError) as caught:
                post.get_session_token()
        self.assertEqual(caught.exception.reason, "cookie_invalid")
        self.assertTrue(caught.exception.recoverable)

    def test_http_400_auth_error_can_refresh_before_publication(self):
        error = HTTPError(post.DEFAULT_TOKEN_URL, 400, "Bad Request", Message(), io.BytesIO(b'{"error_code":"400016"}'))
        with patch.object(post.request, "urlopen", side_effect=error):
            with self.assertRaises(post.XueqiuError) as caught:
                post.get_session_token()
        self.assertTrue(caught.exception.recoverable)

    def test_verification_has_distinct_reason(self):
        for stage in ("token", "post"):
            with self.subTest(stage=stage), self.assertRaises(post.XueqiuError) as caught:
                post.parse_response("<html>请完成人机验证</html>", stage)
            self.assertEqual(caught.exception.reason, "verification_required")
            self.assertEqual(caught.exception.recoverable, stage == "token")

    def test_unconfirmed_publication_is_not_retried(self):
        for body in ('{"success":true}', "<html>unavailable</html>"):
            with self.subTest(body=body), patch.object(post.request, "urlopen", return_value=self.response(body)), patch.object(post, "refresh_waf_cookie") as recover:
                with self.assertRaises(post.XueqiuError):
                    post.with_recovery(lambda: post.call_xueqiu(post.DEFAULT_API_URL, {}, post.get_xueqiu_cookie()))
                recover.assert_not_called()

    def test_network_failure_after_post_is_not_retried(self):
        with patch.object(post.request, "urlopen", side_effect=URLError("timed out")), patch.object(post, "refresh_waf_cookie") as recover:
            with self.assertRaises(post.XueqiuError):
                post.with_recovery(lambda: post.call_xueqiu(post.DEFAULT_API_URL, {}, post.get_xueqiu_cookie()))
            recover.assert_not_called()

    def test_refresh_retries_once(self):
        error = post.XueqiuError("cookie_invalid", "expired", True)
        action = Mock(side_effect=[error, (200, '{"id":123}')])
        with patch.object(post, "refresh_waf_cookie", return_value=True) as recover:
            post.with_recovery(action)
        self.assertEqual(action.call_count, 2)
        recover.assert_called_once()

    def test_second_failure_stops(self):
        action = Mock(side_effect=post.XueqiuError("waf_blocked", "rejected", True))
        with patch.object(post, "refresh_waf_cookie", return_value=True) as recover:
            with self.assertRaises(post.XueqiuError):
                post.with_recovery(action)
        self.assertEqual(action.call_count, 2)
        recover.assert_called_once()

    def test_captcha_from_browser_stops_retry(self):
        proc = Mock(returncode=1, stdout=json.dumps({"reason":"verification_required","detail":"请完成人机验证"}))
        action = Mock(side_effect=post.XueqiuError("cookie_invalid", "expired", True))
        with patch.object(post.subprocess, "run", return_value=proc), patch.object(Path, "exists", return_value=True):
            with self.assertRaises(post.XueqiuError) as caught:
                post.with_recovery(action)
        self.assertEqual(caught.exception.reason, "verification_required")
        action.assert_called_once()

    def test_validate_checks_sending_token_even_if_posted(self):
        text_file = self.base / "text.txt"
        text_file.write_text("test")
        args = Mock(text_file=str(text_file), trade_date="20261019", session_label="morning-close", dry_run=False, validate_only=True)
        with patch.object(post, "parse_args", return_value=args), patch.object(post, "history_contains", return_value=True), patch.object(post, "get_session_token") as token, patch.object(post, "validate_text", return_value=(200, '{"success":true}')), patch.object(post, "append_history"), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(post.main(), 0)
        token.assert_called_once()

    def test_posted_history_still_skips_publication(self):
        text_file = self.base / "text.txt"
        text_file.write_text("test")
        args = Mock(text_file=str(text_file), trade_date="20261019", session_label="morning-close", dry_run=False, validate_only=False)
        with patch.object(post, "parse_args", return_value=args), patch.object(post, "history_contains", return_value=True), patch.object(post, "post_status") as publish, patch.object(post, "append_history"), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(post.main(), 0)
        publish.assert_not_called()

    def test_browser_merge_excludes_unrelated_domains(self):
        cookie = refresh.merge_cookie_header("", [{"name":"u","value":"123","domain":".xueqiu.com"}, {"name":"bad","value":"secret","domain":"evilxueqiu.com"}])
        self.assertEqual(cookie, "u=123")

    def browser_refresh(self, user="123", captcha=False):
        context = Mock()
        context.cookies.return_value = [{"name":"u", "value":user, "domain":".xueqiu.com"}, {"name":"xq_a_token", "value":"browser-new", "domain":".xueqiu.com"}]
        page = context.new_page.return_value
        page.title.return_value = "访问提示" if captcha else "我的首页"
        page.url = "https://check.xueqiu.com/captcha" if captcha else "https://xueqiu.com/"
        page.locator.return_value.inner_text.return_value = "请完成人机验证" if captcha else "home"
        page.evaluate.return_value = "browser-agent"
        response = context.request.get.return_value
        response.status = 200
        response.text.return_value = '{"session_token":"session"}'
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        with patch("playwright.sync_api.sync_playwright", return_value=manager), patch.object(refresh.request, "urlopen", side_effect=URLError("closed")), patch.object(refresh, "load_cookie", return_value=post.get_xueqiu_cookie()), patch.object(refresh, "PROFILE_PATH", self.base / "profile"), patch.object(refresh, "ENV_PATH", self.env), patch.object(refresh, "save_cookie") as save:
            result = refresh.refresh(True)
        return result, context, save

    def test_persistent_browser_preserves_remembered_tokens(self):
        result, context, save = self.browser_refresh()
        self.assertTrue(result["ok"])
        context.add_cookies.assert_not_called()
        self.assertIn("xq_a_token=browser-new", save.call_args.args[0])
        self.assertEqual(save.call_args.args[1], "browser-agent")
        context.close.assert_called_once()

    def test_browser_verification_does_not_overwrite_cookie(self):
        result, context, save = self.browser_refresh(captcha=True)
        self.assertEqual(result["reason"], "verification_required")
        save.assert_not_called()
        context.request.get.assert_not_called()

    def test_other_browser_account_is_never_published_as(self):
        result, context, save = self.browser_refresh(user="456")
        self.assertEqual(result["reason"], "account_mismatch")
        context.new_page.assert_not_called()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
