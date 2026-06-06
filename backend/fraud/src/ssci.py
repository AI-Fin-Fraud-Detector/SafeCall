import math
from .const import (
    INFERENCES_PER_TRIGGER,
    DELTA_N,
    LAMBDA,
    TAU,
    BETA_PRIOR_A,
    BETA_PRIOR_B,
    ZETA,
    ETA,
    SSCI_PRIOR_W_MAX,
    SSCI_IDENTITY_PRIOR,
    CALLER_TYPE_NON_CONTACT,
)


def extract_trigger_results(raw_results: list[bool]) -> list[bool]:
    """
    Extract trigger decisions from raw inference results.
    Takes every INFERENCES_PER_TRIGGER-th result (3, 6, 9, ...).
    Example: [F, T, F, T, T, T] -> [F, T]
    """
    if not raw_results:
        return []
    return [
        raw_results[idx]
        for idx in range(INFERENCES_PER_TRIGGER - 1, len(raw_results), INFERENCES_PER_TRIGGER)
    ]


def compute_ssci(trigger_results: list[bool], caller_type: str | None = None) -> dict:
    """
    Compute SSCI confidence and sub-scores from trigger results.
    Based on evidence (by length), agreement (historical), and stability (flip EMA).

    ``caller_type`` carries the caller's relationship to the user
    (CALLER_TYPE_CONTACT / CALLER_TYPE_NON_CONTACT / CALLER_TYPE_PRIVATE). It drives
    the Phase 4 identity-prior adjustment, which blends the raw confidence with a
    per-identity prior; the prior dominates early and fades as evidence grows. None
    or an unrecognized value falls back to the general (non_contact) prior.
    """
    if not trigger_results:
        return {}

    k = len(trigger_results)
    y_k = trigger_results[-1]
    n_values = [DELTA_N * i for i in range(1, k + 1)]  # 6, 12, 18, ...
    n_k = float(n_values[-1])

    # Phase 1: Evidence by length
    evidence = 1.0 - math.exp(-(n_k / LAMBDA))

    # Phase 2: Historical agreement (exclude current trigger, with beta smoothing)
    if k == 1:
        agreement = 0.5
    else:
        weighted_match_sum = 0.0
        weight_sum = 0.0
        for j in range(k - 1):
            n_j = float(n_values[j])
            weight = math.exp(-((n_k - n_j) / TAU))
            weight_sum += weight
            if trigger_results[j] == y_k:
                weighted_match_sum += weight

        agreement = (weighted_match_sum + BETA_PRIOR_A) / (
            weight_sum + BETA_PRIOR_A + BETA_PRIOR_B
        )

    # Phase 3: Recent stability (EMA over flips)
    if k == 1:
        flip_ema = 0.0
        stability = 0.5
    else:
        flip_ema = 0.0
        prev_n = float(n_values[0])
        for idx in range(1, k):
            curr_n = float(n_values[idx])
            delta_n = curr_n - prev_n
            rho_k = 1.0 - math.exp(-(delta_n / ZETA))
            c_k = 1 if trigger_results[idx] != trigger_results[idx - 1] else 0
            flip_ema = ((1.0 - rho_k) * flip_ema) + (rho_k * float(c_k))
            prev_n = curr_n

        stability = math.exp(-(ETA * flip_ema))

    raw_confidence = evidence * agreement * stability

    # Phase 4: Identity Prior Adjustment
    # Blend the raw confidence with a per-identity prior. The blending weight
    # w_k = W_MAX * (1 - E_k) lets the prior dominate early (small evidence) and
    # recede as the dialogue grows (E_k -> 1).
    prior_table = SSCI_IDENTITY_PRIOR.get(
        caller_type, SSCI_IDENTITY_PRIOR[CALLER_TYPE_NON_CONTACT]
    )
    prior = prior_table[1 if y_k else 0]
    prior_weight = SSCI_PRIOR_W_MAX * (1.0 - evidence)
    confidence = (1.0 - prior_weight) * raw_confidence + prior_weight * prior

    scam_probability = confidence if y_k else (1.0 - confidence)

    return {
        "available": True,
        "caller_type": caller_type,
        "trigger_index": k,
        "n_k": int(n_k),
        "latest_trigger_decision": y_k,
        "trigger_results": trigger_results,
        "evidence": round(evidence, 7),
        "agreement": round(agreement, 7),
        "stability": round(stability, 7),
        "flip_ema": round(flip_ema, 7),
        "raw_confidence": round(raw_confidence, 7),
        "identity_prior": round(prior, 7),
        "prior_weight": round(prior_weight, 7),
        "confidence": round(confidence, 7),
        "decision_label": "scam" if y_k else "normal",
        "scam_probability": round(scam_probability, 7),
    }
