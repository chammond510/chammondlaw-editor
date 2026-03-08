from unittest.mock import patch
from io import BytesIO
from types import SimpleNamespace
import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from docx import Document as DocxDocument
from docx.shared import Inches

from .agent_service import (
    AGENT_FINALIZATION_MAX_OUTPUT_TOKENS,
    AGENT_FINALIZATION_REASONING_EFFORT,
    AgentConfigurationError,
    DocumentResearchAgent,
    _client_file_function_tools,
    _extract_output_text,
    _extract_hosted_tool_calls,
    _knowledge_function_tools,
    _normalize_edit_result,
    _normalize_mcp_server_url,
    _request_requirements_block,
    _requested_full_text_sources,
)
from .import_service import import_docx_to_tiptap
from .models import (
    Document,
    DocumentClientFile,
    DocumentResearchMessage,
    DocumentResearchRun,
    DocumentResearchSession,
    DocumentVersion,
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


def _build_docx_bytes(*, margin_inches=1.0):
    doc = DocxDocument()
    section = doc.sections[0]
    section.left_margin = Inches(margin_inches)
    section.right_margin = Inches(margin_inches)
    doc.styles["Normal"].font.name = "Courier New"

    heading = doc.add_heading("Imported Brief Heading", level=1)
    heading.runs[0].bold = True

    paragraph = doc.add_paragraph()
    paragraph.add_run("This is a ")
    paragraph.add_run("bold").bold = True
    paragraph.add_run(" paragraph.")

    bullet = doc.add_paragraph(style="List Bullet")
    bullet.add_run("First exhibit item")

    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].paragraphs[0].add_run("Exhibit").bold = True
    table.rows[0].cells[1].paragraphs[0].add_run("Description").bold = True
    table.rows[1].cells[0].text = "A"
    table.rows[1].cells[1].text = "Marriage certificate"

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


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

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_edit_starts_background_run(self, agent_cls):
        agent = agent_cls.return_value

        def fake_start_edit_run(*, run, **kwargs):
            run.status = "in_progress"
            run.stage = "waiting_openai"
            run.response_id = "resp_edit_1"
            run.response_count = 1
            run.save(update_fields=["status", "stage", "response_id", "response_count", "updated_at"])
            return run

        agent.start_edit_run.side_effect = fake_start_edit_run

        response = self.client.post(
            reverse("research_agent_edit", kwargs={"doc_id": self.document.id}),
            data={
                "instruction": "Rewrite this paragraph to tighten the nexus analysis.",
                "selected_text": "The client fears return because gang threats escalated.",
                "selection_from": 5,
                "selection_to": 62,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["run"]["status"], "in_progress")
        self.assertEqual(payload["run"]["response_id"], "resp_edit_1")

    @patch("editor.agent_views.DocumentResearchAgent")
    def test_agent_run_status_returns_completed_edit_payload(self, agent_cls):
        agent = agent_cls.return_value
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="edit",
            status="in_progress",
            stage="waiting_openai",
        )

        def fake_advance_run(*, run):
            run.status = "completed"
            run.stage = "completed"
            run.response_id = "resp_edit_done"
            run.result_payload = {
                "edit_summary": "Tighten the nexus paragraph.",
                "rationale": "This version states the legal standard first and then ties it to the facts.",
                "operation": "replace_selection",
                "target_text": "The client fears return because gang threats escalated.",
                "proposed_text": "The record shows the gang threatened the client because he reported the extortion to police.",
                "notes": "Uses a clearer causal link.",
                "selected_text": "The client fears return because gang threats escalated.",
                "selection_from": 5,
                "selection_to": 62,
                "selection_required": True,
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
        self.assertEqual(payload["edit_result"]["operation"], "replace_selection")
        self.assertIn("legal standard first", payload["edit_result"]["rationale"])

    def test_agent_apply_edit_snapshots_current_content_and_saves_new_content(self):
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="edit",
            status="completed",
            stage="completed",
            result_payload={
                "edit_summary": "Strengthen nexus paragraph",
                "operation": "replace_selection",
                "proposed_text": "Updated paragraph text.",
            },
        )
        current_content = _sample_tiptap("Original paragraph text.")
        new_content = _sample_tiptap("Updated paragraph text.")

        response = self.client.post(
            reverse("research_agent_apply_edit", kwargs={"doc_id": self.document.id}),
            data={
                "run_id": str(run.public_id),
                "current_content": current_content,
                "new_content": new_content,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.document.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(self.document.content, new_content)
        self.assertEqual(run.result_payload["applied_at"], payload["edit_result"]["applied_at"])
        snapshot = DocumentVersion.objects.get(id=payload["version"]["id"])
        self.assertEqual(snapshot.content, current_content)
        self.assertTrue(snapshot.label.startswith("Before agent edit - Strengthen nexus paragraph"))

    def test_client_document_upload_and_list_for_current_document(self):
        upload = SimpleUploadedFile(
            "police-report.txt",
            b"On March 1, 2024, the client reported the threats to police.",
            content_type="text/plain",
        )

        upload_response = self.client.post(
            reverse("document_client_file_upload", kwargs={"doc_id": self.document.id}),
            data={"file": upload, "title": "Police Report"},
        )

        self.assertEqual(upload_response.status_code, 200)
        payload = upload_response.json()["client_file"]
        self.assertEqual(payload["title"], "Police Report")
        self.assertIn("reported the threats", payload["extracted_text"])

        list_response = self.client.get(
            reverse("document_client_file_list", kwargs={"doc_id": self.document.id}),
            data={"q": "police"},
        )

        self.assertEqual(list_response.status_code, 200)
        results = list_response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Police Report")


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

        client_doc_schema = {
            tool["name"]: tool
            for tool in _client_file_function_tools()
        }["search_client_documents"]["parameters"]
        self.assertEqual(client_doc_schema["required"], ["query", "limit"])
        self.assertEqual(
            sorted(client_doc_schema["properties"].keys()),
            sorted(client_doc_schema["required"]),
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
    def test_build_tools_uses_low_context_web_search_for_suggest(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )

        tools = agent._build_tools(mode="suggest", include_mcp=False)
        web_tool = next(tool for tool in tools if tool.get("type") == "web_search_preview")

        self.assertEqual(web_tool["search_context_size"], "low")

    @patch("editor.agent_service._new_openai_client")
    def test_build_tools_includes_client_document_functions_when_current_document_has_uploads(self, new_client):
        DocumentClientFile.objects.create(
            document=self.document,
            title="Police Report",
            original_file=SimpleUploadedFile("police-report.txt", b"Client reported the extortion to police."),
            extracted_text="Client reported the extortion to police.",
            uploaded_by=self.user,
            metadata={"filename": "police-report.txt", "extension": ".txt"},
        )
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )

        tools = agent._build_tools(mode="chat", include_mcp=False)
        function_names = sorted(tool.get("name") for tool in tools if tool.get("type") == "function")

        self.assertIn("search_client_documents", function_names)
        self.assertIn("get_client_document", function_names)

    @patch("editor.agent_service._new_openai_client")
    def test_local_function_budget_forces_final_response_instead_of_failing(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="edit",
            status="in_progress",
            stage="running_tools",
            response_id="resp_local_tools",
            metadata=agent._initial_run_metadata(mode="edit", previous_response_id=""),
        )
        response = SimpleNamespace(id="resp_local_tools")
        function_call = SimpleNamespace(
            name="search_client_documents",
            call_id="call_client_doc_search",
            arguments='{"query":"draft statement","limit":2}',
        )
        queued_response = SimpleNamespace(id="resp_forced_final", status="queued", error=None, usage=None, output=[])

        with patch("editor.agent_service.AGENT_MAX_LOCAL_FUNCTION_ROUNDS", 0):
            with patch.object(agent, "_call_local_tool", return_value={"results": [{"id": 1, "title": "Draft Statement"}]}):
                with patch.object(agent, "_create_background_response", return_value=queued_response) as create_background:
                    updated = agent._continue_after_function_calls(
                        run=run,
                        response=response,
                        function_calls=[function_call],
                    )

        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.stage, "forcing_final")
        self.assertEqual(updated.response_id, "resp_forced_final")
        self.assertTrue(updated.metadata.get("allow_over_budget_finalization"))
        request = create_background.call_args.kwargs
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["tool_choice"], "none")
        self.assertEqual(request["max_output_tokens"], AGENT_FINALIZATION_MAX_OUTPUT_TOKENS)
        self.assertEqual(request["reasoning_effort"], AGENT_FINALIZATION_REASONING_EFFORT)

    @patch("editor.agent_service._new_openai_client")
    def test_budget_error_allows_forced_finalization_after_local_tool_cap(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="edit",
            status="in_progress",
            stage="forcing_final",
            local_function_rounds=9,
            metadata={"allow_over_budget_finalization": True},
        )

        with patch("editor.agent_service.AGENT_MAX_LOCAL_FUNCTION_ROUNDS", 8):
            self.assertEqual(agent._budget_error(run), "")

    @patch("editor.agent_service._new_openai_client")
    def test_budget_error_does_not_use_response_or_reasoning_caps(self, new_client):
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
            response_count=99,
            usage={"reasoning_tokens": 999999, "total_tokens": 100},
            metadata=agent._initial_run_metadata(mode="chat", previous_response_id=""),
        )

        self.assertEqual(agent._budget_error(run), "")

    @patch("editor.agent_service._new_openai_client")
    def test_total_token_budget_queues_compact_finalization_instead_of_failing(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="edit",
            status="in_progress",
            stage="running_tools",
            response_id="resp_budgeted",
            usage={"total_tokens": 130},
            tool_calls=[
                {
                    "source": "client_docs",
                    "type": "function_call",
                    "name": "get_client_document",
                    "status": "completed",
                    "arguments": {"file_id": 1},
                    "output_excerpt": '{"text":"Client states the officer requested the waiver at the interview."}',
                }
            ],
            metadata=agent._initial_run_metadata(mode="edit", previous_response_id=""),
        )
        queued_response = SimpleNamespace(id="resp_final_budget", status="queued", error=None, usage=None, output=[])

        with patch("editor.agent_service.AGENT_MAX_TOTAL_TOKENS", 120):
            with patch.object(agent, "_create_background_response", return_value=queued_response) as create_background:
                updated = agent.advance_run(run=run)

        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.stage, "forcing_final")
        self.assertEqual(updated.response_id, "resp_final_budget")
        self.assertTrue(updated.metadata.get("budget_recovery_attempted"))
        self.assertTrue(updated.metadata.get("allow_over_budget_finalization"))
        request = create_background.call_args.kwargs
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["tool_choice"], "none")
        self.assertIn("verified research already gathered", request["instructions"].lower())

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

    def test_requested_full_text_sources_detects_policy_and_case_quote_requests(self):
        sources = _requested_full_text_sources(
            "Find quotes from case law and quotes from the USCIS Policy Manual for this issue."
        )

        self.assertEqual(sources, {"case", "policy"})

    def test_request_requirements_block_guides_full_text_tools(self):
        note = _request_requirements_block(
            "Find quotes from case law and quotes from the USCIS Policy Manual that I can use."
        )

        self.assertIn("Turn requirements JSON:", note)
        self.assertIn('"requires_exact_quotes": true', note)
        self.assertIn('"required_full_text_sources": [', note)
        self.assertIn('"case"', note)
        self.assertIn('"policy"', note)
        self.assertIn("policy=search_references(source_code='uscis_pm')->get_reference", note)
        self.assertIn("case_law=get_document_text", note)

    @patch("editor.agent_service._new_openai_client")
    def test_start_edit_run_stores_structured_requirements(self, new_client):
        agent = DocumentResearchAgent(
            document=self.document,
            user=self.user,
        )
        session = DocumentResearchSession.objects.create(document=self.document, user=self.user)
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="edit",
            status="queued",
            stage="queued",
        )
        queued_response = SimpleNamespace(id="resp_edit_start", status="queued", error=None, usage=None, output=[])

        with patch.object(agent, "_build_tools", return_value=[]):
            with patch.object(agent, "_create_background_response", return_value=queued_response):
                updated = agent.start_edit_run(
                    run=run,
                    instruction="Find quotes from the USCIS Policy Manual and revise this section.",
                    selected_text="Selected paragraph text.",
                )

        self.assertEqual(updated.metadata.get("phase"), "research")
        requirements = updated.metadata.get("requirements") or {}
        self.assertEqual(requirements.get("mode"), "edit")
        self.assertEqual(requirements.get("output_format"), "edit_json")
        self.assertTrue(requirements.get("has_selected_text"))
        self.assertTrue(requirements.get("requires_exact_quotes"))
        self.assertIn("policy", requirements.get("required_full_text_sources") or [])

    @patch("editor.agent_service._new_openai_client")
    def test_record_response_artifacts_updates_evidence_pack(self, new_client):
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
            metadata=agent._initial_run_metadata(mode="chat", previous_response_id=""),
        )
        response = SimpleNamespace(
            id="resp_evidence",
            usage=None,
            output=[
                SimpleNamespace(
                    type="mcp_call",
                    name="get_reference",
                    status="completed",
                    arguments='{"reference_id":324,"source_code":"uscis_pm"}',
                    output='{"title":"USCIS Policy Manual","text":"A CPR may change to or add another waiver filing basis by making the request in writing."}',
                    error="",
                )
            ],
        )

        agent._record_response_artifacts(run, response)

        evidence_pack = (run.metadata or {}).get("evidence_pack") or {}
        self.assertEqual(evidence_pack["counts"]["legal_authorities"], 1)
        self.assertEqual(evidence_pack["legal_authorities"][0]["tool"], "get_reference")
        self.assertIn("waiver filing basis", evidence_pack["legal_authorities"][0]["excerpt"])
        metrics = (run.metadata or {}).get("metrics") or {}
        self.assertEqual(metrics["tool_source_counts"]["biaedge"], 1)
        self.assertIn("biaedge", metrics["used_sources"])

    def test_normalize_edit_result_falls_back_to_append_without_selected_text(self):
        normalized = _normalize_edit_result(
            {
                "edit_summary": "Add a conclusion",
                "operation": "replace_selection",
                "proposed_text": "For these reasons, USCIS should approve the petition.",
            },
            request_payload={"selected_text": ""},
        )

        self.assertEqual(normalized["operation"], "append_to_document")

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
        self.assertIn("Evidence pack:", request["input_payload"])
        self.assertIn("waiver filing basis", request["input_payload"])
        self.assertEqual(updated.metadata["finalization_source"], "empty_response")
        self.assertEqual(updated.metadata["metrics"]["finalization_source"], "empty_response")

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
        with patch("editor.agent_service.AGENT_MAX_TOOL_CALLS", 24):
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

    @patch("editor.agent_service._new_openai_client")
    def test_advance_run_requests_full_text_before_finalizing_quote_answer(self, new_client):
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
            response_id="resp_policy_search",
            request_payload={
                "message": "Find quotes from case law and quotes from the USCIS Policy Manual for this paragraph.",
                "selected_text": "Selected paragraph text.",
            },
            metadata=agent._initial_run_metadata(mode="chat", previous_response_id=""),
            tool_calls=[
                {
                    "source": "biaedge",
                    "type": "mcp_call",
                    "name": "search_references",
                    "status": "completed",
                    "arguments": {"query": "uscis policy manual change waiver basis", "source_code": "uscis_pm"},
                }
            ],
        )

        completed_response = SimpleNamespace(
            id="resp_policy_search",
            status="completed",
            error=None,
            output_text=(
                "I do not have a verified direct quote from the USCIS Policy Manual in the materials gathered for this turn."
            ),
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            text=(
                                "I do not have a verified direct quote from the USCIS Policy Manual in the materials gathered for this turn."
                            ),
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=None,
        )
        queued_response = SimpleNamespace(
            id="resp_quote_verify",
            status="queued",
            error=None,
            usage=None,
            output=[],
        )

        agent.client.responses.retrieve = lambda *args, **kwargs: completed_response

        with patch.object(agent, "_build_tools", return_value=[{"type": "mcp"}]):
            with patch.object(agent, "_create_background_response", return_value=queued_response) as create_background:
                updated = agent.advance_run(run=run)

        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.stage, "verifying_quotes")
        self.assertEqual(updated.response_id, "resp_quote_verify")
        self.assertTrue(updated.metadata.get("quote_source_verification_attempted"))
        request = create_background.call_args.kwargs
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(request["previous_response_id"], "resp_policy_search")
        self.assertEqual(request["instructions"], agent._run_instructions(run))
        self.assertIn("Turn requirements JSON:", request["input_payload"])
        self.assertIn("missing_full_text_sources: policy, case_law", request["input_payload"])


class DocumentImportTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="editor-import-tests-")
        cls._override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(username="importer", password="secret")
        self.client.force_login(self.user)
        self.document_type = DocumentType.objects.create(
            name="Imported Brief",
            slug="imported-brief",
            category="brief",
            export_format="court_brief",
            description="Brief imported from Word",
            icon="📘",
        )

    def test_import_docx_to_tiptap_preserves_basic_structure(self):
        content = import_docx_to_tiptap(BytesIO(_build_docx_bytes()))
        nodes = content.get("content", [])

        self.assertEqual(content.get("type"), "doc")
        self.assertEqual(nodes[0]["type"], "heading")
        self.assertEqual(nodes[0]["attrs"]["level"], 1)
        self.assertEqual(nodes[1]["type"], "paragraph")
        self.assertEqual(nodes[2]["type"], "bulletList")
        self.assertEqual(nodes[3]["type"], "table")

    def test_import_document_creates_editable_document_with_source_docx(self):
        upload = SimpleUploadedFile(
            "existing-brief.docx",
            _build_docx_bytes(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        response = self.client.post(
            reverse("import_document"),
            {
                "title": "Existing I-751 Brief",
                "document_type": self.document_type.slug,
                "file": upload,
            },
        )

        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(title="Existing I-751 Brief")
        self.assertTrue(bool(document.source_docx))
        self.assertEqual(document.document_type, self.document_type)
        self.assertEqual(document.versions.count(), 1)
        self.assertEqual(document.versions.first().label, "Imported from Word")
        self.assertEqual(document.content.get("type"), "doc")

    def test_docx_export_uses_source_docx_template_when_present(self):
        upload = SimpleUploadedFile(
            "existing-brief.docx",
            _build_docx_bytes(margin_inches=1.5),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        document = Document.objects.create(
            title="Imported Export Template",
            document_type=self.document_type,
            content={
                "type": "doc",
                "content": [
                    {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Updated Heading"}]},
                    {"type": "paragraph", "content": [{"type": "text", "text": "Updated body paragraph."}]},
                ],
            },
            source_docx=upload,
            created_by=self.user,
        )

        response = self.client.get(reverse("export_docx", kwargs={"doc_id": document.id}))

        self.assertEqual(response.status_code, 200)
        exported = DocxDocument(BytesIO(response.content))
        self.assertAlmostEqual(exported.sections[0].left_margin.inches, 1.5, places=2)
        self.assertEqual(exported.paragraphs[0].text, "Updated Heading")


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
