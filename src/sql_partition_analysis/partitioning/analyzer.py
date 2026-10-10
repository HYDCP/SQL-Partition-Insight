"""Public SQL analysis service; execution engines provide dialect semantics."""

from sql_partition_analysis.engines import get_engine_adapter


def analyze_profile(profile, catalog, engine="adb_pg"):
    """Analyze a profile, defaulting to the legacy ADB PostgreSQL behavior."""
    return get_engine_adapter(engine).analyze_profile(profile, catalog)
