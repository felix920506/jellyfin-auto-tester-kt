import json
import tempfile
import unittest
from pathlib import Path

from creatures.execution.tools.jellyfin_api import JellyfinAPI


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


class JellyfinAPITests(unittest.TestCase):
    def test_request_builds_url_injects_token_and_logs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeHTTPClient([FakeResponse(204, "", {"X-Test": "yes"})])
            api = JellyfinAPI(
                base_url="http://localhost:8097",
                run_id="run",
                artifacts_root=temp_dir,
                client=client,
            )
            api.token = "token-1"

            result = api.request("POST", "/Library", body={"Name": "Movies"})

            self.assertEqual(result["status_code"], 204)
            self.assertEqual(client.requests[0]["url"], "http://localhost:8097/Library")
            self.assertEqual(client.requests[0]["json"], {"Name": "Movies"})
            self.assertEqual(client.requests[0]["headers"]["X-Emby-Token"], "token-1")
            self.assertTrue(Path(temp_dir, "run", "http_log.jsonl").exists())

    def test_request_retries_connection_refused(self):
        client = FakeHTTPClient(
            [
                ConnectionRefusedError("connection refused"),
                FakeResponse(200, "ok"),
            ]
        )
        api = JellyfinAPI(client=client, sleep_fn=lambda seconds: None)

        result = api.request("GET", "/health")

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(len(client.requests), 2)

    def test_wait_healthy_uses_public_info_fallback(self):
        client = FakeHTTPClient(
            [
                FakeResponse(404, "missing"),
                FakeResponse(200, json.dumps({"Version": "10.8.0"})),
            ]
        )
        api = JellyfinAPI(client=client, sleep_fn=lambda seconds: None)

        result = api.wait_healthy(timeout_s=1)

        self.assertTrue(result["healthy"])
        self.assertEqual(result["endpoint_used"], "/System/Info/Public")
        self.assertEqual(client.requests[0]["url"], "http://localhost:8096/health")
        self.assertEqual(
            client.requests[1]["url"],
            "http://localhost:8096/System/Info/Public",
        )

    def test_complete_startup_wizard_treats_403_as_already_provisioned(self):
        client = FakeHTTPClient([FakeResponse(403, "already done")])
        api = JellyfinAPI(client=client)

        result = api.complete_startup_wizard()

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
        api = JellyfinAPI(client=client)

        result = api.authenticate("admin", "admin")

        self.assertTrue(result["success"])
        self.assertEqual(result["token"], "token-1")
        self.assertEqual(result["user_id"], "user-1")
        self.assertEqual(api.token, "token-1")
        self.assertEqual(
            client.requests[0]["headers"]["Authorization"],
            (
                'MediaBrowser Client="Jellyfin Auto Tester", '
                'Device="Stage2", DeviceId="jellyfin-auto-tester-stage2", Version="1.0"'
            ),
        )


if __name__ == "__main__":
    unittest.main()
