# hero_ai_logic.py

from dataclasses import dataclass
from typing import List, Dict, Any, Literal

ActionType = Literal["FOLD", "CALL", "RAISE", "CHECK"]

RANKS = "23456789TJQKA"

@dataclass
class HeroState:
    street: str                # "preflop", "flop", "turn", "river"
    hero_cards: List[str]      # e.g. ["As", "Kd"]
    board: List[str]           # e.g. ["Ah", "Kc", "7d"]
    pot: int
    to_call: int               # amount hero needs to call now
    hero_stack: int
    villain_stack: int
    position: str              # "BTN","CO","HJ","LJ","SB","BB","UTG"
    is_preflop_raiser: bool    # hero was aggressor pre
    num_players: int           # players still in pot
    villain_vpip: float        # simple stat, 0–100
    villain_pfr: float         # simple stat, 0–100
    min_raise: int             # table minimum raise size
    big_blind: int             # for sizing

# -------- Helper: convert ranks for comparison --------

def rank_value(card: str) -> int:
    return RANKS.index(card[0])

def sort_cards_desc(cards: List[str]) -> List[str]:
    return sorted(cards, key=lambda c: rank_value(c), reverse=True)


# -------- 1. Preflop opening range (simplified solver-style) --------

def classify_preflop_hand(hero_cards: List[str]) -> str:
    """
    Returns categories like "premium", "strong", "speculative", "trash".
    Based on common 6-max charts, approximated.
    """
    if len(hero_cards) != 2:
        return "trash"

    c1, c2 = hero_cards
    r1, r2 = c1[0], c2[0]
    s1, s2 = c1[1], c2[1]
    offsuit = (s1 != s2)
    suited = (s1 == s2)

    # Sort ranks by strength
    ranks_sorted = sorted([r1, r2], key=lambda r: RANKS.index(r), reverse=True)
    hi, lo = ranks_sorted

    pair = (r1 == r2)

    # Pairs
    if pair:
        if hi in "AKQJTT":    # AA–TT
            return "premium"
        if hi in "9987":      # 99–77
            return "strong"
        if hi in "654":       # 66–44
            return "speculative"
        return "trash"        # 33–22 in most positions

    # Broadways
    broadway = hi in "AKQJ" and lo in "AKQJT"
    if broadway and suited:
        if hi in "AKQ" and lo in "KQJT":
            return "premium"      # AKs, AQs, AJs, KQs
        return "strong"           # KJs, QJs, JTs suited
    if broadway and offsuit:
        if {hi, lo} == set("AK") or {hi, lo} == set("AQ"):
            return "strong"       # AKo, AQo
        return "speculative"      # AJo, KQo, etc.

    # Suited connectors and one-gappers
    if suited:
        # convert indexes
        hi_idx = RANKS.index(hi)
        lo_idx = RANKS.index(lo)
        gap = hi_idx - lo_idx
        if 1 <= gap <= 3 and hi_idx >= RANKS.index("8"):
            return "speculative"  # 98s, T9s, JTs, QJs etc.
        if 1 <= gap <= 3:
            return "lo_spec"      # small suited connectors
        # suited Ax junk
        if hi == "A":
            return "speculative"

    # Offsuit broadway-ish or medium junk
    if hi == "A" and lo in "T987654":
        return "lo_spec"
    if hi == "K" and lo in "T9":
        return "lo_spec"

    return "trash"


