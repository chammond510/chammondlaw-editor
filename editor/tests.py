from unittest.mock import patch
from io import BytesIO
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from docx import Document as DocxDocument

from .agent_service import (
    AGENT_FINALIZATION_MAX_OUTPUT_TOKENS,
    AGENT_FINALIZATION_REASONING_EFFORT,
    AgentConfigurationError,
    DocumentResearchAgent,
    _extract_output_text,
    _extract_hosted_tool_calls,
    _knowledge_function_tools,
    _normalize_mcp_server_url,
)
from .models import (
    Document,
    DocumentResearchMessage,
    DocumentResearchRun,
    DocumentResearchSession,
    DocumentType,
)


def _sample_tiptap(text):
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                    }
                ],
            }
        ],
    }


def _sample_cover_letter():
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Via FedEx"}]},
            {"type": "paragraph", "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "March 7, 2026"}]},
            {"type": "paragraph", "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "U.S. Citizenship and Immigration Services"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Dallas Lockbox"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "P.O. Box 660867"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Dallas, TX 75266"}]},
            {"type": "paragraph", "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "RE: Form I-130, Petition for Alien Relative"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Petitioner: Jane Doe"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Beneficiary: John Doe"}]},
            {"type": "paragraph", "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": "Dear USCIS Officer:"}]},
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Please find enclosed this Form I-130 filing and supporting documentation."}],
            },
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "A. Case Summary"}]},
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "The petitioner is a U.S. citizen seeking classification for her spouse."}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Please find enclosed the following documents in support of this filing:"}],
            },
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Forms"}]},
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Form I-130, Petition for Alien Relative"}]}]},
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Form G-28, Notice of Entry of Appearance"}]}]},
                ],
            },
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Supporting Evidence"}]},
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Marriage certificate"}]}]},
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Petitioner passport copy"}]}]},
                ],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "Respectfully submitted,"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Chris Hammond"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Attorney for Petitioner"}]},
        ],
    }


class AgentResearchViewsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lawyer", password="secret")
        self.client.force_login(self.user)
        self.document_type = DocumentType.objects.create(
            name="Defensive Asylum Brief",
            slug="defensive-asylum-brief",
            category="brief",
            template_content=_sample_tiptap("Template"),
        )
        self.document = Document.objects.create(
            title="Test Brief",
            document_type=self.document_type,
            content=_sample_tiptap("The client fears return because gang threats escalated after reporting extortion."),
            created_by=self.user,
        )

    def test_agent_session_bootstraps_empty_session(self):
        response = self.client.get(
            reverse("research_agent_session", kwargs={"doc_id": self.document.id})
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["messages"], [])
        self.assertTrue(DocumentResearchSession.objects.filter(document=self.document, user=self.user).exists())

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_chat_starts_background_run_and_persists_user_message(self, agent_cls):
        agent = agent_cls.return_value

        def fake_start_chat_run(*, run, **kwargs):
            run.status = "in_progress"
            run.stage = "waiting_openai"
            run.response_id = "resp_test_123"
            run.response_count = 1
            run.save(update_fields=["status", "stage", "response_id", "response_count", "updated_at"])
            return run

        agent.start_chat_run.side_effect = fake_start_chat_run

        response = self.client.post(
            reverse("research_agent_chat", kwargs={"doc_id": self.document.id}),
            data={
                "message": "What precedent should I add here?",
                "selected_text": "The client fears return because gang threats escalated.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["user_message"]["content"], "What precedent should I add here?")
        self.assertEqual(payload["run"]["status"], "in_progress")
        self.assertEqual(payload["run"]["response_id"], "resp_test_123")

        session = DocumentResearchSession.objects.get(document=self.document, user=self.user)
        self.assertEqual(session.last_response_id, "")
        self.assertEqual(session.messages.count(), 1)
        self.assertTrue(DocumentResearchRun.objects.filter(session=session, response_id="resp_test_123").exists())

    def test_agent_chat_returns_conflict_when_run_already_active(self):
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="in_progress",
            stage="waiting_openai",
        )

        response = self.client.post(
            reverse("research_agent_chat", kwargs={"doc_id": self.document.id}),
            data={"message": "Can you help with this paragraph?"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["run"]["id"], str(run.public_id))

    def test_agent_reset_clears_messages(self):
        session = DocumentResearchSession.objects.create(
            document=self.document,
            user=self.user,
            last_response_id="resp_old",
        )
        DocumentResearchMessage.objects.create(session=session, role="user", content="Question")
        DocumentResearchMessage.objects.create(session=session, role="assistant", content="Answer", response_id="resp_old")

        response = self.client.post(
            reverse("research_agent_reset", kwargs={"doc_id": self.document.id})
        )

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.last_response_id, "")
        self.assertEqual(session.messages.count(), 0)

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_chat_returns_json_for_unexpected_exception(self, agent_cls):
        agent_cls.return_value.start_chat_run.side_effect = RuntimeError("boom")

        response = self.client.post(
            reverse("research_agent_chat", kwargs={"doc_id": self.document.id}),
            data={"message": "Help me with this paragraph."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["error"],
            "The agent failed unexpectedly while starting.",
        )

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_chat_returns_json_for_constructor_configuration_error(self, agent_cls):
        agent_cls.side_effect = AgentConfigurationError("OPENAI_API_KEY is not configured.")

        response = self.client.post(
            reverse("research_agent_chat", kwargs={"doc_id": self.document.id}),
            data={"message": "Help me with this paragraph."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"],
            "OPENAI_API_KEY is not configured.",
        )

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_run_status_persists_completed_chat_message(self, agent_cls):
        agent = agent_cls.return_value
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        user_message = DocumentResearchMessage.objects.create(
            session=session,
            role="user",
            content="Can you strengthen this section?",
            selection_text="The client reported gang extortion to police.",
        )
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="in_progress",
            stage="waiting_openai",
            user_message=user_message,
        )

        def fake_advance_run(*, run):
            run.status = "completed"
            run.stage = "completed"
            run.response_id = "resp_chat_done"
            run.result_payload = {
                "answer": "Matter of C-T-L- supports the nexus rule here.",
                "response_id": "resp_chat_done",
                "tool_calls": [{"source": "biaedge", "type": "mcp_call", "name": "search_cases"}],
                "citations": [],
                "used_tools": ["biaedge"],
                "metadata": {"model": "gpt-5.4"},
            }
            run.save(update_fields=["status", "stage", "response_id", "result_payload", "updated_at"])
            return run

        agent.advance_run.side_effect = fake_advance_run

        response = self.client.get(
            reverse("research_agent_run", kwargs={"run_id": run.public_id})
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run"]["status"], "completed")
        self.assertEqual(payload["assistant_message"]["content"], "Matter of C-T-L- supports the nexus rule here.")

        run.refresh_from_db()
        session.refresh_from_db()
        self.assertIsNotNone(run.assistant_message_id)
        self.assertEqual(session.last_response_id, "resp_chat_done")
        self.assertTrue(
            DocumentResearchMessage.objects.filter(session=session, role="assistant", response_id="resp_chat_done").exists()
        )

    @patch("editor.agent_views._persist_chat_completion", side_effect=RuntimeError("persist boom"))
    def test_agent_run_status_returns_fallback_chat_message_when_persist_fails(self, persist_completion):
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        user_message = DocumentResearchMessage.objects.create(
            session=session,
            role="user",
            content="Can you strengthen this section?",
            selection_text="The client reported gang extortion to police.",
        )
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="completed",
            stage="completed",
            user_message=user_message,
            response_id="resp_chat_done",
            result_payload={
                "answer": "Matter of C-T-L- supports the nexus rule here.",
                "response_id": "resp_chat_done",
                "tool_calls": [{"source": "biaedge", "type": "mcp_call", "name": "search_cases"}],
                "citations": [],
                "used_tools": ["biaedge"],
                "metadata": {"model": "gpt-5.4"},
            },
        )

        response = self.client.get(
            reverse("research_agent_run", kwargs={"run_id": run.public_id})
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run"]["status"], "completed")
        self.assertEqual(payload["assistant_message"]["content"], "Matter of C-T-L- supports the nexus rule here.")
        self.assertEqual(payload["assistant_message"]["response_id"], "resp_chat_done")
        persist_completion.assert_called_once()

        run.refresh_from_db()
        self.assertTrue(run.metadata.get("assistant_persist_failed"))
        self.assertIsNone(run.assistant_message_id)

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_suggest_returns_json_for_constructor_configuration_error(self, agent_cls):
        agent_cls.side_effect = AgentConfigurationError("OPENAI_API_KEY is not configured.")

        response = self.client.post(
            reverse("research_agent_suggest", kwargs={"doc_id": self.document.id}),
            data={"selected_text": "Selected text"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"],
            "OPENAI_API_KEY is not configured.",
        )

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_suggest_starts_background_run(self, agent_cls):
        agent = agent_cls.return_value

        def fake_start_suggest_run(*, run, **kwargs):
            run.status = "in_progress"
            run.stage = "waiting_openai"
            run.response_id = "resp_suggest_1"
            run.response_count = 1
            run.save(update_fields=["status", "stage", "response_id", "response_count", "updated_at"])
            return run

        agent.start_suggest_run.side_effect = fake_start_suggest_run

        response = self.client.post(
            reverse("research_agent_suggest", kwargs={"doc_id": self.document.id}),
            data={
                "selected_text": "The threats were because he reported gang extortion.",
                "focus_note": "Find the best nexus authority.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["run"]["status"], "in_progress")
        self.assertEqual(payload["run"]["response_id"], "resp_suggest_1")

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_run_status_returns_completed_suggest_payload(self, agent_cls):
        agent = agent_cls.return_value
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="suggest",
            status="in_progress",
            stage="waiting_openai",
        )

        def fake_advance_run(*, run):
            run.status = "completed"
            run.stage = "completed"
            run.response_id = "resp_suggest_done"
            run.result_payload = {
                "selection_summary": "Nexus support for gang-based persecution.",
                "draft_gap": "The paragraph needs controlling nexus authority and one central reason language.",
                "authorities": [
                    {
                        "kind": "case",
                        "title": "Matter of C-T-L-",
                        "citation": "25 I&N Dec. 341 (BIA 2010)",
                        "document_id": 101,
                        "reference_id": None,
                        "precedential_status": "precedential",
                        "validity_status": "good_law",
                        "relevance": "Explains the one central reason standard.",
                        "suggested_use": "Use it to frame the nexus paragraph.",
                        "pinpoint": "One protected ground must be one central reason for the harm.",
                    }
                ],
                "search_notes": "Searched precedential BIA nexus authorities.",
                "next_questions": ["Do you also need PSG-specific authority?"],
                "tool_calls": [{"source": "biaedge", "name": "search_cases", "type": "mcp_call"}],
                "citations": [],
                "raw_answer": "{}",
            }
            run.save(update_fields=["status", "stage", "response_id", "result_payload", "updated_at"])
            return run

        agent.advance_run.side_effect = fake_advance_run

        response = self.client.get(
            reverse("research_agent_run", kwargs={"run_id": run.public_id})
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run"]["status"], "completed")
        self.assertEqual(payload["suggest_result"]["selection_summary"], "Nexus support for gang-based persecution.")
        self.assertEqual(payload["suggest_result"]["authorities"][0]["citation"], "25 I&N Dec. 341 (BIA 2010)")


class AgentServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="agent_service_user", password="secret")
        self.document_type = DocumentType.objects.create(
            name="Agent Service Brief",
            slug="agent-service-brief",
            category="brief",
            template_content=_sample_tiptap("Template"),
        )
        self.document = Document.objects.create(
            title="Agent Service Test Document",
            document_type=self.document_type,
            content=_sample_tiptap("The client reported gang extortion to police."),
            created_by=self.user,
        )

    def test_knowledge_function_tools_use_valid_strict_required_lists(self):
        tools = {
            tool["name"]: tool
            for tool in _knowledge_function_tools()
        }

        search_schema = tools["search_exemplars"]["parameters"]
        self.assertEqual(
            search_schema["required"],
            ["query", "limit", "document_type_slug"],
        )
        self.assertEqual(
            sorted(search_schema["properties"].keys()),
            sorted(search_schema["required"]),
        )

    @patch("editor.agent_service._new_openai_client")
    def test_build_tools_omits_knowledge_functions_without_active_exemplars(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )

        tools = agent._build_tools(mode="chat", include_mcp=False)

        self.assertFalse(any(tool.get("type") == "function" for tool in tools))

    @patch("editor.agent_service._new_openai_client")
    def test_run_response_loop_forces_final_answer_after_tool_only_round(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )

        first_response = SimpleNamespace(
            id="resp_tool_only",
            status="completed",
            output_text="",
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="search_exemplars",
                    status="completed",
                    call_id="call_123",
                    arguments='{"query":"nexus brief","limit":3,"document_type_slug":""}',
                )
            ],
        )
        final_response = SimpleNamespace(
            id="resp_final",
            status="completed",
            output_text="Matter of C-T-L- supports the nexus rule.",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            text="Matter of C-T-L- supports the nexus rule.",
                            annotations=[],
                        )
                    ],
                )
            ],
        )

        with patch("editor.agent_service._MAX_FUNCTION_ROUNDS", 1):
            with patch.object(agent, "_create_response", side_effect=[first_response, final_response]) as create_response:
                result = agent._run_response_loop(
                    instructions="Test instructions",
                    input_payload="Test input",
                    tools=[{"type": "function", "name": "search_exemplars"}],
                    previous_response_id=None,
                    initial_tool_choice="auto",
                )

        self.assertEqual(result["answer"], "Matter of C-T-L- supports the nexus rule.")
        self.assertEqual(result["response_id"], "resp_final")
        self.assertEqual(create_response.call_count, 2)
        forced_request = create_response.call_args_list[1].kwargs
        self.assertEqual(forced_request["tools"], [])
        self.assertEqual(forced_request["previous_response_id"], "resp_tool_only")
        self.assertEqual(forced_request["tool_choice"], "none")
        self.assertEqual(forced_request["max_output_tokens"], AGENT_FINALIZATION_MAX_OUTPUT_TOKENS)
        self.assertEqual(forced_request["reasoning_effort"], AGENT_FINALIZATION_REASONING_EFFORT)

    def test_extract_output_text_reads_refusal_parts(self):
        response = SimpleNamespace(
            output_text="",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="refusal",
                            refusal="I can’t comply with that request.",
                        )
                    ],
                )
            ],
        )

        self.assertEqual(_extract_output_text(response), "I can’t comply with that request.")

    def test_extract_hosted_tool_calls_captures_mcp_output_excerpt(self):
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="mcp_call",
                    name="get_reference",
                    status="completed",
                    arguments='{"reference_id": 324, "max_chars": 12000}',
                    output='{"title":"USCIS Policy Manual","text":"Generally, a CPR who initially files a waiver under one waiver filing basis may change to or add another waiver filing basis by making the request in writing."}',
                    error="",
                )
            ]
        )

        tool_calls = _extract_hosted_tool_calls(response)

        self.assertEqual(tool_calls[0]["name"], "get_reference")
        self.assertIn("output_excerpt", tool_calls[0])
        self.assertIn("waiver filing basis", tool_calls[0]["output_excerpt"])

    def test_normalize_mcp_server_url_adds_scheme_and_default_path(self):
        self.assertEqual(
            _normalize_mcp_server_url("biaedge-mcp.onrender.com"),
            "https://biaedge-mcp.onrender.com/mcp",
        )
        self.assertEqual(
            _normalize_mcp_server_url("https://biaedge-mcp.onrender.com"),
            "https://biaedge-mcp.onrender.com/mcp",
        )
        self.assertEqual(
            _normalize_mcp_server_url("http://localhost:8001"),
            "http://localhost:8001/mcp",
        )

    @patch("editor.agent_service._new_openai_client")
    def test_failed_status_with_tool_budget_attempts_final_recovery(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="in_progress",
            stage="waiting_openai",
            response_id="resp_failed",
            tool_calls=[
                {"source": "biaedge", "name": f"search_cases_{index}", "type": "mcp_call"}
                for index in range(24)
            ],
        )
        failed_response = SimpleNamespace(
            id="resp_failed",
            status="failed",
            error=None,
            incomplete_details=None,
            usage=None,
            output=[],
        )
        recovery_response = SimpleNamespace(
            id="resp_recover",
            status="queued",
            error=None,
            usage=None,
            output=[],
        )

        agent.client.responses.retrieve = lambda *args, **kwargs: failed_response
        with patch.object(agent, "_create_background_response", return_value=recovery_response) as create_background:
            updated = agent.advance_run(run=run)

        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.stage, "recovering_failure")
        self.assertEqual(updated.response_id, "resp_recover")
        recovery_request = create_background.call_args.kwargs
        self.assertEqual(recovery_request["tool_choice"], "none")
        self.assertEqual(recovery_request["max_output_tokens"], AGENT_FINALIZATION_MAX_OUTPUT_TOKENS)
        self.assertEqual(recovery_request["reasoning_effort"], AGENT_FINALIZATION_REASONING_EFFORT)

    @patch("editor.agent_service._new_openai_client")
    def test_queue_force_final_response_uses_compacted_tool_digest_without_previous_chain(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="in_progress",
            stage="waiting_openai",
            response_id="resp_current",
            request_payload={
                "message": "Find quotes for this paragraph.",
                "selected_text": "Selected paragraph text.",
            },
            tool_calls=[
                {
                    "source": "biaedge",
                    "type": "mcp_call",
                    "name": "get_reference",
                    "status": "completed",
                    "arguments": {"reference_id": 324},
                    "output_excerpt": '{"title":"USCIS Policy Manual","text":"A CPR may change to or add another waiver filing basis by making the request in writing."}',
                }
            ],
        )
        current_response = SimpleNamespace(id="resp_current")
        queued_response = SimpleNamespace(id="resp_final", status="queued", error=None, usage=None, output=[])

        with patch.object(agent, "_create_background_response", return_value=queued_response) as create_background:
            updated = agent._queue_force_final_response(run=run, response=current_response)

        self.assertEqual(updated.response_id, "resp_final")
        request = create_background.call_args.kwargs
        self.assertIsNone(request["previous_response_id"])
        self.assertEqual(request["tool_choice"], "none")
        self.assertIn("Verified research results from tools:", request["input_payload"])
        self.assertIn("waiver filing basis", request["input_payload"])

    @patch("editor.agent_service._new_openai_client")
    def test_failed_status_with_tool_budget_has_clearer_error_message(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="in_progress",
            stage="waiting_openai",
            response_id="resp_failed",
            tool_calls=[
                {"source": "biaedge", "name": f"search_cases_{index}", "type": "mcp_call"}
                for index in range(24)
            ],
            metadata={"failed_recovery_attempted": True},
        )
        failed_response = SimpleNamespace(
            id="resp_failed",
            status="failed",
            error=None,
            incomplete_details=None,
            usage=None,
            output=[],
        )

        agent.client.responses.retrieve = lambda *args, **kwargs: failed_response
        updated = agent.advance_run(run=run)

        self.assertEqual(updated.status, "failed")
        self.assertIn("tool-call budget", updated.error_message)

    @patch("editor.agent_service._new_openai_client")
    def test_incomplete_without_answer_forces_final_response_instead_of_more_continuations(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="in_progress",
            stage="waiting_openai",
            response_id="resp_incomplete",
            tool_calls=[{"source": "biaedge", "name": "search_cases", "type": "mcp_call"}],
        )
        incomplete_response = SimpleNamespace(
            id="resp_incomplete",
            status="incomplete",
            error=None,
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output_text="",
            output=[],
        )

        with patch.object(agent, "_queue_force_final_response", return_value=run) as force_final:
            updated = agent._continue_incomplete_response(run=run, response=incomplete_response)

        self.assertIs(updated, run)
        force_final.assert_called_once_with(run=run, response=incomplete_response)

    @patch("editor.agent_service._new_openai_client")
    def test_advance_run_reassembles_partial_answer_across_continuation(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="in_progress",
            stage="waiting_openai",
            response_id="resp_partial",
            metadata=agent._initial_run_metadata(mode="chat", previous_response_id=""),
        )

        partial_response = SimpleNamespace(
            id="resp_partial",
            status="incomplete",
            error=None,
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output_text="Beginning of the answer. ",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            text="Beginning of the answer. ",
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=None,
        )
        queued_response = SimpleNamespace(
            id="resp_tail",
            status="queued",
            error=None,
            usage=None,
            output=[],
        )
        tail_response = SimpleNamespace(
            id="resp_tail",
            status="completed",
            error=None,
            output_text="End of the answer.",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            text="End of the answer.",
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=None,
        )

        def retrieve(response_id, **kwargs):
            if response_id == "resp_partial":
                return partial_response
            if response_id == "resp_tail":
                return tail_response
            raise AssertionError(f"Unexpected response id: {response_id}")

        agent.client.responses.retrieve = retrieve

        with patch.object(agent, "_build_tools", return_value=[]):
            with patch.object(agent, "_create_background_response", return_value=queued_response):
                updated = agent.advance_run(run=run)

        self.assertEqual(updated.response_id, "resp_tail")
        self.assertEqual(updated.status, "queued")

        updated.refresh_from_db()
        completed = agent.advance_run(run=updated)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            completed.result_payload["answer"],
            "Beginning of the answer. End of the answer.",
        )


class CoverLetterExportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="exporter", password="secret")
        self.client.force_login(self.user)
        self.document_type = DocumentType.objects.create(
            name="I-130 Cover Letter",
            slug="i-130-cover-letter-test",
            category="cover_letter",
            export_format="cover_letter",
            template_content=_sample_cover_letter(),
        )
        self.document = Document.objects.create(
            title="I-130 Cover Letter",
            document_type=self.document_type,
            content=_sample_cover_letter(),
            created_by=self.user,
        )

    def test_docx_export_uses_cover_letter_style_anchor(self):
        response = self.client.get(reverse("export_docx", kwargs={"doc_id": self.document.id}))

        self.assertEqual(response.status_code, 200)
        exported = DocxDocument(BytesIO(response.content))
        non_empty = [p.text.strip() for p in exported.paragraphs if p.text.strip()]

        self.assertGreaterEqual(len(non_empty), 10)
        self.assertEqual(non_empty[0], "Chris Hammond Law Firm")
        self.assertIn("Immigration Attorney", non_empty[:6])
        self.assertIn("RE:\tForm I-130, Petition for Alien Relative", non_empty)
        self.assertIn("Respectfully submitted,", non_empty)
        self.assertIn("Christopher Hammond, Esq.", non_empty)
        self.assertGreaterEqual(len(exported.tables), 2)
        self.assertEqual(exported.tables[0].cell(0, 0).text.strip(), "EXHIBIT 1")
        self.assertEqual(exported.tables[0].cell(1, 0).text.strip(), "EXHIBIT 2")


