"""Tests for the agent turn limit (max_turns) resolution.

The upstream QueryEngine defaults to 8 turns, which aborts multi-layer
cartography workflows ("Stopped after 8 turns") long before the model can
inspect every layer, load data, symbolize and export. GeoHarness defaults to
40 and honours GEOH_MAX_TURNS.
"""

from __future__ import annotations

import pytest

from geoharness.config.settings import GeoConfig
from geoharness.runtime import _DEFAULT_MAX_TURNS, _resolve_max_turns


class TestResolveMaxTurns:
    def test_default_is_generous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEOH_MAX_TURNS", raising=False)
        assert _resolve_max_turns(GeoConfig()) == _DEFAULT_MAX_TURNS

    def test_default_is_at_least_30(self) -> None:
        """The user-facing contract: never abort at 8 again."""
        assert _DEFAULT_MAX_TURNS >= 30

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEOH_MAX_TURNS", "60")
        assert _resolve_max_turns(GeoConfig()) == 60

    def test_env_invalid_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEOH_MAX_TURNS", "abc")
        assert _resolve_max_turns(GeoConfig()) == _DEFAULT_MAX_TURNS

    def test_env_below_one_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEOH_MAX_TURNS", "0")
        assert _resolve_max_turns(GeoConfig()) == _DEFAULT_MAX_TURNS


class TestEngineGetsMaxTurns:
    @pytest.mark.asyncio
    async def test_runtime_engine_honours_limit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """build_geo_runtime must pass the resolved limit to the engine."""
        monkeypatch.setenv("GEOH_MAX_TURNS", "31")
        from geoharness.runtime import build_geo_runtime

        rt = await build_geo_runtime(cwd=tmp_path)
        assert rt.engine.max_turns == 31


@pytest.mark.asyncio
async def test_query_engine_default_was_8() -> None:
    """Guards the premise: upstream QueryEngine still defaults to 8.

    If upstream changes its default, this test failing is a reminder to
    re-check that GeoHarness's explicit value still overrides it.
    """
    import inspect

    from openharness.engine.query_engine import QueryEngine

    sig = inspect.signature(QueryEngine.__init__)
    assert sig.parameters["max_turns"].default == 8