def preflop_decision(state: HeroState) -> Dict[str, Any]:
    cat = classify_preflop_hand(state.hero_cards)

    # Adjust slightly vs recreational (high VPIP, low PFR)
    loose_passive = (state.villain_vpip >= 45 and state.villain_pfr <= 15)
    nitty = (state.villain_vpip <= 18 and state.villain_pfr <= 14)

    open_size = max(2.2 * state.big_blind, state.min_raise)

    # Default action
    action: ActionType = "FOLD"
    amount = 0
    reason = ""

    # Opening (no raise yet) – assume we are first in
    # You may pass extra flags into HeroState if you want “facing_raise” etc.
    if state.to_call == state.big_blind:   # not perfect but okay in your sim
        if cat == "premium":
            action = "RAISE"
            amount = int(2.5 * state.big_blind)
            reason = "Open-raise with premium hand from {}.".format(state.position)
        elif cat == "strong":
            action = "RAISE"
            amount = int(2.3 * state.big_blind)
            reason = "Open-raise strong but non-premium hand."
        elif cat == "speculative":
            if state.position in ("BTN", "CO", "SB"):
                action = "RAISE"
                amount = int(2.2 * state.big_blind)
                reason = "Late-position open with speculative hand."
            else:
                action = "FOLD"
                reason = "Speculative hand but early position – fold preflop."
        elif cat == "lo_spec":
            if state.position == "BTN":
                action = "RAISE"
                amount = int(2.2 * state.big_blind)
                reason = "Exploit late position to open marginal suited/wheel combo."
            else:
                action = "FOLD"
                reason = "Too loose to open from this position."
        else:
            action = "FOLD"
            reason = "Trash hand preflop – fold."
        return {"action": action, "amount": amount, "reason": reason}

    # Facing a single open (you can refine with more fields later)
    # Very rough: 3bet premium, call some strong/speculative, fold junk.
    if state.to_call > state.big_blind:
        pot_odds = state.to_call / max(state.pot + state.to_call, 1)
        if cat == "premium":
            # 3-bet for value
            action = "RAISE"
            amount = int(3.3 * state.to_call)
            reason = "3-bet premium hand for value."
        elif cat == "strong":
            # Mix call/3bet; keep simple: call mostly, 3bet vs loose fish
            if loose_passive:
                action = "RAISE"
                amount = int(3.1 * state.to_call)
                reason = "Exploit loose opener: 3-bet strong hand for value."
            else:
                action = "CALL"
                amount = state.to_call
                reason = "Call raise with strong but non-premium hand."
        elif cat in ("speculative", "lo_spec"):
            if pot_odds < 0.25 and state.position in ("BTN", "CO"):
                action = "CALL"
                amount = state.to_call
                reason = "Call with speculative hand in position with decent pot odds."
            else:
                action = "FOLD"
                reason = "Speculative hand but poor position/odds – fold."
        else:
            # vs nit, we fold even more
            action = "FOLD"
            reason = "Weak hand facing raise – fold preflop."
        return {"action": action, "amount": amount, "reason": reason}

    # default
    return {"action": "CHECK", "amount": 0, "reason": "No reason to invest more preflop."}


# -------- 2. Postflop hand strength bucket (very simplified) --------

def classify_postflop_bucket(hero_cards: List[str], board: List[str]) -> str:
    """
    Very rough bucket: 'nut', 'strong', 'medium', 'weak', 'draw', 'air'.
    You can wire this into your existing 7-card evaluator for more precision.
    """
    if not board or len(hero_cards) != 2:
        return "air"

    all_cards = hero_cards + board
    ranks = [c[0] for c in all_cards]
    board_ranks = [c[0] for c in board]

    # Count pairs with board
    hero_ranks = [hero_cards[0][0], hero_cards[1][0]]
    hero_same = (hero_ranks[0] == hero_ranks[1])

    # Simple top pair check
    board_sorted = sort_cards_desc(board)
    top_board_rank = board_sorted[0][0]

    # Sets: pair in hand + 1 on board
    set_or_better = False
    for r in hero_ranks:
        if board_ranks.count(r) == 2 or (hero_same and board_ranks.count(r) >= 1):
            set_or_better = True

    # Flush draw: 4 of same suit among hero+board
    suits = [c[1] for c in all_cards]
    flush_draw = any(suits.count(s) == 4 for s in "cdhs")

    # Straight draw: very naive – just check for 4 connected ranks
    uniq_ranks = sorted(set(ranks), key=lambda r: RANKS.index(r))
    has_sd = False
    for i in range(len(uniq_ranks) - 3):
        seq = uniq_ranks[i:i+4]
        if RANKS.index(seq[-1]) - RANKS.index(seq[0]) == 3:
            has_sd = True

    # Core classification
    if set_or_better:
        return "nut"
    if any(r == top_board_rank for r in hero_ranks):
        # top pair
        return "strong"
    if flush_draw or has_sd:
        return "draw"
    # 2nd pair-ish
    second_board_rank = board_sorted[1][0] if len(board_sorted) > 1 else None
    if second_board_rank and any(r == second_board_rank for r in hero_ranks):
        return "medium"

    # Underpair or nothing
    return "weak"


