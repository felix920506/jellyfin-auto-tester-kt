import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creatures.execution.tools import docker_manager


class FakeImage:
    attrs = {"RepoDigests": ["jellyfin/jellyfin@sha256:abc"], "Size": 10485760}


class FakeImages:
    def __init__(self):
        self.pulled = []

    def pull(self, image):
        self.pulled.append(image)
        return FakeImage()


class FakeContainer:
    def __init__(self, container_id="container-1", name="jf-test"):
        self.id = container_id
        self.short_id = container_id[:12]
        self.name = name
        self.status = "running"
        self.attrs = {"Created": "2020-01-01T00:00:00Z"}
        self.removed = False
        self.stopped = False
        self.exec_calls = []

    def exec_run(self, command, stdout=True, stderr=True, demux=True):
        self.exec_calls.append(
            {
                "command": command,
                "stdout": stdout,
                "stderr": stderr,
                "demux": demux,
            }
        )
        return SimpleNamespace(exit_code=0, output=(b"out", b"err"))

    def logs(self, **kwargs):
        self.log_kwargs = kwargs
        return b"log line\n"

    def stop(self, timeout=10):
        self.stopped = True
        self.stop_timeout = timeout

    def remove(self, force=True):
        self.removed = True
        self.remove_force = force


class FakeContainers:
    def __init__(self):
        self.items = {}
        self.running = []
        self.run_calls = []
        self.fail_ports = set()

    def list(self, all=False, filters=None):
        return self.running

    def get(self, key):
        if key not in self.items:
            raise RuntimeError("not found")
        return self.items[key]

    def run(self, image, **kwargs):
        self.run_calls.append({"image": image, **kwargs})
        host_port = next(iter(kwargs["ports"].values()))
        if host_port in self.fail_ports:
            raise RuntimeError("port is already allocated")
        container = FakeContainer(container_id=f"container-{host_port}", name=kwargs["name"])
        self.items[container.id] = container
        self.items[container.name] = container
        return container


class FakeAPI:
    def inspect_container(self, container_id):
        return {"Id": container_id, "State": {"Status": "running"}}


class FakeClient:
    def __init__(self):
        self.images = FakeImages()
        self.containers = FakeContainers()
        self.api = FakeAPI()


class DockerManagerTests(unittest.TestCase):
    def test_pull_rejects_non_jellyfin_image(self):
        manager = docker_manager.DockerManager(client=FakeClient())

        with self.assertRaises(ValueError):
            manager.pull("ubuntu:latest", run_id="run")

    def test_pull_returns_digest_and_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeClient()
            manager = docker_manager.DockerManager(artifacts_root=temp_dir, client=client)

            result = manager.pull("jellyfin/jellyfin:10.9.7", run_id="run")

            self.assertEqual(result["digest"], "jellyfin/jellyfin@sha256:abc")
            self.assertEqual(result["size_mb"], 10.0)
            self.assertEqual(client.images.pulled, ["jellyfin/jellyfin:10.9.7"])
            self.assertTrue(Path(temp_dir, "run", "docker_ops.log").exists())

    def test_start_retries_fallback_port_and_maps_volumes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeClient()
            client.containers.fail_ports.add(8096)
            manager = docker_manager.DockerManager(artifacts_root=temp_dir, client=client)
            host_config = Path(temp_dir, "config")

            with patch.object(docker_manager, "_register_cleanup"):
                result = manager.start(
                    image="jellyfin/jellyfin:10.9.7",
                    ports={"host": 8096, "container": 8096},
                    volumes=[
                        {
                            "host": str(host_config),
                            "container": "/config",
                            "mode": "rw",
                        }
                    ],
                    env_vars={"JELLYFIN_LOG_LEVEL": "Debug"},
                    run_id="abcdef123456",
                )

            self.assertEqual(result["host_port"], 8097)
            self.assertEqual(result["name"], "jf-test-abcdef12")
            self.assertEqual(result["base_url"], "http://localhost:8097")
            self.assertTrue(host_config.exists())
            self.assertEqual(len(client.containers.run_calls), 2)
            second_call = client.containers.run_calls[1]
            self.assertEqual(second_call["ports"], {"8096/tcp": 8097})
            self.assertEqual(second_call["restart_policy"], {"Name": "no"})
            self.assertEqual(second_call["labels"]["jf-auto-tester"], "1")
            self.assertEqual(
                second_call["volumes"][str(host_config.resolve())],
                {"bind": "/config", "mode": "rw"},
            )

    def test_start_enforces_running_container_limit(self):
        client = FakeClient()
        client.containers.running = [FakeContainer("one"), FakeContainer("two")]
        manager = docker_manager.DockerManager(client=client)

        with self.assertRaises(RuntimeError):
            manager.start(
                image="jellyfin/jellyfin:10.9.7",
                ports={},
                volumes=[],
                env_vars={},
                run_id="abcdef12",
            )

    def test_exec_uses_shell_and_splits_demuxed_output(self):
        client = FakeClient()
        container = FakeContainer("container-1")
        client.containers.items["container-1"] = container
        manager = docker_manager.DockerManager(client=client)

        result = manager.exec("container-1", "ls /media", timeout_s=1, run_id="run")

        self.assertEqual(result["stdout"], "out")
        self.assertEqual(result["stderr"], "err")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(container.exec_calls[0]["command"], ["/bin/sh", "-lc", "ls /media"])

    def test_logs_stop_and_inspect(self):
        client = FakeClient()
        container = FakeContainer("container-1")
        client.containers.items["container-1"] = container
        manager = docker_manager.DockerManager(client=client)

        self.assertEqual(manager.logs("container-1", tail=10)["logs"], "log line\n")
        self.assertEqual(manager.inspect("container-1")["Id"], "container-1")
        self.assertEqual(manager.stop("container-1")["status"], "removed")
        self.assertTrue(container.stopped)
        self.assertTrue(container.removed)


if __name__ == "__main__":
    unittest.main()
