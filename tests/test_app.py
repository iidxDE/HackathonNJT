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
    assert at.subheader[0].value.startswith("Best option: CM → HB + PATH. Leave 07:40, train 872")
    ranked = at.dataframe[0].value
    ranked = getattr(ranked, "data", ranked)
    assert len(ranked) == 7
    assert list(ranked["Option"][:3]) == ["CM → HB + PATH", "NP → NY", "SE → NY"]
    assert any("changes" in m.value for m in at.markdown)  # claim panel
