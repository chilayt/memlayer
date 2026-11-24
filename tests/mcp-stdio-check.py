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
from mcp_stdio import MemlayerBridge, confirm_candidate, loopback_origin, main


def terminal_review(args, answer):
    stdin, stdout, stderr = io.StringIO(), io.StringIO(), io.StringIO()
    with (
        patch.object(stdin, "isatty", return_value=True),
        patch.object(stdout, "isatty", return_value=True),
        patch.multiple(sys, argv=args, stdin=stdin, stdout=stdout, stderr=stderr),
        patch("builtins.input", side_effect=answer) as prompt,
    ):
        status = main()
    return status, stdout.getvalue(), stderr.getvalue(), prompt.call_args


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
    blocked_targets = [
        seed("other-project", "p_else", "author-note"),
        seed("other-subject", "p_mcp_test", "private-note"),
        seed("unconfirmed", "p_mcp_test", "author-note", confirmed=False),
        seed("inferred", "p_mcp_test", "author-note", perspective="inferred"),
    ]
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
        current_id = visible[0]["id"]

        # The listing filter also matches references in other facts. Exercise a target
        # beyond the first page so terminal review can locate an older rule by ID.
        with service.lock:
            for index in range(21):
                service.store.append({
                    "event_id": f"review-filter-noise-{index}",
                    "project": bridge.project,
                    "subject": bridge.subject,
                    "text": f"Reference noise {index} for {current_id}",
                    "at": "2099-12-31T00:00:00+00:00",
                })
        assert bridge.current_fact(current_id)["text"] == statement

        async def propose(text, source):
            proposal = await call("propose_memory", {"text": text, "source_ref": source})
            assert proposal["status"] == "pending"
            return proposal["candidate_id"]

        correction = "FixtureRevisedZ16: use the silver marker for the archive."
        replacement = await propose(correction, "fixture-correction")
        assert (await call("recall_memory", {"query": "FixtureRevisedZ16"}))["facts"] == []
        for wrong_id in blocked_targets:
            status, preview, error, prompt = terminal_review(
                [*args, "approve", replacement, "--replace", wrong_id], AssertionError("Unexpected prompt")
            )
            assert status == 2 and "configured project and subject" in error and not preview and prompt is None
        command = [*args, "approve", replacement, "--replace", current_id]
        approval = f"REPLACE {replacement} {current_id}"
        piped = subprocess.run(
            [sys.executable, "-B", *command], input=approval + "\n", text=True, capture_output=True, timeout=10,
        )
        assert piped.returncode == 2 and "interactive terminal" in piped.stderr
        status, preview, error, prompt = terminal_review(command, [f"APPROVE {replacement}"])
        assert status == 1 and not error and approval in prompt.args[0]
        assert correction in preview and statement in preview
        assert (await call("recall_memory", {"query": "FixtureGateM51"}))["facts"][0]["id"] == current_id
        status, _, error, _ = terminal_review(command, [approval])
        assert status == 0 and not error, error
        assert (await call("recall_memory", {"query": "FixtureGateM51"}))["facts"] == []
        revised = (await call("recall_memory", {"query": "FixtureRevisedZ16"}))["facts"][0]
        assert revised["text"] == correction and revised["source_ids"] == ["fixture-correction"]
        with service.lock:
            assert service.store.get_fact(current_id)["status"] == "superseded"

        limited = "FixtureExceptionT24: for the annual night audit only, use the violet marker."
        exception_id = await propose(limited, "fixture-exception")
        assert (await call("recall_memory", {"query": "FixtureExceptionT24"}))["facts"] == []
        status, preview, error, prompt = terminal_review(
            [*args, "approve", exception_id, "--exception", revised["id"]],
            [f"EXCEPTION {exception_id} {revised['id']}"],
        )
        assert status == 0 and not error, error
        assert limited in preview and correction in preview and prompt.args[0].startswith("Type EXCEPTION ")
        active = (await call("recall_memory", {"query": "FixtureExceptionT24"}))["facts"][0]
        assert correction in active["text"] and limited in active["text"]
        assert set(active["source_ids"]) == {"fixture-correction", "fixture-exception"}
        assert (await call("recall_memory", {"query": "FixtureRevisedZ16"}))["facts"][0]["id"] == active["id"]

        edited = await propose("FixtureEditedN38: use a second conditional marker.", "fixture-edit")
        with service.lock:
            active_path = service.store._path(service.store.get_fact(active["id"]))
            original_markdown = active_path.read_text(encoding="utf-8")

        for rebuild in (False, True):
            def edit_while_reviewing(_prompt):
                with service.lock:
                    active_path.write_text(original_markdown + "\nSynthetic edited wording.\n", encoding="utf-8")
                    if rebuild:
                        service.store.rebuild()
                return f"EXCEPTION {edited} {active['id']}"

            status, preview, error, _ = terminal_review(
                [*args, "approve", edited, "--exception", active["id"]], edit_while_reviewing,
            )
            assert status == 2 and "Review both again" in error
            assert "Synthetic edited wording" not in preview
            assert any(row["id"] == edited for row in bridge.pending())
            assert (await call("recall_memory", {"query": "FixtureEditedN38"}))["facts"] == []
            with service.lock:
                active_path.write_text(original_markdown, encoding="utf-8")
                if rebuild:
                    service.store.rebuild()

        stale = await propose("FixtureStaleX44: disagree with the current rule.", "fixture-stale")

        def withdraw_while_reviewing(_prompt):
            with service.lock:
                service.store.retract(active["id"], "synthetic concurrent withdrawal")
            return f"REPLACE {stale} {active['id']}"

        status, _, error, _ = terminal_review(
            [*args, "approve", stale, "--replace", active["id"]], withdraw_while_reviewing,
        )
        assert status == 2 and "Review both again" in error
        assert any(row["id"] == stale for row in bridge.pending())
        assert (await call("recall_memory", {"query": "FixtureStaleX44"}))["facts"] == []
        assert (await call("recall_memory", {"query": "FixtureExceptionT24"}))["facts"] == []
    print("PASS stdio discovery, scope, idempotency, interactive add/replace/exception, and stale review after edit or withdrawal")


if __name__ == "__main__":
    asyncio.run(run_checks())
