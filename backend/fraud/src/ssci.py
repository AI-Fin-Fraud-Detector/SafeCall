import statistics


def compute_ssci(trigger_results: list[bool], flip_ema: float) -> dict:
    """Compute SSCI metrics from trigger results and flip EMA.

    Returns dict with confidence, scam_probability, evidence, agreement, stability.
    """
    if not trigger_results:
        return {}

    evidence = sum(trigger_results) / len(trigger_results)

    if len(trigger_results) > 1:
        std = statistics.stdev([1.0 if r else 0.0 for r in trigger_results])
        agreement = max(0.0, min(1.0, 1.0 - (std / 0.5)))
    else:
        agreement = 1.0

    stability = 1.0 - flip_ema

    scam_probability = (evidence * 0.5) + (agreement * 0.3) + (stability * 0.2)
    confidence = 1.0 - scam_probability

    return {
        "confidence": round(confidence, 7),
        "scam_probability": round(scam_probability, 7),
        "evidence": round(evidence, 7),
        "agreement": round(agreement, 7),
        "stability": round(stability, 7),
    }


def update_flip_ema(current_ema: float, prev: bool | None, curr: bool, alpha: float) -> float:
    """Update EMA of prediction flips.

    Returns updated EMA value.
    """
    if prev is None:
        return 0.0
    flip = float(prev != curr)
    return (alpha * flip) + ((1.0 - alpha) * current_ema)
