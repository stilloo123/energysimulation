# Strategy Context — Bootstrapped from Historical CSV

avg_regret_proxy: 1454.41  |  intervals_analysed: 287

## Learned Rules

1. During hours 09-10, where avg_regret exceeds 6,000-20,000 and cleared prices regularly surpass 19,000, aggressively prioritize discharge regardless of SOC regime; the agent is systematically missing extreme price spikes in these hours.
2. During hour 08, with avg_regret ~4,072 and cleared price ~4,871, ensure discharge is the default action; the dominant_direction is already discharge but regret remains high, suggesting limit prices are set too low and are not capturing the full price spike.
3. For high_soc regime (n=137, avg_regret=2,331), the dominant direction is charge despite high cleared prices (~2,067), indicating the agent is incorrectly charging when it should be discharging; add a rule to force discharge or idle when SOC is high and price exceeds a threshold (e.g., >500).
4. During overnight hours 00-04 where dominant direction is charge and prices are low (<170), charging is correct but regret in hours 02-03 is elevated (329-679); review whether charge limit prices are set too high, causing the agent to miss favorable charge opportunities at the actual cleared price.
5. During hours 11-13, dominant_direction is 'none' yet regret remains moderate (123-537) against high cleared prices (387-12,139); implement a discharge bias when cleared price exceeds 1,000 and SOC is mid or high rather than remaining idle.
6. For mid_soc regime (avg_regret=824, dominant_direction=charge), cross-check against hour-of-day: charging at mid-SOC during hours 09-11 when prices are at daily peaks is likely the primary regret driver; override charge recommendations with discharge when hour is 08-10 and SOC >= mid.
7. Hours 14-16 and 18-23 show near-zero regret (<20), confirming the agent performs well in afternoons and evenings; preserve existing logic for these hours and focus optimization budget entirely on the 08-11 morning spike window.
