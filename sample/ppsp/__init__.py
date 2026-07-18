from pathlib import Path

from DispatchThis import CORE_API_VERSION, SampleSemantics, register_provider


_RECOVERY = Path(__file__).with_name("_recovery.py")
exec(
    compile(_RECOVERY.read_text(encoding="utf-8"), str(_RECOVERY), "exec"),
    globals(),
    globals(),
)


provider = SampleSemantics(
    provider_id="ppsp-c0714",
    name="ppsp c0714 entry trampoline",
    api_version=CORE_API_VERSION,
    branch_targets=globals()["branch_targets"],
)


register_provider(provider)
