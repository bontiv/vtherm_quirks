"""Base class of the quirks that keep a Tuya preset in line with the mode of VTherm.

Many Tuya radiator valves keep their mode (schedule, manual regulation, off...)
in a data point that Zigbee2MQTT and ZHA expose as the ``preset`` of the
climate entity. The core of VTherm neither sends nor tracks that preset. A
subclass says which preset each VTherm mode needs; this base class:

* sets it before the hvac mode of VTherm is sent (hook);
* watches the climate and corrects a preset that changes;
* stops after ``MAX_CORRECTIONS`` corrections in a row and reports it in the
  repairs, until VTherm changes mode.

The name starts with ``_``: the registry does not look for quirks in this module.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import State, callback

from ..const import STATE_UNAVAILABLE, STATE_UNKNOWN
from .base import BaseQuirk, QuirkIssue

# Corrections in a row before the repair issue, as in the tracking of the core.
MAX_CORRECTIONS = 3
# A preset already sent is not sent again before this delay (seconds): the TRV
# needs time to report its new preset.
RESEND_DELAY = 10.0

ISSUE_PRESET_KEEPS_CHANGING = "preset_keeps_changing"


class TuyaPresetQuirk(BaseQuirk):
    """Keeps the preset of a climate in line with the mode of VTherm.

    Extension points of the subclasses:

    * :meth:`_target_preset` (required): preset wanted for a mode, None to impose nothing;
    * :meth:`_accepted`: presets accepted for a wanted preset;
    * :meth:`_may_enforce`: whether a preset may be sent for this state of the TRV;
    * :meth:`_check_state`: model specific check of each state;
    * :meth:`_on_corrected`: called after a correction is sent;
    * :meth:`_after_accepted`: called when the preset is an accepted one.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        # Mode announced by the last hook and the mode of VTherm at that time:
        # the hook mode only holds while VTherm keeps this mode (the core can
        # turn the underlying off, for a window, while VTherm stays in heat).
        self._hook_mode: str | None = None
        self._hook_runtime_mode: str | None = None
        # Preset wanted at the last check, corrections done for it.
        self._target: str | None = None
        self._corrections = 0
        self._stuck: QuirkIssue | None = None
        # Last preset sent and when.
        self._sent_target: str | None = None
        self._sent_at: float | None = None

    # -- extension points ----------------------------------------------
    def _target_preset(self, mode: str) -> str | None:
        """Preset wanted when VTherm is in ``mode``, None to impose nothing."""
        raise NotImplementedError

    def _accepted(self, target: str) -> tuple[str, ...]:
        """Presets accepted when ``target`` is wanted."""
        return (target,)

    def _may_enforce(self, state: State) -> bool:
        """True when a preset may be sent to the TRV in this state."""
        return True

    def _check_state(self, state: State, preset: str) -> None:
        """Model specific check of a state of the TRV."""

    def _on_corrected(self, old: str, target: str) -> None:
        """A correction from ``old`` to ``target`` was sent."""

    def _after_accepted(self, state: State, target: str) -> None:
        """The TRV is in an accepted preset for ``target``."""

    # -- helpers -------------------------------------------------------
    def _now(self) -> float:
        return self.hass.loop.time()

    def _runtime_mode(self) -> str | None:
        mode = getattr(self.runtime, "vtherm_hvac_mode", None)
        return None if mode is None else str(mode)

    def _wanted_mode(self) -> str | None:
        """Mode of the underlying wanted by VTherm, None when unknown."""
        runtime_mode = self._runtime_mode()
        if self._hook_mode is not None and runtime_mode == self._hook_runtime_mode:
            return self._hook_mode
        return runtime_mode

    @staticmethod
    def _preset_of(state: State | None) -> str | None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        preset = state.attributes.get("preset_mode")
        return str(preset) if preset else None

    def _send_preset(self, preset: str) -> None:
        """Set the preset in a background task, without waiting for the TRV."""
        self._sent_target = preset
        self._sent_at = self._now()
        data = {"entity_id": self.underlying_entity_id, "preset_mode": preset}
        self.spawn(
            self.call_service("climate", "set_preset_mode", data, blocking=False),
            f"preset {preset}",
        )

    def _change_target(self, target: str | None) -> None:
        """A new preset is wanted: forget the corrections and the stuck issue."""
        if target == self._target:
            return
        self.log.debug("wanted preset %s -> %s, corrections reset", self._target, target)
        self._target = target
        self._corrections = 0
        self._sent_target = None
        if self._stuck is not None:
            self._stuck = None
            self.request_issue_sync()

    # -- lifecycle and hook --------------------------------------------
    async def async_startup(self) -> None:
        """Check the preset once Home Assistant is running."""
        self._enforce()

    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Set the preset that matches the mode, without waiting for the TRV."""
        if climate_entity_id != self.underlying_entity_id:
            return
        mode = str(hvac_mode)
        self._hook_mode = mode
        self._hook_runtime_mode = self._runtime_mode()
        target = self._target_preset(mode)
        self._change_target(target)
        if target is None:
            return
        state = self.underlying_state
        if not self.underlying_available or state is None:
            self.log.debug("TRV unavailable, preset %s not sent", target)
            return
        if not self._may_enforce(state):
            self.log.debug("TRV in %s, preset %s left to the state tracking", state.state, target)
            return
        preset = self._preset_of(state)
        if preset not in self._accepted(target):
            self._send_preset(target)
            if preset is not None:
                self._on_corrected(preset, target)

    # -- TRV state tracking --------------------------------------------
    @callback
    def on_underlying_state(self, old_state: State | None, new_state: State) -> None:
        self._enforce()

    def _enforce(self) -> None:
        """Check the preset against the wanted mode and correct it if needed."""
        state = self.underlying_state
        preset = self._preset_of(state)
        if state is None or preset is None:
            return
        self._check_state(state, preset)

        mode = self._wanted_mode()
        if mode is None:
            return
        target = self._target_preset(mode)
        self._change_target(target)
        if target is None:
            return
        if preset in self._accepted(target):
            self._after_accepted(state, target)
            return
        if self._stuck is not None or not self._may_enforce(state):
            return
        if (
            self._sent_target == target
            and self._sent_at is not None
            and self._now() - self._sent_at < RESEND_DELAY
        ):
            self.log.debug("preset %s instead of %s, waiting for the TRV", preset, target)
            return
        if self._corrections >= MAX_CORRECTIONS:
            self.log.warning(
                "the preset keeps leaving %s (now %s), corrections stopped", target, preset
            )
            self._stuck = QuirkIssue(
                ISSUE_PRESET_KEEPS_CHANGING,
                placeholders={"preset": target, "current": preset},
            )
            self.request_issue_sync()
            return
        self._corrections += 1
        self.log.info(
            "preset %s instead of %s, correction %s/%s",
            preset,
            target,
            self._corrections,
            MAX_CORRECTIONS,
        )
        self._send_preset(target)
        self._on_corrected(preset, target)

    # -- checks --------------------------------------------------------
    def check_configuration(self) -> list[QuirkIssue]:
        """Report a TRV that keeps leaving its preset."""
        return [] if self._stuck is None else [self._stuck]
