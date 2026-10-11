import pytest

from src import config

pytest.importorskip("streamlit")
needs_data = pytest.mark.skipif(not (config.POOLS_PARQUET.exists() and config.DRIVE_CACHE_PARQUET.exists()),
                                reason="artifacts not built")


@needs_data
def test_demo_preset_renders_offline():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(config.REPO_ROOT / "app" / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert at.info[0].value.startswith("Reliability is estimated from about one weekday")
    assert at.warning[0].value.startswith("Delay history predates the Portal cutover")
    assert at.subheader[0].value.startswith("Best option: ")
    ranked = at.dataframe[0].value
    ranked = getattr(ranked, "data", ranked)
    assert len(ranked) == 7  # CM/ST/RT → NY have no train: one notice instead of rows
    # 3.1: the SE exclusion-run alternative, labelled with the assets it still uses
    alt = ranked.loc[ranked["Option"] == "SE → TS → HB + PATH", "Flags"].iloc[0]
    assert alt.startswith("Avoids Penn Station and the North River tunnels. Still uses: PATH tunnels.")
    assert not {"CM → NY", "ST → NY", "RT → NY"} & set(ranked["Option"])
    assert any(i.value.startswith("Midtown Direct to Penn suspended weekdays Oct 11–Nov 14") for i in at.info)
    assert {"CM → HB + PATH", "ST → HB + PATH", "RT → HB + PATH"} <= set(ranked["Option"])
    # Penalty $75 → $0 re-selects NP's train from 3224 to the timetable's 3926 (sweep row 1).
    def np_train():
        d = at.dataframe[0].value
        d = getattr(d, "data", d)
        return d.loc[d["Option"] == "NP → NY", "Train (chosen)"].iloc[0]
    assert np_train() == "3224"
    at.number_input[1].set_value(0.0)
    at.run()
    assert np_train() == "3926"
    assert any("top option" in m.value for m in at.markdown)  # claim panel


@needs_data
@pytest.mark.skipif(not config.network_npz(config.DEMO_SERVICE_DATE).exists(), reason="network not built")
def test_router_is_default_and_matches_safety_net():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(config.REPO_ROOT / "app" / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception and at.radio[0].value == "router"
    best_router = at.subheader[0].value
    at.radio[0].set_value("safety_net")
    at.run()
    assert not at.exception and at.subheader[0].value == best_router
