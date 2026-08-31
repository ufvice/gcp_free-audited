import hashlib
import os
import types
import unittest
from unittest import mock

from google.cloud import compute_v1

import gcp


class SecurityHardeningTests(unittest.TestCase):
    def test_traffic_limit_accepts_only_safe_free_tier_range(self):
        self.assertEqual(gcp.normalize_traffic_limit_gib("100"), 100)
        self.assertEqual(gcp.normalize_traffic_limit_gib(199), 199)
        self.assertIsNone(gcp.normalize_traffic_limit_gib("0"))
        self.assertIsNone(gcp.normalize_traffic_limit_gib("200"))
        self.assertIsNone(gcp.normalize_traffic_limit_gib("100; id"))

    def test_source_cidr_accepts_only_ipv4_and_normalizes_hosts(self):
        self.assertEqual(gcp.normalize_source_cidr("203.0.113.10"), "203.0.113.10/32")
        self.assertEqual(gcp.normalize_source_cidr("203.0.113.10/24"), "203.0.113.0/24")
        self.assertIsNone(gcp.normalize_source_cidr("0.0.0.0/0"))
        self.assertIsNone(gcp.normalize_source_cidr("::/0"))
        self.assertIsNone(gcp.normalize_source_cidr("not-an-ip"))

    def test_long_instance_names_get_distinct_tags(self):
        prefix = "a" * 60
        first = gcp.managed_network_tag(prefix + "-one")
        second = gcp.managed_network_tag(prefix + "-two")
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 63)

    def test_restricted_ssh_rules_are_tag_scoped(self):
        inserted = []
        instance = {
            "name": "vm-one",
            "network": "global/networks/default",
        }

        with mock.patch.object(
            gcp, "upsert_firewall_rule", side_effect=lambda _project, rule: inserted.append(rule) or True
        ):
            self.assertTrue(
                gcp.add_restricted_ssh_ingress(
                    "example-project", instance, "203.0.113.10/32"
                )
            )

        self.assertEqual(len(inserted), 2)
        deny_rule, allow_rule = inserted
        expected_tag = gcp.managed_network_tag("vm-one")
        self.assertEqual(allow_rule.target_tags, [expected_tag])
        self.assertEqual(allow_rule.source_ranges, ["203.0.113.10/32"])
        self.assertEqual(allow_rule.allowed[0].I_p_protocol, "tcp")
        self.assertEqual(allow_rule.allowed[0].ports, ["22"])
        self.assertEqual(deny_rule.target_tags, [expected_tag])
        self.assertEqual(deny_rule.source_ranges, ["0.0.0.0/0"])
        self.assertEqual(deny_rule.denied[0].I_p_protocol, "tcp")
        self.assertEqual(deny_rule.denied[0].ports, ["22"])
        self.assertLess(allow_rule.priority, deny_rule.priority)

    def test_remote_script_is_streamed_to_root_and_hash_checked(self):
        script_path = os.path.join(os.path.dirname(gcp.__file__), gcp.LOCAL_SCRIPT_PATHS["apt"])
        with open(script_path, "rb") as script_file:
            expected_hash = hashlib.sha256(script_file.read()).hexdigest()

        calls = []

        def fake_run(command, input=None):
            calls.append((command, input))
            return types.SimpleNamespace(returncode=0)

        instance = {
            "name": "vm",
            "zone": "us-west1-b",
            "external_ip": "203.0.113.20",
        }
        remote = {"method": "ssh", "user": "tester", "port": "22", "key": ""}
        with mock.patch.object(gcp.subprocess, "run", side_effect=fake_run):
            self.assertTrue(gcp.run_remote_script("example-project", instance, "apt", remote))

        self.assertEqual(len(calls), 1)
        command, streamed_bytes = calls[0]
        remote_command = command[-1]
        with open(script_path, "rb") as script_file:
            self.assertEqual(streamed_bytes, script_file.read())
        self.assertIn(expected_hash, remote_command)
        self.assertIn("sha256sum", remote_command)
        self.assertIn("sudo -- bash -c", remote_command)
        self.assertIn("mktemp /root/", remote_command)
        self.assertNotIn("raw.githubusercontent.com", remote_command)
        self.assertNotIn("curl", remote_command)

    def test_traffic_limit_is_passed_as_validated_environment(self):
        calls = []

        def fake_run(command, input=None):
            calls.append((command, input))
            return types.SimpleNamespace(returncode=0)

        instance = {
            "name": "vm",
            "zone": "us-west1-b",
            "external_ip": "203.0.113.20",
        }
        remote = {"method": "ssh", "user": "tester", "port": "22", "key": ""}
        with mock.patch.object(gcp.subprocess, "run", side_effect=fake_run):
            self.assertTrue(
                gcp.run_remote_script(
                    "example-project",
                    instance,
                    "net_shutdown",
                    remote,
                    traffic_limit_gib=100,
                )
            )

        self.assertIn("env GCP_FREE_TRAFFIC_LIMIT_GIB=100", calls[0][0][-1])

        with mock.patch.object(gcp.subprocess, "run") as run_mock:
            self.assertFalse(
                gcp.run_remote_script(
                    "example-project",
                    instance,
                    "net_shutdown",
                    remote,
                    traffic_limit_gib="100; id",
                )
            )
            run_mock.assert_not_called()

    def test_insert_firewall_rule_propagates_operation_error(self):
        firewall_client = mock.Mock()
        firewall_client.insert.return_value = types.SimpleNamespace(name="operation-1")
        operation_client = mock.Mock()
        operation_client.wait.return_value = types.SimpleNamespace(error="permission denied")
        rule = types.SimpleNamespace(name="rule-one")

        with mock.patch.object(gcp.compute_v1, "FirewallsClient", return_value=firewall_client), mock.patch.object(
            gcp.compute_v1, "GlobalOperationsClient", return_value=operation_client
        ):
            self.assertFalse(gcp.insert_firewall_rule("example-project", rule))

    def test_zonal_operation_error_is_raised(self):
        operation_client = mock.Mock()
        operation_client.wait.return_value = types.SimpleNamespace(error="quota failure")
        with mock.patch.object(
            gcp.compute_v1, "ZoneOperationsClient", return_value=operation_client
        ):
            with self.assertRaisesRegex(RuntimeError, "quota failure"):
                gcp.wait_for_operation("example-project", "us-west1-b", "operation-1")


if __name__ == "__main__":
    unittest.main()