class SeedTemplatesTests(TestCase):
    def test_i751_templates_seed_as_three_distinct_cover_letters(self):
        call_command("seed_templates")

        templates = {
            doc_type.slug: doc_type
            for doc_type in DocumentType.objects.filter(slug__icontains="i-751")
        }

        self.assertEqual(
            sorted(templates.keys()),
            [
                "i-751-change-joint-to-waiver-cover-letter",
                "i-751-cover-letter",
                "i-751-waiver-cover-letter",
            ],
        )
        self.assertEqual(templates["i-751-cover-letter"].name, "I-751 Joint Filing Cover Letter")
        self.assertEqual(templates["i-751-waiver-cover-letter"].name, "I-751 Waiver Filing Cover Letter")
        self.assertEqual(
            templates["i-751-change-joint-to-waiver-cover-letter"].name,
            "I-751 Request to Change Joint Filing to Waiver Cover Letter",
        )

        for doc_type in templates.values():
            headings = [
                "".join(
                    child.get("text", "")
                    for child in node.get("content", [])
                    if isinstance(child, dict)
                )
                for node in doc_type.template_content.get("content", [])
                if node.get("type") == "heading"
            ]
            self.assertEqual(headings[:2], ["I. INTRODUCTION", "II. ARGUMENT"])
            self.assertTrue(headings[2].startswith("A."))
            self.assertTrue(headings[3].startswith("B."))
            self.assertTrue(headings[4].startswith("C."))
            self.assertIn("III. CONCLUSION", headings)
