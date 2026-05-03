import json
import tempfile
import unittest
from pathlib import Path

from tools.jellyfin_http import JellyfinHTTP


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class JellyfinHTTPTests(unittest.TestCase):
    def test_request_builds_url_injects_auto_token_and_logs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeHTTPClient([FakeResponse(204, "", {"X-Test": "yes"})])
            http = JellyfinHTTP(
                base_url="http://localhost:8097",
                run_id="run",
                artifacts_root=temp_dir,
                client=client,
            )
            http.token = "token-1"

            result = http.request("POST", "/Library", body_json={"Name": "Movies"})

            self.assertEqual(result["status_code"], 204)
            self.assertEqual(client.requests[0]["url"], "http://localhost:8097/Library")
            self.assertEqual(client.requests[0]["json"], {"Name": "Movies"})
            self.assertEqual(client.requests[0]["headers"]["X-Emby-Token"], "token-1")
            self.assertTrue(Path(temp_dir, "run", "http_log.jsonl").exists())

    def test_auth_none_suppresses_stored_token(self):
        client = FakeHTTPClient([FakeResponse(200, "ok")])
        http = JellyfinHTTP(client=client)
        http.token = "token-1"

        result = http.request("GET", "/Public", auth="none")

        self.assertEqual(result["status_code"], 200)
        self.assertNotIn("X-Emby-Token", client.requests[0]["headers"])

    def test_auth_token_uses_explicit_token(self):
        client = FakeHTTPClient([FakeResponse(200, "ok")])
        http = JellyfinHTTP(client=client)
        http.token = "stored-token"

        result = http.request("GET", "/Items", auth="token", token="custom-token")

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(client.requests[0]["headers"]["X-Emby-Token"], "custom-token")

    def test_body_text_preserves_malformed_json(self):
        client = FakeHTTPClient([FakeResponse(400, "bad json")])
        http = JellyfinHTTP(client=client)

        result = http.request(
            "POST",
            "/Bad",
            auth="none",
            headers={"Content-Type": "application/json"},
            body_text='{"Name":',
        )

        self.assertEqual(result["status_code"], 400)
        self.assertEqual(client.requests[0]["content"], '{"Name":')
        self.assertNotIn("json", client.requests[0])

    def test_body_base64_sends_raw_bytes(self):
        client = FakeHTTPClient([FakeResponse(200, "ok")])
        http = JellyfinHTTP(client=client)

        result = http.request("POST", "/Bytes", auth="none", body_base64="AAEC")

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(client.requests[0]["content"], b"\x00\x01\x02")

    def test_params_timeout_redirects_and_header_pairs_pass_through(self):
        client = FakeHTTPClient([FakeResponse(200, "ok")])
        http = JellyfinHTTP(client=client)

        result = http.request(
            "GET",
            "/Search",
            auth="none",
            params=[["q", "movie"], ["q", "show"]],
            headers=[["X-Test", "one"], ["X-Test", "two"]],
            timeout_s=3,
            follow_redirects=True,
        )

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(client.requests[0]["params"], [["q", "movie"], ["q", "show"]])
        self.assertEqual(client.requests[0]["headers"], [("X-Test", "one"), ("X-Test", "two")])
        self.assertEqual(client.requests[0]["timeout"], 3.0)
        self.assertTrue(client.requests[0]["follow_redirects"])

    def test_multiple_body_modes_are_rejected(self):
        client = FakeHTTPClient([FakeResponse(200, "ok")])
        http = JellyfinHTTP(client=client)

        with self.assertRaisesRegex(ValueError, "body_json, body_text, or body_base64"):
            http.request("POST", "/Bad", body_json={}, body_text="{}")

        self.assertEqual(client.requests, [])

    def test_absolute_url_requires_explicit_allow(self):
        client = FakeHTTPClient([FakeResponse(200, "ok")])
        http = JellyfinHTTP(client=client)

        with self.assertRaisesRegex(ValueError, "absolute URLs"):
            http.request("GET", "http://localhost:8096/health")

        result = http.request(
            "GET",
            "http://localhost:8096/health",
            auth="none",
            allow_absolute_url=True,
        )

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(client.requests[0]["url"], "http://localhost:8096/health")

    def test_request_retries_connection_refused(self):
        client = FakeHTTPClient(
            [
                ConnectionRefusedError("connection refused"),
                FakeResponse(200, "ok"),
            ]
        )
        http = JellyfinHTTP(client=client, sleep_fn=lambda seconds: None)

        result = http.request("GET", "/health")

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(len(client.requests), 2)

    def test_wait_healthy_uses_public_info_fallback(self):
        client = FakeHTTPClient(
            [
                FakeResponse(404, "missing"),
                FakeResponse(200, json.dumps({"Version": "10.8.0"})),
            ]
        )
        http = JellyfinHTTP(client=client, sleep_fn=lambda seconds: None)

        result = http.wait_healthy(timeout_s=1)

        self.assertTrue(result["healthy"])
        self.assertEqual(result["endpoint_used"], "/System/Info/Public")
        self.assertEqual(client.requests[0]["url"], "http://localhost:8096/health")
        self.assertEqual(
            client.requests[1]["url"],
            "http://localhost:8096/System/Info/Public",
        )

    def test_complete_startup_wizard_treats_403_as_already_provisioned(self):
        client = FakeHTTPClient([FakeResponse(403, "already done")])
        http = JellyfinHTTP(client=client)

        result = http.complete_startup_wizard()

        self.assertFalse(result["provisioned"])
        self.assertTrue(result["already_provisioned"])
        self.assertEqual(len(client.requests), 1)

    def test_authenticate_stores_token(self):
        client = FakeHTTPClient(
            [
                FakeResponse(
                    200,
                    json.dumps(
                        {
                            "AccessToken": "token-1",
                            "User": {"Id": "user-1"},
                        }
                    ),
                )
            ]
        )
        http = JellyfinHTTP(client=client)

        result = http.authenticate("admin", "admin")

        self.assertTrue(result["success"])
        self.assertEqual(result["token"], "token-1")
        self.assertEqual(result["user_id"], "user-1")
        self.assertEqual(http.token, "token-1")
        self.assertEqual(
            client.requests[0]["headers"]["Authorization"],
            (
                'MediaBrowser Client="Jellyfin Auto Tester", '
                'Device="Stage2", DeviceId="jellyfin-auto-tester-stage2", Version="1.0"'
            ),
        )


if __name__ == "__main__":
    unittest.main()
