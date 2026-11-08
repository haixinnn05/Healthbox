"""Gemini adapter with a fake client: schema validation, fact-ID checks, limits, timeouts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.ai import AIError, AIService
from app.config import Settings


class FakeModels:
    def __init__(self, text=None, delay=0.0):
        self.text, self.delay, self.calls = text, delay, 0

    async def generate_content(self, **kw):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return SimpleNamespace(text=self.text, usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5))


def service(text, delay=0.0, **kw):
    s = Settings(genai_backend="developer_api", ai_timeout_s=0.2, **kw)
    ai = AIService(s)
    models = FakeModels(text, delay)
    ai.client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return ai, models


FACTS = [{"id": "F-next", "text": "Next confirmed dose: A x1 at 8:00 PM."}]
HELP = [{"id": "H-due", "title": "Due reminders", "text": "..."}]


def test_label_fields_are_bounded_and_need_review():
    ai, _ = service(json.dumps({"medication_label": "Demo A", "strength_text": "5 mg", "units_per_dose": 9,
                                "frequency_text": "twice daily", "ambiguity_notes": None, "requires_review": False}))
    out = asyncio.run(ai.extract_label([(b"img1", "image/png"), (b"img2", "image/png")], 3, "bottle"))
    assert out["units_per_dose"] is None and "outside the demo range" in out["ambiguity_notes"]
    assert out["requires_review"] is True


def test_assistant_rejects_invented_fact_ids():
    ai, _ = service(json.dumps({"answer_text": "Swallowed.", "supporting_fact_ids": ["F-made-up"],
                                "suggested_page": None, "needs_more_information": False, "scope": "in_scope"}))
    with pytest.raises(AIError) as ex:
        asyncio.run(ai.ask("did I take it", HELP, FACTS, []))
    assert ex.value.code == "AI_INVALID"


def test_assistant_valid_answer():
    ai, _ = service(json.dumps({"answer_text": "Your next dose is A at 8 PM.", "supporting_fact_ids": ["F-next"],
                                "suggested_page": "today", "needs_more_information": False, "scope": "in_scope"}))
    out = asyncio.run(ai.ask("what is next", HELP, FACTS, []))
    assert out["suggested_page"] == "today" and ai.tokens == {"input": 10, "output": 5}


def test_timeout_is_not_retried():
    ai, models = service("{}", delay=1.0)
    with pytest.raises(AIError) as ex:
        asyncio.run(ai.ask("q", HELP, FACTS, []))
    assert ex.value.code == "AI_TIMEOUT" and models.calls == 1


def test_request_limit():
    ai, _ = service(json.dumps({"answer_text": "ok", "supporting_fact_ids": [], "scope": "in_scope"}), ai_request_limit=1)
    asyncio.run(ai.ask("q", HELP, FACTS, []))
    with pytest.raises(AIError) as ex:
        asyncio.run(ai.ask("q", HELP, FACTS, []))
    assert ex.value.code == "AI_LIMIT"


def test_ai_module_has_no_hardware_or_plan_access():
    import app.ai as mod
    src = open(mod.__file__).read()
    for forbidden in ("hardware", "workflow", "plans", "store", "serial"):
        assert f"from .{forbidden}" not in src and f"import {forbidden}" not in src


def test_bottle_photos_are_sent_together():
    ai, models = service(json.dumps({"medication_label": "Demo A", "requires_review": True}))
    captured = {}
    orig = models.generate_content

    async def spy(**kw):
        captured.update(kw)
        return await orig(**kw)
    models.generate_content = spy
    asyncio.run(ai.extract_label([(b"a", "image/jpeg"), (b"b", "image/jpeg"), (b"c", "image/jpeg")], 3, "bottle"))
    contents = captured["contents"]
    assert len(contents) == 4 and "round bottle" in contents[-1]  # 3 photos + one instruction, one request
    assert models.calls == 1


class RateLimited(Exception):
    code = 429


async def _rate_limited_then(ai_service, fails, make_ok=None):
    calls = []

    async def generate_content(**kw):
        calls.append(kw)
        if len(calls) <= fails:
            raise RateLimited("429 RESOURCE_EXHAUSTED")
        return SimpleNamespace(text="OK", usage_metadata=None)

    ai_service.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    ai_service.rate_limit_waits = (0, 0)
    return calls


def _service():
    from app.ai import AIService
    from app.config import Settings
    return AIService(Settings.from_env(genai_backend="developer_api"))


def test_rate_limit_is_retried_and_counts_once():
    import asyncio
    ai = _service()

    async def run():
        calls = await _rate_limited_then(ai, 2)
        await ai._generate(["x"], None)
        return calls
    calls = asyncio.run(run())
    assert len(calls) == 3 and ai.requests_used == 1 and ai.last_outcome == "ok"


def test_rate_limit_that_does_not_clear_says_so():
    import asyncio

    import pytest

    from app.ai import AIError
    ai = _service()

    async def run():
        await _rate_limited_then(ai, 9)
        await ai._generate(["x"], None)
    with pytest.raises(AIError) as e:
        asyncio.run(run())
    assert e.value.code == "AI_RATE_LIMIT" and "free limit" in e.value.message


def test_other_client_errors_are_not_retried():
    import asyncio

    import pytest

    from app.ai import AIError
    ai = _service()
    calls = []

    class Refused(Exception):
        code = 403

    async def generate_content(**kw):
        calls.append(kw)
        raise Refused("403")
    ai.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    ai.rate_limit_waits = (0, 0)
    with pytest.raises(AIError):
        asyncio.run(ai._generate(["x"], None))
    assert len(calls) == 1


def test_daily_limit_is_not_retried_and_says_when_it_resets():
    import asyncio

    import pytest

    from app.ai import AIError
    ai = _service()
    calls = []

    class Daily(Exception):
        code = 429

        def __str__(self):
            return "429 RESOURCE_EXHAUSTED {'@type': 'RetryInfo', 'retryDelay': '79831s'}"

    async def generate_content(**kw):
        calls.append(kw)
        raise Daily()
    ai.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    ai.rate_limit_waits = (0, 0)
    with pytest.raises(AIError) as e:
        asyncio.run(ai._generate(["x"], None))
    assert len(calls) == 1 and e.value.code == "AI_DAILY_LIMIT" and "22 hours" in e.value.message