def postflop_decision(state: HeroState) -> Dict[str, Any]:
    bucket = classify_postflop_bucket(state.hero_cards, state.board)
    pot = max(state.pot, 1)
    to_call = state.to_call
    stack = state.hero_stack

    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0

    # Bet sizes
    small_bet = int(0.33 * pot)
    big_bet = int(0.75 * pot)
    jam = stack   # all-in for simplicity

    # Default outputs
    action: ActionType = "CHECK"
    amount = 0
    reason = f"{bucket} hand postflop."

    # No bet to us, we are aggressor / IP simple logic
    if to_call == 0:
        if bucket == "nut":
            action, amount, reason = "RAISE", big_bet, "Value-bet big with very strong hand."
        elif bucket == "strong":
            action, amount, reason = "RAISE", small_bet, "Value-bet strong but non-nut hand."
        elif bucket == "draw":
            action, amount, reason = "RAISE", small_bet, "Semi-bluff with draw."
        elif bucket == "medium":
            action, amount, reason = "CHECK", 0, "Check medium-strength hand to pot-control."
        else:  # weak or air
            action, amount, reason = "CHECK", 0, "Check with weak/air hand."
        return {"action": action, "amount": amount, "reason": reason}

    # Facing a bet (to_call > 0)
    if bucket == "nut":
        # Raise or sometimes call – keep simple: raise big
        action = "RAISE"
        amount = max(big_bet, int(2.5 * to_call))
        reason = "Raise for value with very strong made hand."
    elif bucket == "strong":
        # Mostly call, sometimes raise vs fish
        if state.villain_vpip >= 40:
            action = "RAISE"
            amount = max(small_bet, int(2.2 * to_call))
            reason = "Raise strong hand vs loose opponent for value."
        else:
            action = "CALL"
            amount = to_call
            reason = "Call with strong made hand to keep pot manageable."
    elif bucket == "draw":
        # Compare pot odds to rough draw odds ~20–35%
        if pot_odds <= 0.25:
            action = "CALL"
            amount = to_call
            reason = "Call with draw – pot odds reasonable."
        else:
            # sometimes semi-bluff raise if deep
            if stack > 3 * pot:
                action = "RAISE"
                amount = max(small_bet, int(2.2 * to_call))
                reason = "Semi-bluff raise with draw, deep effective stacks."
            else:
                action = "FOLD"
                amount = 0
                reason = "Fold draw – pot odds too poor."
    elif bucket == "medium":
        # Pot control / fold to big pressure
        if pot_odds <= 0.20:
            action = "CALL"
            amount = to_call
            reason = "Call small bet with medium-strength hand."
        else:
            action = "FOLD"
            amount = 0
            reason = "Fold medium-strength hand vs large bet."
    else:  # weak / air
        # Very occasionally bluff-raise small bets vs fit-or-fold types
        if pot_odds < 0.10 and state.villain_vpip <= 25:
            action = "RAISE"
            amount = max(small_bet, int(2.5 * to_call))
            reason = "Bluff-raise small bet vs tight opponent."
        else:
            action = "FOLD"
            amount = 0
            reason = "Fold weak/air hand facing a bet."

    return {"action": action, "amount": amount, "reason": reason}


# -------- 3. Main entry point you call from app.py --------

def hero_ai_decision(raw_state: Dict[str, Any]) -> Dict[str, Any]:
    """
    raw_state: dict you build inside app.py when it's Hero's turn.
    It must contain the keys required by HeroState.
    """
    state = HeroState(**raw_state)

    if state.street == "preflop":
        return preflop_decision(state)
    else:
        return postflop_decision(state)
