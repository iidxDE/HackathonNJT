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
    assert len(ranked) == 6  # CM/ST/RT → NY have no train: one notice instead of rows
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
