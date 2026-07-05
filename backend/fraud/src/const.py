# ========== GPT設定 ==========
MAX_TOKENS = 300
TEMPERATURE = 0.8
TOP_P = 0.95

# ========== Caller Identity ==========
# Relationship of the incoming caller to the user. Computed on the mobile app
# and carried through to compute_ssci as a prior. Keep these wire values in sync
# with the Android CallerType constants.
CALLER_TYPE_CONTACT = "contact"          # number found in the user's contacts
CALLER_TYPE_NON_CONTACT = "non_contact"  # has a number but not in contacts
CALLER_TYPE_PRIVATE = "private"          # withheld / no number ("未顯示來電")

# ========== SSCI Configuration ==========
# SSCI (Streaming Scam Confidence Index) Parameters
# 每次目前判斷約等於 2 句（caller + receiver），所以每 3 次判斷對應 Δn=6。
INFERENCES_PER_TRIGGER = 3
SENTENCES_PER_INFERENCE = 2
DELTA_N = INFERENCES_PER_TRIGGER * SENTENCES_PER_INFERENCE  # = 6
LAMBDA = 12.0
TAU = 6.0
BETA_PRIOR_A = 0.05
BETA_PRIOR_B = 0.05
ZETA = 200.0
ETA = 1.5

# Phase 4: Identity Prior Adjustment
# Upper bound of the prior's influence; the blending weight is w_k = W_MAX*(1-E_k),
# so the prior dominates early (short dialogue) and fades as evidence accumulates.
SSCI_PRIOR_W_MAX = 0.8
# Per-identity prior confidence P_id(y_k), keyed by caller_type then by the decision
# y_k (0 = non-scam, 1 = scam). A trusted contact yields high confidence for a
# non-scam decision and low confidence for a scam decision; unknown numbers reverse
# this. None / unrecognized caller_type falls back to the general (non_contact) prior.
SSCI_IDENTITY_PRIOR = {
    CALLER_TYPE_CONTACT: {0: 0.9, 1: 0.1},      # trusted
    CALLER_TYPE_NON_CONTACT: {0: 0.5, 1: 0.5},  # general / standard
    CALLER_TYPE_PRIVATE: {0: 0.2, 1: 0.8},      # unknown / suspicious
}

# Per-identity scam decision thresholds (scam_probability > threshold → fraud alert).
# Compensates the Phase 4 identity prior: the prior suppresses a contact's scam
# probability, so contacts alarm at a lower bar; a private caller's probability is
# already boosted by the prior, so it needs a higher bar. None / unrecognized
# caller_type falls back to the non_contact threshold.
SSCI_SCAM_THRESHOLDS = {
    CALLER_TYPE_CONTACT: 0.40,
    CALLER_TYPE_NON_CONTACT: 0.50,
    CALLER_TYPE_PRIVATE: 0.55,
}
SSCI_MAX_DURATION_SECONDS = 120   # minimum call age before SSCI action can fire
SSCI_SCAM_GRACE_SECONDS = 30
SSCI_SCAM_WAIT_SECONDS = 90
SSCI_SAFE_WAIT_SECONDS = 180

# ========== ANTI-FRAUD SYSTEM PROMPT (ENGLISH) ==========
ANTI_FRAUD_SYSTEM_PROMPT = """You are {user_name}, a person who can be reached at phone number {user_phone}.

You've just answered a phone call from an unknown caller. You should act naturally as yourself - {user_name} - but be cautious about providing personal information to strangers.

Your behavior strategy:
- Sound confused about unexpected calls, especially if they claim to be from banks, tech support, government agencies, etc.
- Ask clarifying questions like "Who is this?", "Which [bank/company] are you from?", "Why are you calling me?"
- Express that you don't understand technical terms or legal jargon
- Be hesitant about providing personal information: "I'm not comfortable giving that information over the phone"
- Sometimes ask them to repeat things: "I didn't catch that, could you say that again?"
- Occasionally ask how they got your number

Speech characteristics for {user_name}:
- Use natural, conversational English
- Sound slightly cautious but not immediately hostile
- Ask questions when things don't make sense
- Express uncertainty: "I'm not sure I understand...", "That doesn't sound right..."

Remember: You are {user_name} answering your phone at {user_phone}. Stay in character and be naturally suspicious of unsolicited calls."""

