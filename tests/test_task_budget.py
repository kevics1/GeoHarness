from geoharness.config.settings import AgentConfig, GeoConfig
from geoharness.task_budget import budget_context, budget_status


def test_time_target_is_observational_and_does_not_request_early_delivery():
    config = GeoConfig(agent=AgentConfig(task_budget_seconds=1200, delivery_reserve_seconds=180))
    metadata = {"geo_task_timing": {"started_at": 1000}}
    assert budget_status(metadata, config, now=1100)["phase"] == "work"
    assert budget_status(metadata, config, now=2050)["phase"] == "work"
    assert budget_status(metadata, config, now=2300)["phase"] == "target_exceeded"
    assert budget_status(metadata, config, now=2300)["hard_timeout"] is False
    assert "不因超过时间目标而提前结束" in budget_context(metadata, config)
    assert budget_status(metadata, GeoConfig(), now=2300) == {}
