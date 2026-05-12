from __future__ import annotations

from shared.models import EnergyAgentCard, StatsResponse

_CROSS_VALIDATE_THRESHOLD = 0.20  # 20% divergence triggers flag


def rank_markets(stats: list[StatsResponse]) -> list[StatsResponse]:
    return sorted(
        [s for s in stats if s.type == "market"],
        key=lambda s: s.total_volume,
        reverse=True,
    )


def rank_traders(stats: list[StatsResponse]) -> list[StatsResponse]:
    return sorted(
        [s for s in stats if s.type == "trader"],
        key=lambda s: s.roi,
        reverse=True,
    )


def rank_energy_agents(
    cards: list[EnergyAgentCard],
    trader_observed: dict[str, float],
) -> list[dict]:
    """
    Sort energy agents by avg_rec_regret (lower = better).
    Cross-validate self-reported vs trader-observed regret.
    trader_observed: {agent_url: observed_avg_regret}
    """
    ranked = []
    for card in cards:
        obs = trader_observed.get(card.url)
        self_rep = card.avg_rec_regret

        if self_rep is not None and obs is not None and obs > 0:
            delta_pct = abs(self_rep - obs) / obs * 100
            flagged = delta_pct > (_CROSS_VALIDATE_THRESHOLD * 100)
        else:
            delta_pct = None
            flagged = False

        ranked.append({
            "name": card.name,
            "url": card.url,
            "specialization": card.specialization,
            "intervals_advised": card.intervals_advised,
            "learning_generation": card.learning_generation,
            "active_traders": card.active_traders,
            "self_reported_regret": self_rep,
            "trader_observed_regret": obs,
            "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
            "flagged": flagged,
            "avg_rec_regret": self_rep,
        })

    ranked.sort(key=lambda x: (x["avg_rec_regret"] is None, x["avg_rec_regret"] or 999))
    return ranked


def network_summary(
    stats: list[StatsResponse],
    energy_cards: list[EnergyAgentCard],
    trader_observed: dict[str, float],
) -> dict:
    markets = [s for s in stats if s.type == "market"]
    traders = [s for s in stats if s.type == "trader"]
    ranked_energy = rank_energy_agents(energy_cards, trader_observed)

    return {
        "total_markets": len(markets),
        "total_traders": len(traders),
        "total_energy_agents": len(energy_cards),
        "total_volume_mwh": sum(s.total_volume for s in traders),
        "total_pnl": sum(s.profit for s in traders),
        "top_energy_agent": ranked_energy[0]["name"] if ranked_energy else None,
    }
