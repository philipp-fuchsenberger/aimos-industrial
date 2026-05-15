"""CR-283: Per-call cost calculation for hosted LLM APIs.

Prices in USD per million tokens (as of 2026-04-07 — verify periodically).
Returns USD cost for one call. Currency conversion is the caller's job.
"""

# (input_per_mtok_usd, output_per_mtok_usd)
# Full Mistral catalog verified against console.mistral.ai on 2026-04-07.
_PRICES_USD = {
    # ── Mistral text models (current generation) ───────────────────────────
    "mistral-small-latest":      (0.15, 0.60),     # Small 4 — workhorse default for routine meta-agents
    "mistral-small-4":           (0.15, 0.60),
    "mistral-medium-latest":     (0.40, 2.00),     # Medium 3
    "mistral-medium-3":          (0.40, 2.00),
    "mistral-large-latest":      (0.50, 1.50),     # Large 3 — flagship general-purpose
    "mistral-large-3":           (0.50, 1.50),
    # Reasoning specialists
    "magistral-small-latest":    (0.50, 1.50),     # same price as Large 3, reasoning-optimized → ADM.3 default
    "magistral-medium-latest":   (2.00, 5.00),     # premium reasoning, only when Magistral Small fails
    # Coding specialists (verified against api.mistral.ai 2026-04-11)
    # NOTE: devstral-small-2507 + devstral-medium-2507 retiring 2026-05-31
    # (Mistral Deprecation Notice 2026-05-04). Migrated callers to
    # `devstral-latest` (price ↑ from $0.10/$0.30 to $0.40/$2.00).
    "devstral-latest":           (0.40, 2.00),     # replacement for retired 2507-variants
    "devstral-2512":             (0.40, 2.00),
    "codestral-latest":          (0.30, 0.90),
    "codestral-2508":            (0.30, 0.90),
    # Vision
    "pixtral-large-latest":      (2.00, 6.00),     # frontier vision
    "pixtral-12b":               (0.15, 0.15),     # lightweight vision — DPL.0 BIOS-screenshots default
    # Edge / legacy text
    "ministral-3-3b":            (0.10, 0.10),
    "ministral-3-8b":            (0.15, 0.15),
    "ministral-3-14b":           (0.20, 0.20),
    "mistral-7b":                (0.25, 0.25),
    "mixtral-8x7b":              (0.70, 0.70),
    "mixtral-8x22b":              (2.00, 6.00),
    # Embedding (per input only, no output)
    "mistral-embed":             (0.10, 0.00),
    "codestral-embed":           (0.15, 0.00),
    # Moderation
    "mistral-moderation":        (0.10, 0.00),
    # Anthropic (fallback only)
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-opus-4-6":           (15.00, 75.00),
    "claude-haiku-4-5":          (0.80, 4.00),
    # Groq (CR-299, 2026-05-05) — sehr schnell, Cross-Provider-Diversitaet
    # Verifiziert gegen groq.com/pricing 2026-05-05
    "llama-3.3-70b-versatile":   (0.59, 0.79),     # 128K context, schnell, Tool-Calls
    "llama-3.1-70b-versatile":   (0.59, 0.79),
    "llama-3.1-8b-instant":      (0.05, 0.08),     # sehr guenstig, Routine, ~1000 t/s
    "mixtral-8x7b-32768":        (0.24, 0.24),     # 32K context, Tool-Calls
    "gemma2-9b-it":              (0.20, 0.20),     # 9B, schnell, mittel
    "gemma-4-31b":               (0.40, 0.60),     # CR-301/Gemma4: 31B Dense, multimodal
                                                    # NICHT auf API verfuegbar Stand 2026-05;
                                                    # Pricing geschaetzt fuer baldigen API-Release
    "deepseek-r1-distill-llama-70b": (0.75, 0.99),  # Reasoning auf Groq
    # DeepSeek (CR-299, 2026-05-05) — sehr guenstig, gut bei Code
    # Verifiziert gegen api-docs.deepseek.com/quick_start/pricing 2026-05-05
    "deepseek-chat":             (0.27, 1.10),     # V3, Tool-Calls, Worker-Default
    "deepseek-reasoner":         (0.55, 2.19),     # R1, Reasoning-Modell
    "deepseek-coder":            (0.14, 0.28),     # Code-Aufgaben
    # Local — no API cost
    "qwen3.5:27b":               (0.0, 0.0),
    "qwen2.5:32b":               (0.0, 0.0),
}


def calc_cost_usd(model: str, in_tokens: int, out_tokens: int) -> float:
    """Return USD cost for one LLM call. Unknown models cost 0 (logged elsewhere)."""
    prices = _PRICES_USD.get(model)
    if prices is None:
        return 0.0
    in_p, out_p = prices
    return (in_tokens / 1_000_000.0) * in_p + (out_tokens / 1_000_000.0) * out_p


def known_models() -> list[str]:
    return sorted(_PRICES_USD.keys())
