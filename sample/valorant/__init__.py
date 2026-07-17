"""Standalone Valorant provider entrypoint and registration boundary."""

from pathlib import Path

from DispatchThis import SampleSemantics, register_provider


_RECOVERY = Path(__file__).with_name("_recovery.py")
exec(compile(_RECOVERY.read_text(encoding="utf-8"), str(_RECOVERY), "exec"), globals(), globals())


provider = SampleSemantics(
    provider_id="valorant-emdqx-0927cb886ad9a706",
    name="Valorant emdqx automatic collector",
    api_version=4,
    branch_targets=globals()["branch_targets"],
    call_targets=globals()["call_targets"],
    global_data=globals()["global_data"],
    string_recovery=globals()["string_recovery"],
)


register_provider(provider)
