"""System-key discovery tests using fake subprocesses, with no Docker or HTTP."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
from types import SimpleNamespace
import unittest
from unittest import mock

import token_benchmark_system_keys as system_keys


class SystemKeyTests(unittest.TestCase):
    def setUp(self):
        self.service = {
            "Id": "service-test-id", "Name": "/sub2api", "State": {"Running": True},
            "Config": {"Env": ["DATABASE_HOST=postgres", "DATABASE_PORT=5432",
                                "DATABASE_USER=sub2api", "DATABASE_DBNAME=sub2api",
                                "DATABASE_PASSWORD=DO-NOT-PRINT-DATABASE-PASSWORD"]},
            "NetworkSettings": {
                "Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
                "Networks": {"sub2api_default": {"IPAddress": "172.19.0.2", "Aliases": ["sub2api"]}},
            },
        }
        self.database = {
            "Id": "postgres-test-id", "Name": "/sub2api-postgres", "State": {"Running": True},
            "Config": {"Env": ["POSTGRES_USER=sub2api", "POSTGRES_DB=sub2api",
                                "POSTGRES_PASSWORD=DO-NOT-PRINT-DATABASE-PASSWORD"]},
            "NetworkSettings": {"Ports": {"5432/tcp": None}, "Networks": {
                "sub2api_default": {"IPAddress": "172.19.0.3", "Aliases": ["postgres", "sub2api-postgres"]}}},
        }
        self.identities = [
            ("cline", "Cline", "Test", "sk-fcf", "cafd"),
            ("opencode", "OpenCode", "OpenCode", "sk-d01", "6c97"),
            ("commandcode", "commandcode", "cc", "sk-c58", "bbeb"),
        ]
        self.rows = []
        self.secrets = {}
        for index, (channel, name, group, prefix, suffix) in enumerate(self.identities, 1):
            self.secrets[channel] = prefix + "x" * 36 + suffix
            self.rows.append({
                "api_key_id": index + 20, "name": name, "user_id": 1,
                "group_id": index, "group_name": group, "platform": "openai",
                "status": "active", "user_status": "active", "group_status": "active",
                "user_deleted": False, "group_deleted": False, "expired": False,
                "quota_exhausted": False, "key_prefix": prefix, "key_suffix": suffix,
            })
        self.sql = []
        self.calls = []
        self.return_rows = None
        self.process_failure = None
        self.include_secrets = False
        self.patcher = mock.patch.object(system_keys.subprocess, "run", side_effect=self.fake_run)
        self.run = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        network = mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        self.socket = network.start()
        self.addCleanup(network.stop)

    def tearDown(self):
        self.socket.assert_not_called()

    def fake_run(self, args, **kwargs):
        self.calls.append((list(args), copy.deepcopy(kwargs)))
        self.assertIsInstance(args, list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(args[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
        command = args[3:]
        if self.process_failure is not None:
            return self.process_failure
        if command[0] == "inspect":
            name = command[-1]
            inspected = self.service if name == "sub2api" else self.database
            return SimpleNamespace(returncode=0, stdout=json.dumps([inspected]), stderr="")
        self.assertEqual(command[0], "exec")
        self.assertIn("psql", command)
        query = kwargs.get("input")
        self.assertIsInstance(query, str)
        self.sql.append(query)
        rows = copy.deepcopy(self.rows if self.return_rows is None else self.return_rows)
        if self.include_secrets:
            for row in rows:
                channel = next((entry[0] for entry in self.identities if entry[1] == row["name"]), "cline")
                row["key"] = self.secrets[channel]
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    def load(self, include_secrets=False, base_url="http://127.0.0.1:8080/v1"):
        self.include_secrets = include_secrets
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return system_keys.load_system_keys(base_url, include_secrets=include_secrets)

    def assert_no_secrets(self, value):
        serialized = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        for secret in self.secrets.values():
            self.assertNotIn(secret, serialized)
        self.assertNotIn("DO-NOT-PRINT-DATABASE-PASSWORD", serialized)

    def test_metadata_mode_matches_three_screenshot_masks_without_fetching_keys(self):
        self.rows.reverse()
        keys, metadata = self.load()
        self.assertEqual(keys, {})
        self.assertEqual(set(metadata["identities"]), {entry[0] for entry in self.identities})
        for channel, name, group, prefix, suffix in self.identities:
            identity = metadata["identities"][channel]
            self.assertEqual(identity["name"], name)
            self.assertEqual(identity["group_name"], group)
            self.assertEqual(identity["masked_key"], prefix + "..." + suffix)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.sql), 1)
        self.assert_no_secrets(metadata)
        self.assert_no_secrets(self.stdout.getvalue() + self.stderr.getvalue())
        # Selecting only a prefix/suffix is allowed; returning a full-key column
        # or constructing a full-key JSON field is forbidden in metadata mode.
        self.assertIsNone(re.search(r"\b(?:[a-z_]+\.)?key\s+as\s+[\"']?key\b", self.sql[0], re.I))
        self.assertIsNone(re.search(r"['\"]key['\"]\s*,\s*(?:[a-z_]+\.)?key\b", self.sql[0], re.I))

    def test_secret_mode_returns_keys_but_metadata_and_logs_stay_masked(self):
        keys, metadata = self.load(include_secrets=True)
        self.assertEqual(keys, self.secrets)
        self.assertEqual(len(set(keys.values())), 3)
        self.assert_no_secrets(metadata)
        self.assert_no_secrets(self.stdout.getvalue() + self.stderr.getvalue())
        for args, kwargs in self.calls:
            self.assert_no_secrets(args)
            self.assert_no_secrets(kwargs)

    def test_database_query_is_scoped_and_transaction_is_read_only(self):
        self.load()
        query = self.sql[0]
        invocation, options = self.calls[-1]
        self.assertIn("default_transaction_read_only=on", " ".join(invocation))
        self.assertIn("statement_timeout=", " ".join(invocation))
        self.assertIn("--no-password", invocation)
        self.assertIn("-X", invocation)
        self.assertTrue(options["capture_output"])
        self.assertTrue(options["text"])
        self.assertLessEqual(options["timeout"], 30)
        self.assertRegex(query, r"(?i)\bSELECT\b")
        self.assertRegex(query, r"(?i)\bWHERE\b")
        self.assertNotRegex(query, r"(?i)\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|COPY)\b")
        for _, name, _, _, _ in self.identities:
            self.assertIn(name.lower(), query.lower())
        self.assertRegex(query, r"(?i)LEFT\(k\.key,\s*6\)")
        self.assertRegex(query, r"(?i)RIGHT\(k\.key,\s*4\)")

    def test_remote_base_url_is_rejected_before_docker(self):
        with self.assertRaises(ValueError):
            self.load(base_url="https://api.example.com/v1")
        self.run.assert_not_called()

    def test_container_names_cannot_inject_docker_options(self):
        for service, database in (("--host=remote", "sub2api-postgres"),
                                  ("sub2api", "db; echo nope"), ("same", "same")):
            with self.subTest(service=service, database=database), self.assertRaises(ValueError):
                system_keys.load_system_keys("http://127.0.0.1:8080/v1", service, database)
        self.run.assert_not_called()

    def test_wrong_local_service_port_is_rejected_before_database_query(self):
        with self.assertRaises(ValueError):
            self.load(base_url="http://127.0.0.1:9999/v1")
        self.assertEqual(self.sql, [])

    def test_database_host_must_belong_to_inspected_container(self):
        self.service["Config"]["Env"][0] = "DATABASE_HOST=unrelated.example.test"
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.sql, [])

    def test_duplicate_or_missing_matching_keys_are_rejected(self):
        for rows in (self.rows[:-1], self.rows + [copy.deepcopy(self.rows[0])]):
            with self.subTest(count=len(rows)):
                self.return_rows = rows
                with self.assertRaises(ValueError) as error:
                    self.load()
                self.assert_no_secrets(str(error.exception))

    def test_inactive_deleted_expired_or_exhausted_keys_are_rejected(self):
        fields = {"status": "disabled", "user_status": "disabled", "group_status": "disabled",
                  "user_deleted": True, "group_deleted": True, "expired": True, "quota_exhausted": True}
        for field, value in fields.items():
            with self.subTest(field=field):
                self.return_rows = copy.deepcopy(self.rows)
                self.return_rows[0][field] = value
                with self.assertRaises(ValueError):
                    self.load()

    def test_mismatched_name_or_mask_is_rejected(self):
        for field, value in (("name", "unrelated"), ("key_prefix", "sk-fff"), ("key_suffix", "ffff")):
            with self.subTest(field=field):
                self.return_rows = copy.deepcopy(self.rows)
                self.return_rows[0][field] = value
                with self.assertRaises(ValueError):
                    self.load()

    def test_keys_from_different_users_are_rejected(self):
        self.rows[0]["user_id"] = 2
        with self.assertRaises(ValueError):
            self.load()

    def test_subprocess_errors_do_not_reveal_output_or_database_password(self):
        secret_text = "DO-NOT-PRINT-DATABASE-PASSWORD " + next(iter(self.secrets.values()))
        self.process_failure = SimpleNamespace(returncode=1, stdout=secret_text, stderr=secret_text)
        with self.assertRaises(ValueError) as error:
            self.load()
        self.assert_no_secrets(str(error.exception))
        self.assert_no_secrets(self.stdout.getvalue() + self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
