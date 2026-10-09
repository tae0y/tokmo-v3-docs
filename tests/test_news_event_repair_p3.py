from datetime import UTC, datetime

from tokmo.programs.personalized_event_analysis import analyze_event
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.values.news_event import NewsReactionV1
from tokmo.values.personalized_event import PortfolioContext, ScenarioNode

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def test_dynamic_scenarios_persist_render_and_action_draft_zero_write(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    episode = store.upsert("scenario", (), NOW)
    nodes = tuple(
        ScenarioNode(f"n{i}", "MET" if i == 0 else "PENDING", i == 0, None)
        for i in range(4)
    )
    store.replace_scenario_nodes(episode.episode_id, nodes, now=NOW)
    assert len(store.scenario_nodes(episode.episode_id)) == 4
    result = analyze_event(
        episode_id=episode.episode_id,
        entities=("semiconductor",),
        scenarios=nodes,
        context=PortfolioContext((), (), True),
        prior_reaction=NewsReactionV1.NOT_HELPFUL,
    )
    assert result.action_draft and "확인" in result.action_draft
    assert result.acknowledges_prior_reaction is True
    import inspect

    assert "order" not in inspect.getsource(analyze_event).casefold()


def test_mute_is_durable_and_systemic_realert_has_new_identity(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    muted = store.upsert("same-event", (), NOW)
    store.mute_episode(muted.episode_id, "explicit", now=NOW)
    assert store.episode_state(muted.episode_id) == "MUTED"
    systemic = store.upsert("systemic-new-risk", (), NOW)
    assert systemic.episode_id != muted.episode_id
