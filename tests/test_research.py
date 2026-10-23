import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app, build_messages_with_research, build_research_store
from app.research.service import ResearchService
from app.research.store import ResearchStore
from app.schemas import ChatRequest


class FakeResearchProvider:
    async def chat(self, *, messages, model, temperature):
        return (
            "# Test Topic\n\n## Executive Summary\n\nA verified finding [S1].\n\n"
            "## Sources\n\n- [S1] Primary test source"
        )


class FixtureResearchService(ResearchService):
    async def _search_searxng(self, topic, limit):
        return [{
            "source_key": "fixture-1",
            "provider": "Fixture",
            "title": "Primary test source",
            "url": "https://example.com/source",
            "abstract": "Evidence about the test topic.",
            "authors": ["Ada Example"],
            "published_at": "2026-01-01",
            "citation_count": 2,
            "score": 1.0,
        }]

    async def _enrich_web_sources(self, sources, *, limit):
        return [{**source, "content": source["abstract"]} for source in sources]

    async def _index_knowledge(self, job_id, topic, knowledge_key, content):
        return None


class ResearchStoreTests(unittest.TestCase):
    def test_job_lifecycle_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.sqlite3"
            store = ResearchStore(path)
            queued = store.create_job(
                topic="Particle physics",
                tier="scholar",
                estimated_seconds=7200,
                source_target=1000,
                deep_analysis_target=120,
            )
            claimed = store.claim_next()
            self.assertEqual(claimed["job_id"], queued["job_id"])
            self.assertEqual(claimed["status"], "running")

            restarted = ResearchStore(path, recover_interrupted=True)
            recovered = restarted.get_job(queued["job_id"])
            self.assertEqual(recovered["status"], "queued")
            self.assertIn("Recovered", recovered["phase"])

    def test_cancelled_job_cannot_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchStore(Path(directory) / "research.sqlite3")
            job = store.create_job(
                topic="Local knowledge",
                tier="summary",
                estimated_seconds=120,
                source_target=8,
                deep_analysis_target=8,
            )
            cancelled = store.cancel_job(job["job_id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertIsNone(store.claim_next())

    def test_failed_job_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchStore(Path(directory) / "research.sqlite3")
            job = store.create_job(
                topic="Local inference",
                tier="scholar",
                estimated_seconds=120,
                source_target=1000,
                deep_analysis_target=120,
            )
            store.fail_job(job["job_id"], "model ran out of memory")

            retried = store.retry_job(job["job_id"])

            self.assertEqual(retried["status"], "queued")
            self.assertEqual(retried["phase"], "Queued for retry")
            self.assertIsNone(retried["error"])
            self.assertEqual(store.claim_next()["job_id"], job["job_id"])


class ResearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_pipeline_creates_readable_docx_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                data_dir=root,
                research_dir=root / "Research",
                research_model="qwen3:8b",
            )
            store = ResearchStore(root / "research.sqlite3")
            estimate = FixtureResearchService(settings, store, provider=FakeResearchProvider()).estimate(
                "Test Topic", "summary"
            )
            store.create_job(
                topic="Test Topic",
                tier="summary",
                estimated_seconds=estimate["estimated_seconds"],
                source_target=estimate["source_target"],
                deep_analysis_target=estimate["deep_analysis_target"],
            )
            job = store.claim_next()
            service = FixtureResearchService(settings, store, provider=FakeResearchProvider())

            result = await service.run_job(job)

            self.assertEqual(result["status"], "completed")
            artifact = store.get_artifact(result["job_id"])
            path = Path(artifact["artifact_path"])
            self.assertEqual(path.parent.name, "Summary")
            self.assertTrue(path.is_file())
            self.assertTrue(path.with_suffix(".sources.json").is_file())
            text = "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
            self.assertIn("Executive Summary", text)
            self.assertIn("verified finding", text)

    async def test_knowledge_context_uses_lexical_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(data_dir=root, research_dir=root / "Research")
            store = ResearchStore(root / "research.sqlite3")
            job = store.create_job(
                topic="Particle physics",
                tier="scholar",
                estimated_seconds=7200,
                source_target=1000,
                deep_analysis_target=120,
            )
            job = store.claim_next()
            content = "# Particle physics\n\n## Quarks\n\nQuarks carry color charge [S1]."
            artifact_path = root / "Research" / "Scholar" / "particle-physics.md"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(content)
            store.finish_job(
                job["job_id"],
                artifact_name=artifact_path.name,
                artifact_path=str(artifact_path),
                artifact_media_type="text/markdown",
                content_markdown=content,
                knowledge_key="particle-physics",
                sources_found=1000,
                sources_analyzed=120,
            )
            service = ResearchService(settings, store, provider=FakeResearchProvider())
            with patch.object(service, "_query_knowledge", side_effect=RuntimeError("offline")):
                context = await service.context_for_mentions(
                    ["particle-physics"], "What charge do quarks carry?"
                )
            self.assertIn("color charge", context)
            self.assertIn('key="particle-physics"', context)


class ResearchApiTests(unittest.TestCase):
    def test_scholar_requires_estimate_confirmation_then_queues(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), research_dir=Path(directory) / "Research")
            app.dependency_overrides[get_settings] = lambda: settings
            build_research_store.cache_clear()
            client = TestClient(app)
            try:
                estimate = client.post(
                    "/api/research/estimate",
                    json={"topic": "Particle physics", "tier": "scholar"},
                )
                self.assertEqual(estimate.status_code, 200)
                self.assertEqual(estimate.json()["source_target"], 1000)

                rejected = client.post(
                    "/api/research/jobs",
                    json={"topic": "Particle physics", "tier": "scholar"},
                )
                self.assertEqual(rejected.status_code, 409)

                accepted = client.post(
                    "/api/research/jobs",
                    json={"topic": "Particle physics", "tier": "scholar", "confirmed": True},
                )
                self.assertEqual(accepted.status_code, 201)
                self.assertEqual(accepted.json()["status"], "queued")
                self.assertEqual(client.get("/api/research/jobs").json()["jobs"][0]["topic"], "Particle physics")

                failed_id = accepted.json()["job_id"]
                store = build_research_store(str(Path(directory) / "research.sqlite3"))
                store.fail_job(failed_id, "test failure")
                retried = client.post(f"/api/research/jobs/{failed_id}/retry")
                self.assertEqual(retried.status_code, 200)
                self.assertEqual(retried.json()["status"], "queued")
            finally:
                client.close()
                app.dependency_overrides.clear()
                build_research_store.cache_clear()

    def test_research_mentions_are_added_as_guarded_context(self):
        class FakeContextService:
            async def context_for_mentions(self, mentions, query):
                return '<research_knowledge key="particle-physics">Quarks [S1]</research_knowledge>'

        request = ChatRequest.model_validate({
            "system_prompt": "You are Friday.",
            "messages": [{"role": "user", "content": "Use @particle-physics to explain quarks"}],
            "research_mentions": ["particle-physics"],
        })
        settings = Settings()

        async def run():
            with patch("app.main.build_research_service", return_value=FakeContextService()):
                return await build_messages_with_research(request, settings)

        import asyncio
        messages = asyncio.run(run())
        self.assertIn("untrusted reference material", messages[1].content)
        self.assertIn("<retrieved_research_context>", messages[-1].content)
        self.assertIn("Quarks [S1]", messages[-1].content)


if __name__ == "__main__":
    unittest.main()
