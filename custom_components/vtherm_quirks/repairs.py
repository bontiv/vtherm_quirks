"""Repair issues of the integration.

The quirks report the problems found in the configuration of a VTherm or of a
device (``BaseQuirk.check_configuration``). They are shown in the Home
Assistant repairs and removed as soon as the problem is fixed. None of them is
fixable by a flow: the user has to change the VTherm or the device settings.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_QUIRK,
    CONF_QUIRKS,
    CONF_TARGET_VTHERM_UNIQUE_ID,
    DATA_ISSUES,
    DOMAIN,
    VT_CONF_NAME,
    VTHERM_DOMAIN,
)
from .quirks import compatible_quirks, get_quirk

_LOGGER = logging.getLogger(__name__)

ISSUE_HOOKS_UNAVAILABLE = "hooks_unavailable"
ISSUE_UNKNOWN_QUIRK = "unknown_quirk"
ISSUE_INCOMPATIBLE_QUIRK = "incompatible_quirk"


@dataclass(frozen=True, slots=True)
class ReportedIssue:
    """An issue to show in the repairs."""

    issue_id: str
    translation_key: str
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING
    placeholders: Mapping[str, str] = field(default_factory=dict)


def _known(hass: HomeAssistant) -> dict[str, set[str]]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(DATA_ISSUES, {})


@callback
def async_sync_issues(
    hass: HomeAssistant, scope: str, issues: Iterable[ReportedIssue]
) -> None:
    """Create the issues of a scope and delete those that are gone."""
    issues = list(issues)
    known = _known(hass)
    current = {issue.issue_id for issue in issues}
    for stale in known.get(scope, set()) - current:
        _LOGGER.debug("Issue cleared: %s (scope %s)", stale, scope)
        ir.async_delete_issue(hass, DOMAIN, stale)
    for issue in issues:
        _LOGGER.debug("Issue created: %s (scope %s)", issue.issue_id, scope)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue.issue_id,
            is_fixable=False,
            severity=issue.severity,
            translation_key=issue.translation_key,
            translation_placeholders=dict(issue.placeholders),
        )
    known[scope] = current


@callback
def async_clear_issues(hass: HomeAssistant, scope: str) -> None:
    """Delete all the issues of a scope."""
    async_sync_issues(hass, scope, [])
    _known(hass).pop(scope, None)


def entry_scope(entry: ConfigEntry) -> str:
    """Scope of the issues of a config entry of the plugin."""
    return f"entry_{entry.entry_id}"


@callback
def async_check_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Report the quirks of an entry that are unknown or incompatible with the VTherm."""
    target = entry.data.get(CONF_TARGET_VTHERM_UNIQUE_ID)
    vtherm_entry = hass.config_entries.async_get_entry(target) if target else None
    vtherm_infos = (
        {**vtherm_entry.data, **vtherm_entry.options}
        if vtherm_entry is not None and vtherm_entry.domain == VTHERM_DOMAIN
        else None
    )
    name = (vtherm_infos or {}).get(VT_CONF_NAME) or entry.title
    issues: list[ReportedIssue] = []
    for underlying, conf in (entry.data.get(CONF_QUIRKS) or {}).items():
        quirk_id = conf.get(CONF_QUIRK)
        quirk = get_quirk(quirk_id)
        placeholders = {"name": name, "underlying": underlying, "quirk": str(quirk_id)}
        if quirk is None:
            issues.append(
                ReportedIssue(
                    f"{entry.entry_id}_{underlying}_{ISSUE_UNKNOWN_QUIRK}",
                    ISSUE_UNKNOWN_QUIRK,
                    placeholders=placeholders,
                )
            )
        elif vtherm_infos is not None and quirk not in compatible_quirks(vtherm_infos):
            issues.append(
                ReportedIssue(
                    f"{entry.entry_id}_{underlying}_{ISSUE_INCOMPATIBLE_QUIRK}",
                    ISSUE_INCOMPATIBLE_QUIRK,
                    placeholders={**placeholders, "quirk": quirk.title},
                )
            )
    async_sync_issues(hass, entry_scope(entry), issues)
