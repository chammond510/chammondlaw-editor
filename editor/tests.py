from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .agent_service import ChatAgentResult, SuggestAgentResult
from .models import (
    Document,
    DocumentResearchMessage,
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
    def test_agent_chat_persists_messages_and_response_id(self, agent_cls):
        agent = agent_cls.return_value
        agent.chat.return_value = ChatAgentResult(
            answer="Use Matter of Acosta and cite the nexus standard more directly.",
            response_id="resp_test_123",
            tool_calls=[{"source": "biaedge", "name": "search_cases", "type": "mcp_call"}],
            citations=[{"type": "web", "title": "Example", "url": "https://example.com"}],
            used_tools=["biaedge"],
            metadata={"model": "gpt-5.4"},
        )

        response = self.client.post(
            reverse("research_agent_chat", kwargs={"doc_id": self.document.id}),
            data={
                "message": "What precedent should I add here?",
                "selected_text": "The client fears return because gang threats escalated.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["assistant_message"]["content"], agent.chat.return_value.answer)

        session = DocumentResearchSession.objects.get(document=self.document, user=self.user)
        self.assertEqual(session.last_response_id, "resp_test_123")
        self.assertEqual(session.messages.count(), 2)
        self.assertTrue(
            DocumentResearchMessage.objects.filter(session=session, role="assistant", response_id="resp_test_123").exists()
        )

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
    def test_agent_suggest_returns_structured_payload(self, agent_cls):
        agent = agent_cls.return_value
        agent.suggest.return_value = SuggestAgentResult(
            selection_summary="Nexus support for gang-based persecution.",
            draft_gap="The paragraph needs controlling nexus authority and one central reason language.",
            authorities=[
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
            search_notes="Searched precedential BIA nexus authorities.",
            next_questions=["Do you also need PSG-specific authority?"],
            response_id="resp_suggest_1",
            tool_calls=[{"source": "biaedge", "name": "search_cases", "type": "mcp_call"}],
            citations=[],
            raw_answer="{}",
        )

        response = self.client.post(
            reverse("research_agent_suggest", kwargs={"doc_id": self.document.id}),
            data={
                "selected_text": "The threats were because he reported gang extortion.",
                "focus_note": "Find the best nexus authority.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["selection_summary"], "Nexus support for gang-based persecution.")
        self.assertEqual(payload["authorities"][0]["citation"], "25 I&N Dec. 341 (BIA 2010)")
        self.assertEqual(payload["tool_calls"][0]["source"], "biaedge")
