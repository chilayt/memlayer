"""Exercise the adapter through a real MCP stdio subprocess and an isolated bridge."""
from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "memlayer"))
from bridge import BridgeService
from mcp_stdio import MemlayerBridge, confirm_candidate, loopback_origin


async def run_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="memlayer-mcp-test-") as tmp:
        service = BridgeService(Path(tmp) / "store", port=0).start()
        try:
            await verify(service)
        finally:
            service.close()


async def verify(service: BridgeService) -> None:
    args = [str(ROOT / "memlayer" / "mcp_stdio.py"), "--bridge", service.url, "--project", "p_mcp_test"]
    bridge = MemlayerBridge(service.url, "p_mcp_test", "author-note")

    def seed(identifier, project, subject, *, confirmed=True, perspective="self"):
        with service.lock:
            return service.store.append({
                "event_id": identifier,
                "project": project,
                "subject": subject,
                "text": f"AnchorQ77 synthetic row {identifier}",
                "confirmed": confirmed,
                "perspective": perspective,
            })["fact_id"]

    known = seed("allowed", "p_mcp_test", "author-note")
    seed("other-project", "p_else", "author-note")
    seed("other-subject", "p_mcp_test", "private-note")
    seed("unconfirmed", "p_mcp_test", "author-note", confirmed=False)
    seed("inferred", "p_mcp_test", "author-note", perspective="inferred")
    for bad_address in (
        "http://outside.invalid:8770",
        "http://name@127.0.0.1:8770",
        "http://127.0.0.1:8770/elsewhere",
        "https://127.0.0.1:8770",
    ):
        try:
            loopback_origin(bad_address)
        except ValueError:
            pass
        else:
            raise AssertionError("Unexpected non-local bridge origin accepted")

    params = StdioServerParameters(command=sys.executable, args=["-B", *args, "serve"])
    async with Client(params, read_timeout_seconds=10) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {"recall_memory", "propose_memory"}
        assert next(tool for tool in tools if tool.name == "recall_memory").annotations.read_only_hint is True

        async def call(name, arguments):
            result = await client.call_tool(name, arguments)
            assert not result.is_error, result.content
            assert isinstance(result.structured_content, dict), result.content
            return result.structured_content

        rows = (await call("recall_memory", {"query": "AnchorQ77", "limit": 10}))["facts"]
        assert [row["id"] for row in rows] == [known], rows
        bad_query = await client.call_tool("recall_memory", {"query": " ", "limit": 1})
        assert bad_query.is_error
        punct = await client.call_tool("recall_memory", {"query": "?!", "limit": 1})
        assert punct.is_error
        bad_write = await client.call_tool("propose_memory", {"text": "x" * 2001})
        assert bad_write.is_error

        statement = "FixtureGateM51 synthetic preference: use the amber marker."
        proposal = await call("propose_memory", {"text": statement})
        candidate_id = proposal["candidate_id"]
        assert proposal["status"] == "pending" and proposal["awaiting_confirmation"]
        retry = await call("propose_memory", {"text": statement})
        assert retry["candidate_id"] == candidate_id and not retry["created"]
        assert (await call("recall_memory", {"query": "FixtureGateM51"}))["facts"] == []

        piped = subprocess.run(
            [sys.executable, "-B", *args, "approve", candidate_id],
            input=f"APPROVE {candidate_id}\n", text=True, capture_output=True, timeout=10,
        )
        assert piped.returncode == 2 and "interactive terminal" in piped.stderr
        assert bridge.pending()[0]["id"] == candidate_id

        fake_stdin, fake_stdout = io.StringIO(), io.StringIO()
        with (
            patch.object(fake_stdin, "isatty", return_value=True),
            patch.object(fake_stdout, "isatty", return_value=True),
            patch.multiple(sys, stdin=fake_stdin, stdout=fake_stdout),
            patch("builtins.input", side_effect=["CANCEL", f"APPROVE {candidate_id}"]),
        ):
            assert not confirm_candidate(bridge, candidate_id)
            assert bridge.pending()[0]["id"] == candidate_id
            assert confirm_candidate(bridge, candidate_id)
        assert statement in fake_stdout.getvalue()
        assert bridge.pending() == []

        visible = (await call("recall_memory", {"query": "FixtureGateM51"}))["facts"]
        assert len(visible) == 1 and visible[0]["text"] == statement
        with service.lock:
            service.store.retract(visible[0]["id"], "synthetic test finished")
        assert (await call("recall_memory", {"query": "FixtureGateM51"}))["facts"] == []
    print("PASS stdio discovery, fixed scope, input validation, idempotent proposal, interactive approval, and retraction")


if __name__ == "__main__":
    asyncio.run(run_checks())
