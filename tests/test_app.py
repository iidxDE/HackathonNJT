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
    assert len(ranked) == 9
    assert {"CM → HB + PATH", "ST → HB + PATH", "RT → HB + PATH"} <= set(ranked["Option"])
    assert any("top option" in m.value for m in at.markdown)  # claim panel
