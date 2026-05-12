from __future__ import annotations

from shared.models import BatteryState


class Battery:
    def __init__(self, cfg: dict):
        self.capacity_mwh: float = cfg["capacity_mwh"]
        self.max_charge_mw: float = cfg["max_charge_mw"]
        self.max_discharge_mw: float = cfg["max_discharge_mw"]
        self.efficiency: float = cfg["efficiency"]
        self._soc_mwh: float = cfg["capacity_mwh"] * cfg["initial_soc_pct"]

    @property
    def soc_mwh(self) -> float:
        return self._soc_mwh

    @property
    def soc_pct(self) -> float:
        return self._soc_mwh / self.capacity_mwh

    def can_charge(self) -> bool:
        return self.soc_pct < 0.90

    def can_discharge(self) -> bool:
        return self.soc_pct > 0.10

    def max_charge_volume(self, interval_hours: float) -> float:
        if not self.can_charge():
            return 0.0
        power_limited = self.max_charge_mw * interval_hours
        # Energy that fits into the battery accounting for efficiency
        headroom = (self.capacity_mwh - self._soc_mwh) / self.efficiency
        return min(power_limited, headroom)

    def max_discharge_volume(self, interval_hours: float) -> float:
        if not self.can_discharge():
            return 0.0
        power_limited = self.max_discharge_mw * interval_hours
        # Energy the battery can actually supply accounting for efficiency
        available = self._soc_mwh * self.efficiency
        return min(power_limited, available)

    def apply_charge(self, volume_mwh: float) -> None:
        self._soc_mwh = min(self.capacity_mwh, self._soc_mwh + volume_mwh * self.efficiency)

    def apply_discharge(self, volume_mwh: float) -> None:
        self._soc_mwh = max(0.0, self._soc_mwh - volume_mwh / self.efficiency)

    def state(self) -> BatteryState:
        return BatteryState(
            soc_mwh=self._soc_mwh,
            capacity_mwh=self.capacity_mwh,
            max_charge_mw=self.max_charge_mw,
            max_discharge_mw=self.max_discharge_mw,
            efficiency=self.efficiency,
        )
