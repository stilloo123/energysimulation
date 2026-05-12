# Narrator

Based on the investigation above, output a JSON recommendation. Output ONLY the JSON object — no prose, no markdown fences.

Required fields:

```
{
  "direction": "charge" | "discharge" | "none",
  "volume_mw": <float, 0–50>,
  "limit_price": <float, $/MWh>,
  "confidence": "high" | "low",
  "reasoning": "<one sentence explaining the direction>",
  "evidence_tool_calls": ["<tool_name>", ...],
  "similar_past_interval_ids": []
}
```

## Field guidance

- **direction**: The recommended action. Use "none" if evidence is insufficient or ambiguous.
- **volume_mw**: How much to bid. Scale down (10–20 MW) if confidence is low or data is sparse. Max 50 MW.
- **limit_price**: For discharge, the minimum acceptable cleared price. For charge, the maximum acceptable cleared price. Set to reference_price ± 10% based on your assessment.
- **confidence**: "high" if tool evidence clearly supports the direction. "low" if heuristics only or mixed signals.
- **reasoning**: One sentence citing the key factor (e.g. "Evening peak hour (18:00) with avg cleared price $180 supports discharge").
- **evidence_tool_calls**: List ONLY the tool names whose results directly support this recommendation. Do NOT list tools you called but didn't use.
- **similar_past_interval_ids**: Leave as empty list [].
